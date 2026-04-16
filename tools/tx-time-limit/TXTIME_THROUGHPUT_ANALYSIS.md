# TXTIME HW Pacing Throughput Limit Analysis

**Platform:** Intel E830 100G NIC, DPDK 25.11 (9 patches), MTL TxTime branch  
**Workload:** ST2110-20 4K p120 (3840×2160, YUV422 10-bit RFC4175PG2BE10)  
**Pacing mode:** TSN LaunchTime (TXTIME doorbell via `ice` driver)  
**Observed throughput:** ~90 fps (target: 120 fps = ~19.9 Gbps)  
**Data integrity:** Bit-perfect (SHA-256 match on all received frames)  
**Pacing accuracy:** R² = 0.999992 at 720p30 (verified with pcap analysis)

---

## 1. Stream Characteristics

| Parameter | Value |
|-----------|-------|
| Packets per frame | 16,458 |
| Packet size (RTP payload) | 1,260 bytes |
| Inter-packet spacing (trs) | 486.086 ns |
| Frame time | 8,333,333 ns (8.333 ms) |
| Reactive span | 8,000,000 ns (8.0 ms, 96% of frame) |
| tr_offset | 318,519 ns |
| Packet rate at 120 fps | 1,974,960 pps (~1.975 Mpps) |
| Line rate required | ~19.9 Gbps |
| Bresenham tick | 128 ns (E830 TXTIME resolution) |
| Bresenham pattern | Alternates 3×128=384 and 4×128=512 ns ticks |

## 2. System Configuration

| Component | Value |
|-----------|-------|
| NIC TX descriptor ring | 4,096 (changed from default 2,048) |
| E830 max TX descriptors | 8,096 (`ICE_MAX_RING_DESC_E830`) |
| Software ring (builder→transmitter) | 65,536 entries (next po2 above 2×16458) |
| Bulk size (dequeue/burst) | 32 (changed from default 4) |
| Frame buffers (framebuff_cnt) | 2 |
| Mbuf mode | Chain (extbuf attached to frame data) |
| Scheduler tasklets on lcore 28 | 3 (builder + transmitter + PTP/RTCP) |
| Scheduler avg loop time | 183–193 ns |
| tx_rs_thresh (DPDK ice default) | 32 |
| tx_free_thresh (DPDK ice default) | 32 |

## 3. Observed Steady-State Metrics (10-second intervals)

| Metric | Value |
|--------|-------|
| TX FPS | 88–91 |
| Epoch drops (skipped frame slots) | 103–163 per 10s |
| Inflight retries | 41–42M per 10s |
| Partial saves (burst rejected) | 12.8–13.3M per 10s |
| Inflight peak | 31 / 0 (31 pkts waiting for NIC ring space) |
| Dequeue empty | 17–24K per 10s |
| SW ring peak | 22,048–22,144 (of 65,536) |
| SW ring backpressure (ring_stop) | 0 |
| Builder ring stops | 0 |
| Drift (ptp_avg per frame) | +2.68 ms |
| Frame overhead avg | 8.8–9.0 ms |
| App busy (no frame ready) | 886–906 per 10s (≈once per frame) |
| Frames in trans | 2 / 2 (always both buffers locked) |
| Launch time delta at dequeue | −1,128 ns (1.1 ms in the past) |
| MDD events | 0 |

## 4. Root Cause: The Throughput Ceiling

### 4.1 The NIC TX Ring Is a Sliding Window

The NIC TX descriptor ring holds 4,096 entries. The HW drains them at the TXTIME-paced
rate of 1 packet per 486 ns:

```
Ring buffer time = 4096 × 486 ns = 1,990,656 ns ≈ 2.0 ms
Frame packet span = 16,458 × 486 ns = 7,998,588 ns ≈ 8.0 ms
Ring coverage = 4096 / 16458 = 24.9% of one frame
```

The ring acts as a **~2 ms sliding window** into the future. The transmitter must
continuously feed the remaining 75% of the frame's packets incrementally, at exactly
the HW drain rate, or fall behind.

### 4.2 The Inflight Retry Tax

