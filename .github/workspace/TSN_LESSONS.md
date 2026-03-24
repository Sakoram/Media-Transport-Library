# TSN LaunchTime in MTL + DPDK — Lessons Learned

## 1. E830 NIC LaunchTime Does NOT Hold Packets

**The most important lesson.** Despite documentation suggesting LaunchTime schedules packets for future transmission, the E830 NIC (device 12d2) does NOT hold packets. If you submit a descriptor with LaunchTime T and the current NIC time is already past T, the packet goes out immediately at wire rate.

**Implication:** Software must gate descriptor submission. Submit each bulk just before its scheduled time using a TSC gate. LaunchTime provides only fine-tuning (sub-µs alignment), not macro-level pacing.

## 2. 128ns Quantization Destroys Double-Precision Pacing

E830 LaunchTime timestamps are quantized to 128ns (right-shift by 7 bits, `>> 7`). PTP timestamps are ~1.77×10¹⁸ nanoseconds (March 2026 TAI). At this magnitude:

- `double` has ULP ≈ 256ns (only 52-bit mantissa)
- `uint64_t += double(7776.43)` truncates to `7680ns = floor(7776/256)*256`
- Over 4115 packets: `4115 × 96ns = 395µs` lost per frame

**Fix:** Use integer-only Bresenham arithmetic in 128ns ticks. Zero truncation error.

## 3. Per-Packet Logging Kills Pacing

`info()` → `vfprintf()` → synchronous syscall → ~50µs stall on the TX lcore. During this stall, the TSC-gated transmitter cannot run, so 6-7 consecutive TSC gates expire. When the stall ends, all 6-7 packets burst out simultaneously → Cinst = 7.

**Rule:** NEVER use `info()` on the TX fast path. Use `dbg()` (compiled out in release) or USDT probes.

## 4. Bulk Size Controls Scheduler Overhead

With `bulk=1`, each packet requires 3-5 scheduler dispatches:
1. Transmitter: check TSC gate → returns early
2. Transmitter: gate passed → dequeue 1 pkt → save inflight → return
3. Transmitter: burst 1 inflight pkt → return
4. Builder: enqueue 1 pkt to ring → return

Total overhead: ~1.5µs/pkt on top of the trs interval.

With `bulk=4`, the same overhead is amortized over 4 pkts → ~0.4µs/pkt.

**Consequence for 1080p30:** trs ≈ 7.776µs, frame_active ≈ 32ms, frame_time = 33.333ms.
- bulk=1: drain = (4115-512) × 9.3µs ≈ 33.5ms > frame_time → epoch advance fires every frame → 2× frame rate
- bulk=4: drain = (4115-512) × 8.2µs ≈ 29.5ms < frame_time → healthy margin

## 5. Ring Size Creates Backpressure

Ring=512 means the builder can only be ~512 packets ahead of the transmitter. Since the transmitter drains at trs rate (~7.8µs/pkt), the ring capacity = ~4ms of data. The builder blocks when the ring is full, naturally synchronizing to real-time.

Ring too large (e.g. 16384) → builder runs ahead, fills all frames at CPU speed → `time_to_tx` goes negative on every sync → cumulative drift.

**Lesson:** Small ring is essential for TSN. The 512 default is correct.

## 6. Epoch Advance Must Be Bootstrap-Only

The epoch-advance mechanism (`cur_epochs++` when `time_to_tx <= 0`) is essential for first-frame bootstrap but dangerous if it fires in steady state.

**Bootstrap problem:** On the first frame, the builder is mid-epoch. `time_to_tx` = epoch_start - current_time = negative (up to -33ms). Without advance, time_to_tx gets clamped to 0, ptp_time_cursor points to the past, creating a permanent VRX offset of -1000 to -2000.

**Steady-state danger:** If drain_time > frame_time (even by 170µs), time_to_tx goes slightly negative every frame. Advance fires every frame → `cur_epochs += 2` per frame → 2× frame rate.

**Solution:** Use a `while (time_to_tx <= 0)` loop that advances multiple epochs if needed. With bulk=4, drain < frame_time → loop never fires after bootstrap. With bulk=1, drain > frame_time → loop fires every frame → must not use bulk=1 with TSN.

**Threshold history:**
- `<= 0`: Works for bootstrap, but fires every frame with bulk=1
- `< -(frame_time/2)`: Too lenient, doesn't fire on typical -10 to -15ms first-frame deficit → permanent VRX offset of -2239
- `<= 0` with while-loop + bulk=4: Correct. Advances 1-2 times on bootstrap, never fires in steady state.

## 7. calc_frame_count_since_epoch Always Returns cur_epochs+1

In normal operation (builder not late), `calc_frame_count_since_epoch` returns `next_free_frame_slot = cur_epochs + 1`. This is correct — each call advances by exactly one epoch.

The epoch-drop path (frame_count_tai > next_free_frame_slot) fires when current PTP time has advanced more than one frame since last sync. This is normal at startup and after long stalls.

## 8. DPDK LaunchTime Descriptor Format

- Set `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP` in port txmode.offloads
- Set `ol_flags |= inf->tx_launch_time_flag` on each mbuf
- Write PTP timestamp to dynamic field: `*RTE_MBUF_DYNFIELD(mbuf, offset, uint64_t*) = ptp_time`
- The ICE driver reads this and writes it to the advanced TX descriptor's `launch_time` field
- NIC truncates to 128ns ticks internally

## 9. trs Is Not Exactly frame_time / total_pkts

```
trs = frame_time × reactive / total_pkts
    = 33,333,333.33 × (1080/1125) / 4115
    = 7776.43 ns
```

The `reactive = 1080/1125 = 0.96` accounts for vertical blanking (45 lines of 1125 total). Frame active time = 32,000,000ns. Frame idle time = 1,333,333ns. Packets only go out during active time, then there's a ~1.33ms gap before the next frame.

## 10. TSC vs PTP Time Domains

- `tsc_time_cursor`: Used for software gate timing (when to submit to NIC)
- `ptp_time_cursor`: Written into LaunchTime descriptor (NIC timestamp)
- They must advance in lockstep: `tsc += step_ns; ptp += step_ns;`
- Initial alignment: `tsc = cur_tsc + time_to_tx_ns` and `ptp = start_time_tai`
- TSC drifts relative to PTP (different oscillators) but over one frame (33ms) the drift is negligible (<1µs)
