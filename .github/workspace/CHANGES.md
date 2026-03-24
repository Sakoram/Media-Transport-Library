# TSN LaunchTime Pacing — All Changes vs `origin/main`

## Goal
Achieve ST 2110-21-2022 **narrow** compliance for `--pacing_way tsn` on Intel E830 NIC (device 12d2) without `--ptp`.

## Test Setup
- 1080p30 progressive, YCbCr-4:2:2, 10-bit
- 4115 pkts/frame, trs ≈ 7776.43 ns, frame_time ≈ 33.333 ms
- Compliance: cmax_narrow=4, vrx_full_narrow=8

---

## Fix 1: LaunchTime TX offload flag (`mt_dev.c`)

**File:** `lib/src/dev/mt_dev.c`

**What:** Enable `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP` in port config when `MT_IF_FEATURE_TX_OFFLOAD_SEND_ON_TIMESTAMP` is set.

**Why:** Without this flag, the NIC ignores LaunchTime descriptors entirely. Packets go out at wire rate immediately, making TSN pacing non-functional.

**Without fix:** All packets in a frame transmit as fast-as-wire burst. No pacing at all.

---

## Fix 2: PTP announce self-reject (`mt_ptp.c`)

**File:** `lib/src/mt_ptp.c`

**What:** Reject PTP announce messages that came from our own port identity.

**Why:** In TSN mode without `--ptp`, MTL's PTP engine could accept its own announce messages reflected back, causing incorrect grandmaster selection and time jumps.

**Without fix:** PTP time discontinuities causing epoch calculation errors.

---

## Fix 3: Bresenham integer pacing (`st_header.h`, `st_tx_video_session.c`)

**Files:** `lib/src/st2110/st_header.h`, `lib/src/st2110/st_tx_video_session.c`

**What:** Added `ptp_cursor_128ns_align` flag and Bresenham parameters to `st_tx_video_pacing`. Replaced `ptp_time_cursor += trs` (double) with integer-only 128ns-tick arithmetic in `pacing_forward_cursor()`.

**Why:** E830 NIC quantizes LaunchTime to 128ns (right-shift 7). At PTP magnitude ~1.77×10¹⁸, `uint64_t += double` has ULP ≈ 256ns. The effective step becomes `floor(trs/256)*256 = 7680ns` instead of target 7776ns. Over 4115 packets this loses ~395µs/frame → cumulative VRX drift.

Bresenham distributes the fractional 128ns tick evenly: base=60 ticks (7680ns), extra=3100 of 4115 pkts get +1 tick (7808ns). Total = 250000 ticks = 32,000,000ns = exact frame active time.

**Without fix:** 3% frame span error → VRX drifts 395µs/frame, rapidly exceeding limits.

---

## Fix 4: 128ns cursor alignment in `tv_sync_pacing()`

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** At frame start, align `ptp_time_cursor` to 128ns boundary: `(ptp + 127) >> 7 << 7`. Reset Bresenham accumulator to 0.

**Why:** Without alignment, the initial cursor may sit at an arbitrary nanosecond offset. NIC truncates each LaunchTime → first packet shifts, and the Bresenham accumulator starts from a non-zero implicit error.

**Without fix:** First packet of each frame has up to 127ns jitter. Compounds with Bresenham across 4115 packets.

---

## Fix 5: Per-bulk TSC gate in transmitter (`st_video_transmitter.c`)

**File:** `lib/src/st2110/st_video_transmitter.c`

**What:** Complete rewrite of `video_trs_launch_time_tasklet()`. Added per-bulk TSC gate (identical pattern to TSC transmitter): check `trs_target_tsc` at entry, check per-bulk after dequeue, save inflight if too early.

Removed `mt_ptp_is_locked()` guard (fallback to TSC transmitter breaks when using TSN without PTP).

**Why:** Original LaunchTime transmitter had no software pacing gate — it submitted all packets from the ring immediately. The E830 NIC's LaunchTime does NOT hold packets (unlike some docs suggest). Without a TSC gate, all 4115 packets arrive at the NIC within microseconds → terrible Cinst (hundreds).

**Without fix:** All packets in frame burst out within ~1ms. Cinst = 700+. NIC LaunchTime alone is insufficient.

---

## Fix 6: Dummy packet cursor skip

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** Only call `pacing_forward_cursor()` when `st20_pkt_idx < st20_total_pkts`.

**Why:** With bulk=4 and total_pkts=4115 (not divisible by 4), the last bulk has 3 real + 1 dummy. Advancing cursor for the dummy adds one extra trs step (~7680ns), inflating frame_span beyond the ideal 32ms.

**Without fix:** Frame span inflated by 7680ns → VRX offset by ~1 trs unit.

---