When the NIC ring is full, `rte_eth_tx_burst()` returns 0 (no packets accepted). The
transmitter saves the batch as "inflight" and retries on the next scheduler loop (~190 ns
later). The NIC frees one descriptor slot every ~486 ns, meaning:

```
Scheduler loops per freed slot = 486 / 190 ≈ 2.6 loops
Useful push rate = 1 loop in 2.6 = 38.5% of transmitter loops
Wasted retry rate = 1.6 loops in 2.6 = 61.5% of transmitter loops
```

**Observed:** 41M inflight retries vs ~14.6M successful pushes per 10s → **74% of
transmitter cycles are wasted retries** (ring full, 0 packets accepted).

Each retry is cheap (~190 ns), but the volume is enormous. The transmitter spends most of
its time spinning on `rte_eth_tx_burst(ring_full) → 0`.

### 4.3 Frame Buffer Recycling Is the Gating Constraint

With chain mbufs (extbuf), each packet's chain mbuf references the frame buffer via
`rte_pktmbuf_attach_extbuf()`. The `sh_info.refcnt` increments for every attached packet.
When the NIC transmits a packet and DPDK cleans up the descriptor, the mbuf is freed,
decrementing `sh_info.refcnt`. When it reaches 0, `tv_frame_free_cb()` fires, calling
`tv_notify_frame_done()` → the app can reuse the frame buffer.

**The frame buffer is locked until the NIC transmits the LAST packet of the frame.**

The last packet has `launch_time = epoch + 8.0 ms`. After the NIC transmits it, DPDK must
clean up the descriptor (during a subsequent `rte_eth_tx_burst` call via
`ice_xmit_cleanup`), which frees the mbuf chain and decrements the extbuf refcnt.

Timeline for a single frame buffer cycle:

```
t = 0.0 ms : Builder starts frame N, stamps first pkt at epoch_N + tr_offset
t = 0.5 ms : Builder finishes all 16,458 pkts (enqueued in SW ring)
t = 0.5 ms : Builder starts frame N+1 in second buffer
t = 1.0 ms : Builder finishes frame N+1 → BOTH BUFFERS LOCKED
t = 1.0–8.3 ms : Builder IDLE, waiting for a frame buffer to become free
             Transmitter pushing frame N pkts through NIC ring
t = 8.0 ms : NIC transmits last pkt of frame N (launch_time = epoch_N + 8ms)
t = 8.0+ ms: DPDK cleanup → extbuf refcnt→0 → tv_frame_free_cb → frame N available
t = 8.3 ms : Builder can start frame N+2 in recycled buffer
```

**Ideal throughput with 2 buffers = 1 / 8.3 ms ≈ 120 fps** — but only if the transmitter
adds zero overhead. In practice:

```
Actual frame cycle = 8.33 ms + drift_avg = 8.33 + 2.68 = 11.01 ms
Actual FPS = 1 / 11.01 ms ≈ 90.8 fps  ← matches observation
```

The +2.68 ms drift is the **cumulative overhead** from:
1. Inflight retry cycles (transmitter spinning on full ring)
2. Scheduler contention (builder/transmitter/PTP sharing one lcore)
3. DPDK descriptor cleanup latency (RS bit batching)
4. App callback latency (get_next_frame, notify_frame_done)

### 4.4 Why 90 fps and Not Some Other Value?

The system reaches a **stable equilibrium** at ~90 fps:

```
At 90 fps:
  Inter-frame interval = 11.1 ms
  NIC pacing span = 8.0 ms (fixed by trs=486ns)
  Overhead budget = 11.1 - 8.0 = 3.1 ms per frame

Overhead breakdown:
  Builder idle (waiting for buffer): ~7 ms (t=1ms to t=8ms)
  But this overlaps with NIC draining, so effective overhead is only
  the gap between "NIC finishes frame N" and "builder gets buffer back":
    cleanup latency + app callback ≈ 0.3–0.5 ms
  Plus inflight retry overhead slowing down the transmitter ≈ 2.0–2.5 ms

Total overhead: ~2.5–3.0 ms → matches drift_avg = 2.68 ms
```

If the system tried to run faster (fewer epoch drops), the transmitter would fall further
behind (more packets in queue, more retries), increasing overhead until it settles back at
~90 fps. If slower, less backpressure → transmitter catches up → speeds back up to ~90 fps.

### 4.5 Launch Times Falling Into the Past

The transmitter dequeues packets whose launch_time is **1.1 ms in the past** relative to
the current PTP clock:

```
From log (steady state):
  delta_ptp_ns = -1,128,192 ns (packets are 1.13ms late when submitted to NIC)
  ring_count = 16,448 (SW ring nearly full — builder far ahead of transmitter)
```

The builder stamps packets with future launch_times and enqueues them. By the time the
transmitter fetches them from the SW ring (after the ring fills and the NIC gradually
drains), the launch_time has already passed. The E830 NIC handles past TXTIME packets by
either transmitting immediately or treating them with a tolerance window. This doesn't
break the packet ordering (they're submitted in order), but it means the NIC is running
in "catch-up" mode rather than precise scheduling.

## 5. What Was Tried (Optimization Phases)

### Phase 1: Increase Bulk Size (ST_SESSION_MAX_BULK 4 → 32)

**Change:** `lib/src/st2110/st_header.h` line 35: `ST_SESSION_MAX_BULK` from 4 to 32.
Added TSN-specific override in `st_tx_video_session.c` to set `s->bulk = 32` for TSN
sessions.

**Rationale:** With bulk=4, the transmitter dequeues only 4 packets per scheduler loop.
When the NIC ring has space, submitting 4 at a time is inefficient — it takes 4×more
scheduler loops to fill the ring.

**Result:** FPS improved from ~80 to ~90. `inflight_peak` rose from 3 to 31, confirming
the transmitter now attempts larger batches. But the NIC ring still saturates, so most
of the 32-pkt batch is saved as inflight.

**Verdict:** ✅ Meaningful improvement (+10 fps), kept.

### Phase 2: Increase NIC TX Descriptor Ring (MT_DEV_TX_DESC 2048 → 4096)

**Change:** `lib/src/dev/mt_dev.h` line 12: `MT_DEV_TX_DESC` from 2048 to 4096.

**Rationale:** With ring=2048 and trs=486ns, the ring held only ~1ms of future packets
(12.5% of frame). Doubling to 4096 gives ~2ms (25% of frame), reducing the frequency of
ring-full conditions and inflight retries.

**Result:** Combined with Phase 1, this enabled the bulk=32 to be effective. Without
the larger ring, bulk=32 would just create larger inflight bursts with no throughput gain.

**Verdict:** ✅ Necessary foundation for Phase 1, kept.

### Phase 3: Inner Drain Loop in Transmitter Tasklet (128-iteration budget)

**Change:** `lib/src/st2110/st_video_transmitter.c` ~line 998: Added a `while` loop after
the initial burst that continues dequeuing-stamping-bursting without returning to the
scheduler, up to 128 iterations. Breaks when NIC ring full (partial burst), SW ring empty,
or budget exhausted.

**Rationale:** Returning to the scheduler after every 32-pkt burst adds ~190ns of overhead
(running other tasklets) before the transmitter can try again. An inner loop skips that
overhead.

**Result:** No improvement. The inner loop breaks on the very first iteration every time
because the NIC ring is already full from the previous burst. The bottleneck is HW drain
rate, not software iteration speed.

```
Metrics with Phase 3 (same as Phase 1+2 alone):
  FPS: 90          inflight: 41M/10s
  partial_save: 13M/10s   inflight_peak: 31/0
```

**Verdict:** ❌ No effect, should be reverted (adds code complexity with zero benefit).

### Prior Baseline (default code)

With original `ST_SESSION_MAX_BULK=4` and `MT_DEV_TX_DESC=2048`:
- FPS: ~78–84 at 4K p120
- Severely NIC-ring-limited (ring holds 1ms, 12.5% of frame)

## 6. Hypotheses and Tests

### H1: Increase TX Descriptor Ring to E830 Maximum (8096)

**Hypothesis:** Doubling from 4096 to 8096 increases the NIC ring buffer from 2.0ms to
3.9ms (47% of frame span). This reduces inflight retry frequency by nearly 2×, cutting the
overhead that causes the +2.68ms drift.

```
Expected at 8096 desc:
  Ring coverage = 8096 / 16458 = 49.2% of frame
  Remaining tail = 8362 pkts × 486 ns = 4.06 ms drain time
  (vs current: 12,362 pkts × 486 = 6.01 ms drain time)
  Fewer retries → lower drift → higher FPS
```

**Predicted outcome:** FPS could reach 100–110. Won't hit 120 unless combined with H2.

**How to test:**
```c
// lib/src/dev/mt_dev.h
#define MT_DEV_TX_DESC (8096)
```
Rebuild, run 40s 4K p120 test, compare FPS & inflight metrics.

**Risk:** E830 driver limits ring to 8096. With `ICE_ALIGN_RING_DESC=32`:
8096 is 8096/32 = 253 which is not power-of-2. DPDK may round down. Check
`rte_eth_dev_info_get` for actual `nb_max` after device init.

### H2: Increase Frame Buffer Count (framebuff_cnt = 3 or 4)

**Hypothesis:** With 2 frame buffers, the builder is **ALWAYS** blocked waiting for a
frame buffer (886 busy events per 886 frames = 100% hit rate). Adding a 3rd buffer allows
the builder to keep producing frames even while the NIC is still draining the oldest one.

With 3 buffers:
```
t = 0.0 ms : Builder starts frame 0 in buffer A
t = 0.5 ms : Builder starts frame 1 in buffer B
t = 1.0 ms : Builder starts frame 2 in buffer C ← NEW: no longer blocked!
t = 1.5 ms : Builder waits for buffer A (still in NIC ring)
t = 8.0 ms : NIC finishes frame 0 → buffer A free
t = 8.0 ms : Builder immediately starts frame 3 in buffer A
```

The builder idle window shrinks from 7ms (with 2 buffers: t=1ms to t=8ms) to ~6.5ms
(with 3 buffers: t=1.5ms to t=8ms). More importantly, the **SW ring stays fuller**,
meaning the transmitter never hits `deq_empty` (currently 17–24K/10s).

**Predicted outcome:** Marginal improvement (5–10 fps) because the bottleneck is still the
NIC ring drain rate. But combined with H1, could help.

**How to test:**
```c
// tests/tools/RxTxApp/src/tx_st20p_app.c:307
ops.framebuff_cnt = 3; // or 4
```
Rebuild RxTxApp, run test, compare `busy` count and FPS.

### H3: Multiple TX Queues Per Session

**Hypothesis:** A single TX queue has one descriptor ring. With 4096 descriptors draining
at 486ns/pkt, the max throughput through that queue is ~2 Mpps. Using 2 TX queues doubles
the effective ring depth to 8192 and the max throughput to ~4 Mpps. Each queue handles
alternating packets (even/odd indices) or frame segments.

**Challenge:** ST2110 requires strict packet ordering. Two NIC queues would need careful
TXTIME coordination to ensure packets arrive at the receiver in order. If both queues drain
at the same rate, interleaving should preserve order.

**Predicted outcome:** Could reach 120 fps by eliminating ring-depth bottleneck entirely.
But requires significant MTL architecture changes (transmitter must manage multiple queues).

**How to test:** Requires code changes in `tv_init_hw()` to allocate 2 queues, and in
`video_trs_launch_time_tasklet()` to alternate burst calls between queues. Complex — defer
until H1+H2 results are known.

### H4: Decouple Frame Buffer From NIC Lifecycle (tx_no_chain mode)

**Hypothesis:** In chain mbuf mode, the frame buffer is locked until the NIC frees the last
mbuf (8ms+). In no-chain mode (`tx_no_chain=true`), packet data is copied into the mbuf
data room during building. The frame buffer is freed immediately after building, with
`tv_frame_free_cb` called at line 2427 of `st_tx_video_session.c`. This eliminates the
8ms frame-lock window entirely.

**Trade-off:** Each packet requires a ~1260-byte memcpy from frame data into the mbuf's
data room. At 16,458 packets: 16,458 × 1260 = 20.7 MB of copies per frame = 2.5 GB/s at
120 fps. This is well within DDR4/5 bandwidth (~50 GB/s), but adds CPU load to the
builder lcore.

With no-chain mode, frame buffers recycle in ~500µs (build time) instead of ~8ms
(NIC drain time). The builder never blocks on buffer availability.

**Predicted outcome:** Potentially close to 120 fps if the CPU can handle the memcpy
overhead. The NIC ring limit still applies, but the builder can keep the SW ring full
at all times, which helps the transmitter maintain maximum throughput.

**How to test:**
```
// Add to MTL init params or per-session flag:
// Force no-chain mode via: --tx_no_chain or ST20_TX_FLAG_DISABLE_CHAIN
// Or manually set: s->tx_no_chain = true in tv_attach()
```
Or set `mt_user_tx_no_chain(impl)` to return true. Rebuild, run test. Watch for increased
CPU usage in the builder but better FPS.

### H5: Combination — H1 + H2 (Most Promising)

**Hypothesis:** TX desc=8096 eliminates most inflight retry overhead. 3 frame buffers
eliminate builder stalls. Together, they remove the two main contributors to the +2.68ms
drift.

**Predicted math:**
```
NIC ring at 8096: ~3.9ms buffer (49% of frame)
Remaining tail: 8362 pkts, drain time 4.06ms
With 3 buffers: builder never blocked on buffer availability
Reduced inflight retries: ~50% fewer (ring holds 2× more)
Estimated drift reduction: 2.68ms → ~1.0ms
Estimated frame cycle: 8.33 + 1.0 = 9.3ms → ~107 fps
```

Still short of 120 fps. Full coverage would need the ring to hold an entire frame (16,458
descriptors), which exceeds the E830 maximum (8096).

**How to test:** Apply both changes simultaneously. This is the recommended next step.

### H6: Separate Builder and Transmitter onto Different Lcores

**Hypothesis:** Builder and transmitter currently share lcore 28 (with 3 tasklets total,
avg loop 190ns). Giving the transmitter its own lcore would let it poll for ring space
without sharing CPU with the builder.

**Counter-evidence:** The 4×FHD multi-core test (sch_session_quota=1) already placed
sessions on separate lcores. Sessions with ZERO epoch drops still only achieved ~96 fps.
This proves that scheduler contention is not the primary bottleneck.

**Predicted outcome:** Minimal improvement (<5 fps). The bottleneck is NIC ring depth,
not CPU.

**How to test:** Use `--sch_session_quota 1` in the RxTxApp command.

### H7: Add Explicit Ring Drain Instrumentation

**Hypothesis:** The +2.68ms drift has an unclear breakdown. We need precise per-frame
timing to identify which phase adds the most overhead: ring fill? ring drain? cleanup?
app callback?

**How to test:** Add timestamps at key points in the frame lifecycle:
1. `t_build_start`: when builder starts frame (in `tv_sync_pacing`)
2. `t_build_done`: when builder finishes (last pkt enqueued)
3. `t_last_pkt_pushed`: when transmitter pushes last pkt of frame to NIC
4. `t_frame_freed`: when `tv_frame_free_cb` fires
5. `t_next_frame_start`: when builder starts the next frame

Report the deltas: build_time, drain_time, cleanup_time, wait_time per frame. This
identifies exactly where the 2.68ms drift accumulates.

## 7. Architectural Constraint Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Frame Lifecycle (chain mode)                    │
│                                                                     │
│  Builder                SW Ring              Transmitter        NIC │
│  ┌──────┐           ┌──────────┐          ┌───────────┐    ┌──────┐│
│  │Build │ enqueue → │ 65536    │ dequeue →│ stamp LT  │ →  │ 4096 ││
│  │16458 │           │ entries  │          │ tx_burst  │    │ desc ││
│  │pkts  │           │          │          │           │    │      ││
│  │~500µs│           │peak=22K  │          │inflight   │    │drain ││
│  │ CPU  │           │          │          │41M retry/s│    │@486ns││
│  └──┬───┘           └──────────┘          └─────┬─────┘    └──┬───┘│
│     │                                           │              │    │
│     └─ frame buffer locked ─────────────────────┼── 8.0 ms ───┘    │
│                                                  │                   │
│     frame_free_cb fires when NIC frees last mbuf ┘                  │
│                                                                     │
│  With 2 buffers: builder idle 7.0ms/frame = 84% of time            │
│  With 3 buffers: builder idle 6.5ms/frame = 78% still idle         │
│  Real fix: decouple buffer lifecycle from NIC drain (H4)            │
│  or increase ring to cover full frame span (need >16458 desc)       │
└─────────────────────────────────────────────────────────────────────┘
```

## 8. The Fundamental Physical Limit

At trs = 486 ns and 16,458 pkts/frame, the NIC needs **exactly 8.0 ms** to pace one
frame's packets over the wire. This is the irreducible floor — no software optimization
can make the NIC send packets faster than the configured pacing rate.

With 2 frame buffers and chain mbufs, the frame cycle is:
```
frame_cycle = max(build_time, nic_drain_time + cleanup) + software_overhead
            = max(0.5ms, 8.0ms + 0.3ms) + 2.68ms
            = 8.3ms + 2.68ms
            = 11.0 ms  → 90 fps
```

To reach 120 fps (8.33 ms cycle), the software overhead must be reduced to:
```
max_overhead = 8.33ms - 8.0ms - 0.3ms = 0.03ms = 30µs
```

This is extremely tight. In practice, reaching 120 fps with TSN pacing on a single TX
queue requires:
1. **Near-zero frame buffer wait** (3+ buffers, or no-chain mode, or copy mode)
2. **Near-zero inflight retries** (ring depth ≫ frame size, or multiple queues)
3. **Minimal scheduler contention** (dedicated lcore for transmitter)

The most pragmatic path: **H1 (8096 desc) + H4 (no-chain mode)**, which attacks both
the ring depth and the frame buffer recycling bottleneck simultaneously.

## 9. Recommended Next Steps (Priority Order)

1. **H1: MT_DEV_TX_DESC = 8096** — smallest change, biggest expected impact
2. **H2: framebuff_cnt = 3** — trivial change, test alongside H1
3. **H4: tx_no_chain mode** — eliminates frame recycling bottleneck entirely
4. **H7: per-frame instrumentation** — if H1+H2 don't reach target, instrument to find remaining overhead
5. **H3: multi-queue** — last resort, significant code change
6. **Revert Phase 3** — inner drain loop adds complexity with zero benefit

## 10. Key Code Locations

| File | Line | What |
|------|------|------|
| `lib/src/dev/mt_dev.h` | 12 | `MT_DEV_TX_DESC` — NIC TX ring depth |
| `lib/src/st2110/st_header.h` | 35 | `ST_SESSION_MAX_BULK` — dequeue/burst batch size |
| `lib/src/st2110/st_tx_video_session.c` | 3685 | TSN ring sizing & bulk override |
| `lib/src/st2110/st_tx_video_session.c` | 2078 | Builder tasklet frame (`tv_tasklet_frame`) |
| `lib/src/st2110/st_tx_video_session.c` | 700–736 | Epoch drop logic (`calc_frame_count_since_epoch`) |
| `lib/src/st2110/st_tx_video_session.c` | 126 | `tv_frame_free_cb` — chain mbuf extbuf callback |
| `lib/src/st2110/st_video_transmitter.c` | 860 | `video_trs_launch_time_tasklet` — transmitter |
| `lib/src/st2110/st_video_transmitter.c` | 84 | `video_trs_tsn_mark_launch_time` — LT stamping |
| `tests/tools/RxTxApp/src/tx_st20p_app.c` | 307 | `framebuff_cnt = 2` — app frame buffer count |
| DPDK `drivers/net/intel/ice/ice_rxtx.h` | 26 | `ICE_MAX_RING_DESC_E830 = 8096` |
| DPDK `drivers/net/intel/ice/ice_ethdev.h` | 81 | `ICE_DEFAULT_TX_FREE_THRESH = 32` |
| DPDK `drivers/net/intel/ice/ice_ethdev.h` | 85 | `ICE_DEFAULT_TX_RSBIT_THRESH = 32` |