## Fix 7: info() → dbg() for per-frame logging

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** Demoted per-frame prints from `info()` to `dbg()`. Frame-done diagnostic wrapped in `#ifdef DEBUG`.

**Why:** `info()` → synchronous `vfprintf()` → ~50µs stall on TX lcore. During the stall, the transmitter misses ~6 TSC gates → 6-7 consecutive packets burst when it resumes → Cinst = 7 (exceeds narrow cmax=4).

**Without fix:** Cinst peaks at 7 on every frame (Cinst wide=16 might pass, narrow=4 fails).

---

## Fix 8: One-shot bootstrap epoch advance + no steady-state advance

**Files:** `lib/src/st2110/st_tx_video_session.c`, `lib/src/st2110/st_header.h`

**What:** Added `tsn_initial_epoch_done` flag to pacing struct. On FIRST `tv_sync_pacing()` call with TSN, if `time_to_tx < 0`, advance `cur_epochs++` and recompute. Set flag, never advance again. In steady state, negative `time_to_tx` is simply clamped to 0.

**Why — two problems solved:**

**Problem A (wtorek4): Advance cascade** — any advance mechanism that fires in steady state destroys throughput:
1. Advance pushes `time_to_tx` to +26ms headroom
2. Builder fills 512-entry ring (~1ms), then sits **blocked for 26ms** until TX starts
3. Total builder cycle = 26ms wait + 28ms build = 54ms (2× frame_time)
4. Next frame even more behind → advance again → cascade
5. Result: 24-25fps, Cinst up to 42757

All threshold-based loops cascaded (`<=0`, `<-(frame_time/2)`, `<-tr_offset`, while-loops).
Any info() logging on the hot path creates a feedback loop that amplifies the cascade.

**Problem B (wtorek5): Bootstrap epoch offset** — without ANY advance, first frame gets:
- Epoch E = floor(cur_tai / frame_time), position P within epoch
- `time_to_tx = tr_offset - P` → negative, clamped to 0
- RTP timestamp encodes epoch E, but packet goes out P ms after epoch start
- **Actual TRO = P (10-17ms) instead of tr_offset (1.274ms)**
- Permanent VRX offset of -(P - tr_offset)/trs ≈ -1200 packets
- Result: VRX = -2050 to -1197, pkt_ts_vs_rtp = 9.35-15.99ms

**The one-shot fix:**
- Bootstrap: advance 1 epoch, time_to_tx = frame_time + tr_offset - P (positive). TRO ≈ tr_offset ✅
- Steady state: flag set, advance never fires. time_to_tx ≈ +5.3ms (ring equilibrium)
- Small stalls: time_to_tx clamped to 0, auto-recovers in ~3 frames
- Large stalls: EPOCH DROP in `calc_frame_count_since_epoch()` handles naturally

**Without fix A:** 24-25fps, advance cascades. **Without fix B:** VRX = -2050, TRO = 11.7ms.

---

## Fix 9: TSN uses bulk=4 (not bulk=1)

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** Removed TSN from the `bulk=1` branch (TSC_NARROW only). TSN uses default bulk=4.

**Why:** With bulk=1, each packet needs 3-5 scheduler dispatches (TSC gate poll, inflight burst, dequeue, build). Overhead ≈ 1.5µs/pkt → effective_trs ≈ 9.3µs → drain_time for (4115−512) pkts ≈ 33.5ms > frame_time (33.333ms). This makes `time_to_tx_ns` negative every frame → epoch-advance fires every frame → +2 epochs per frame → 2× frame rate.

With bulk=4, overhead drops to ~0.4µs/pkt → drain ≈ 30ms < frame_time. Cinst peak = 3 (within narrow cmax=4). VRX compensation applied: `vrx -= (bulk-1)`.

**Without fix:** Stream at 15fps, Cinst up to 1942, VRX -1830 to +1507, pkt_ts_vs_rtp_ts 10-30ms.

---

## Fix 10: Build timeout skip for TSN

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** Skip `stat_exceed_frame_time` / `cbs_build_timeout` check when pacing_way is TSN.

**Why:** TSN builder is rate-limited by NIC TX ring backpressure (ring=512, ~12% of frame). Builder naturally takes ~frame_active_time to finish. This is expected, not a timeout.

**Without fix:** False timeout stats every frame. Could trigger CBS (credit-based shaping) renegotiation.

---

## Fix 11: EPOCH DROP promoted to info() → REVERTED back to dbg()

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** Initially promoted epoch drop message from `dbg()` to `info()`. **REVERTED** back to `dbg()`.

**Why promoted:** Epoch drops are significant events. Needed visibility for debugging.

**Why reverted:** `info()` → `vfprintf()` → ~50µs stall **on the TX lcore hot path**. This stall causes the builder to miss the next frame's window, triggering another epoch drop → chain reaction. This is the root cause of:
- Periodic epoch drops every ~10 seconds in wtorek7
- Cinst spikes to 101 (should be 0-4)
- VRX dips to -93 (should be -2 to 8)

The epoch drop counter `stat_epoch_drop` is still incremented and reported in periodic stats — no visibility lost.

**Added:** Detailed comment in code explaining why `dbg()` is mandatory here.

---

## Fix 12: Epoch-based RTP timestamps for TSN → REVERTED

**File:** `lib/src/st2110/st_tx_video_session.c`

**What:** REVERTED. Originally changed TSN to use `tai_from_frame_count(cur_epochs)` (epoch boundary) for RTP timestamps. Now back to using `ptp_time_cursor` (default MTL behavior).

**Why it was wrong — the key insight from wtorek2 vs wtorek7:**

| Metric | wtorek2 (ptp_cursor RTP) | wtorek7 (epoch-based RTP) |
|--------|--------------------------|---------------------------|
| pkt_ts_vs_rtp | 5-7µs ✅ | 1239µs ❌ (limit: 1000µs) |
| VRX | -2 to 8 ✅ | -93 to 8 (99.93% narrow) |
| Cinst | 0-9 ✅ | 0-101 ❌ (info() stalls) |
| TRO | 1.239ms ✅ | 1.239ms ✅ |
| fps | 24 ❌ (advance cascade) | 30 ✅ |

**The pkt_ts_vs_rtp relationship:**
- With `ptp_time_cursor` RTP: `pkt_ts_vs_rtp = capture_time - ptp_cursor ≈ cable_delay (5µs)` ✅
- With epoch-based RTP: `pkt_ts_vs_rtp = capture_time - epoch_boundary ≈ tr_eff (1235µs)` ❌

EBU LIST computes TRO independently from the first-packet arrival pattern relative to frame period, NOT from the RTP timestamp. Both approaches gave the same TRO—the RTP source only affected pkt_ts_vs_rtp.

wtorek2's only problem was 24fps from the advance cascade (Fix 8). With Fix 8 (one-shot bootstrap + clamp-to-0), ptp_cursor-based RTP should give the best of both worlds.

**Without fix (original epoch-based):** pkt_ts_vs_rtp = 1239µs (exceeds 1ms limit).

---

## Non-functional changes (config/script)

| File | Change | Purpose |
|------|--------|---------|
| `config/tx_1v.json` | PCI addr, fps p30, input path | Local test config |
| `config/rx_1v.json` | PCI addr | Local test config |
| `script/build_dpdk.sh` | Commented out `rm -rf` of DPDK source | Keep source for debugging |

---

## Test Result History

| Capture | Frames | Cinst | VRX | RTP delta | Status |
|---------|--------|-------|-----|-----------|--------|
| narrow (17) | 17 | 0-1 ✅ | 1-8 ✅ | 3000 ✅ | COMPLIANT (too short) |
| monday3 (17) | 17 | 0-7 ❌ | -60 to 8 ❌ | 3000 ✅ | info() stalls |
| monday4-big (159) | 159 | 0-701 ❌ | -3482 to 8 ❌ | 3000-6000 ❌ | epoch doubling (ring=16384) |
| monday4-big2 (81) | 81 | 0-1 ✅ | -3147 to -588 ❌ | 3000 ✅ | cum. drift (ring=16384) |
| monday-cleaned (42) | 42 | 0-1942 ❌ | -1830 to 1507 ❌ | 6000 ❌ | bulk=1 epoch doubling |
| wtorek (30) | 30 | 0-1 ✅ | -2239 to -1385 ❌ | 3000 ✅ | bootstrap offset (threshold too lenient) |
| wtorek2 (30) | 30 | 0-9 ~✅ | -2 to 8 ✅ | 3000 ✅ | 24fps (`<=0` loop fires sporadically) |
| wtorek3 (30) | 30 | 0-173 ❌ | -208 to 8 ❌ | 3000 ✅ | 24fps (`<-tr_offset` loop), wire=30fps |
| wtorek4 (29) | 29 | 0-42757 ❌ | -2059 to 2143 ❌ | 3000-6000 ❌ | 25fps, advance cascade, info() feedback loop |
| wtorek5 (30) | 30 | 0-1 ✅ | -2050 to -1197 ❌ | 3000 ✅ | 30fps, no advance. TRO=11.7ms (RTP includes tr_offset) |
| wtorek6 (30) | 30 | 0-1 ✅ | -3367 to 67 ❌ | 3000 ✅ | 30fps, 1-shot advance. TRO avg=16.4ms, two VRX bands |

**Current:** Fix 12 REVERTED (ptp_cursor RTP restored), Fix 11 REVERTED (EPOCH DROP back to dbg). Expected: pkt_ts_vs_rtp≈5µs + VRX -2 to 8 + Cinst 0-4 + 30fps. Ready for test.
