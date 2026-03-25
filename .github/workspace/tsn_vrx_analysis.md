# TSN ST 2110-21 Compliance — Compressed Analysis

## Current State: czwartek22-pending — Monotonic PTP cursor implemented

### czwartek21 results (epoch drop suppression — CATASTROPHIC FAILURE)
**Epoch drop suppression does NOT work.** Builder lag grows unboundedly.
```
epoch drop 4086 (suppressed for TSN)
time_to_tx min -507236us max -440855us
drift ptp_avg +223449ns tsc_avg +223423ns per frame (n=297)
ttx_bands future=0 borderline=0 past=297
lt_pkts future=0(0.0%) past=1226306(100.0%)
frame_overhead avg=34us max=118us (notify_max=114us getframe_max=0us sync_max=3us) n=298
fps 29.799950 frames 298
```

**Why it failed**: Recovery assumed TSC gate bypass would make frames faster. But:
- On-wire frame cycle = 32ms active + 1.3ms tr_offset gap = 33.3ms = exactly frame_time
- Wire utilization is 100% — there is ZERO slack for recovery
- TSC gate bypass doesn’t make the NIC send faster (NIC is the bottleneck)
- Each frame still takes frame_time + 109µs regardless of time_to_tx
- Without epoch drops to snap forward, drift accumulates unboundedly
- After ~10s: builder 457ms (13.7 frames) behind, ALL packets past-LT
- **Identical failure mode to czwartek7** (builder fell 500ms behind)

**Key insight**: Epoch drops ARE the recovery mechanism. They are NECESSARY.
But epoch drops cause wire-time gaps that destroy VRX. Solution: decouple.

**Fix**: Monotonic PTP cursor (see below). Epoch drops happen normally (builder recovery),
but ptp_time_cursor advances by exactly frame_time each frame, independent of epoch.
No wire-time gaps, no NIC mode transitions, builder recovers via epoch drop.

### czwartek20 results (overhead instrumentation + LT bias, default nb_tx_desc=512)
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| CINST | 0:71.8%, 1:10%, 2:9.3%, 3:7.7%, tail 4-77 | narrow≤4 | **FAIL** |
| VRX | -658(64.5%), [4-8]~35% | [0,8] | **FAIL** |
| pkt_ts_vs_rtp_ts | -33.3ms to -28.2ms | <1ms | **FAIL** |
| inter_frame_rtp_ts | 3000 exact | 3000 | **PASS** |
| TRO | avg=4.6ms, range 0-6.4ms | ~1.27ms | shifted |

**Deep pcap analysis (30 frames):**
- Frames 0-10: TRO≈0µs, P50=1µs, inter-frame=1356µs — **NARROW mode** (future LTs)
- Frame 11: inter-frame gap=6511µs → **epoch drop transition**
- Frames 11-29: TRO=5155µs, P50=8µs, inter-frame=1341µs — **per-descriptor mode** (past LTs)
- Frame 4: one 616µs burst at pkt 1724 (52-pkt burst — NIC scheduling hiccup)
- **Identical pattern to czwartek17**: epoch drop → NIC mode transition → compliance destroyed

**Overhead instrumentation results (FIRST MEASUREMENT):**
```
frame_overhead avg=34µs max=118µs (notify_max=114µs getframe_max=5µs sync_max=14µs) n=299
drift ptp_avg +109295ns tsc_avg +109265ns per frame (n=298)
ttx_bands future=147 borderline=0 past=151
lt_pkts future=1103160(89.7%) past=127317(10.3%)
```

**Overhead breakdown:**
- **Inter-frame overhead** (frame_done → sync_pacing_done): avg=34µs, max=118µs
  - `tv_notify_frame_done` pipeline callback: **max 114µs** (mutex lock/unlock + app callback + condvar signal)
  - `get_next_frame` pipeline callback: max 5µs (mutex + O(n) scan of framebuffs)
  - `tv_sync_pacing` (PTP MMIO read + epoch calc): max 14µs
- **Intra-frame overhead**: ~75µs (scheduler loop 79ns avg × 1029 bulks ≈ 81µs)
- **Total drift**: 34µs inter-frame + 75µs intra-frame ≈ 109µs/frame
- **Builder rate limiting**: Builder → SW ring (always full ~8192) → Transmitter TSC gate → NIC ring → NIC wire rate

**Key log lines:**
```
epoch drop 1 (at frame ~303 = 10.1s into stream)
time_to_tx min -29589us max 5334us
fps 29.900000 (should be 30.000000 — lost 1 frame to epoch drop in 10s stat window)
```

**Root cause confirmed**: 109µs/frame drift → epoch drop every ~303 frames → NIC mode transition → compliance destroyed.
**Fix implemented**: Epoch drop suppression for TSN (see below).

### czwartek19 results (RTP timestamp fix + LT bias)
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| CINST | 0:71.8%, 1:10%, 2:9.3%, 3:7.7%, tail 4-68 | narrow≤4 | **FAIL** |
| VRX | -3606 at epoch drop, stable [5-8] elsewhere | [0,8] | **FAIL** |
| pkt_ts_vs_rtp_ts | avg -29.95ms | <1ms | **FAIL** |
| inter_frame_rtp_ts | 3000 exact | 3000 | **PASS** |
| TRO | avg=4.6ms | ~1.27ms | shifted |

**Deep pcap analysis:**
- Frame 2→3: 7974µs inter-frame gap = epoch drop
- After epoch drop: stable TRO=6633µs, CINST=NARROW within each mode
- Same epoch-drop-induced mode transition pattern as czwartek17/20

**RTP timestamp analysis**: The non-epoch path uses `ptp_time_cursor` (already includes `lt_bias_ns`).
The epoch-path fix added in czwartek19 may be **double-counting** for the default code path
(RxTxApp does NOT pass `--tx_ts_epoch`, so the non-epoch path is taken).
**Hypothesis**: pkt_ts = -29.95ms ≈ -(frame_time - TRO) → the RTP timestamp is still wrong.
This needs revisiting AFTER epoch drops are eliminated (can't diagnose pkt_ts with mode transitions).

### czwartek18 results (LT bias = frame_time, default nb_tx_desc=512)
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| CINST | 0:98.8%, 1:1.2% | narrow≤4 | **NARROW** |
| VRX | -1991(97.9%), -1990(2.1%) | [0,8] | **FAIL** (RTP misaligned) |
| pkt_ts_vs_rtp_ts | -17.809ms constant | <1ms | **FAIL** (RTP misaligned) |
| inter_frame_rtp_ts | 3000 exact | 3000 | **PASS** |
| TRO | avg=16.758ms, jitter ±0.4µs | ~1.27ms | shifted by bias |

**Deep pcap analysis (30 frames, ALL complete):**
- **0 bad frames** (TRO deviation > 100µs: ZERO)
- **P50 inter-pkt gap = 8µs** (per-descriptor NIC pacing at exact TRS rate)
- **P99 = 8-9µs, max = 9µs** — no stalls, no bursts, ZERO outliers
- **Inter-frame gap = 1340-1342µs** (compare czwartek17: 1355-7839µs)
- **Duration = 31.992ms constant** across all frames
- **NIC stays in single mode throughout** — no future→past transition

**Root cause of VRX/pkt_ts failure**: LT bias shifts on-wire time by +frame_time,
but epoch-based RTP timestamp (`ST20_TX_FLAG_RTP_TIMESTAMP_EPOCH`) was NOT biased.
Analyzer sees RTP frame N arriving at frame N+1's wire position → -17.8ms pkt_ts.

**Fix applied**: Add `lt_bias_ns` to `tai_for_rtp_ts` in the epoch-based path of
`tv_update_rtp_time_stamp()`. With bias=frame_time, this shifts RTP by +3000
(frame_time_sampling), matching wire timing. Cost: 33.3ms added latency.
**Expected czwartek19**: VRX=[5,6,7,8], CINST=[0,1], pkt_ts≈1.24ms, ALL NARROW.

### czwartek17 deep pcap analysis (BREAKTHROUGH DISCOVERY)
1-second pcap at µs resolution reveals TWO NIC operating modes in one capture:

**Frames 0-23 (good phase — future LaunchTimes):**
- P50 inter-packet gap = 1µs (bulk-4 NIC pacing, 30µs inter-bulk)
- TRO = constant (±0.7µs jitter) → NARROW COMPLIANT
- VRX=[5,6,7,8], CINST=[0,1,2,3]

**Frame 24 (TRANSITION — mode switch):**
- Inter-frame gap = 7839µs (vs normal 1355µs) → 6484µs excess
- TRO shifts by +6484µs → **THIS CAUSES VRX=-829**
- The NIC switches from batch-per-doorbell to per-descriptor mode

**Frames 24-29 (bad phase — past LaunchTimes):**
- P50 gap = 8µs (per-descriptor NIC pacing at TRS rate)
- TRO = +6484µs (constant but shifted)
- VRX=-829 at the transition, then stable

**Root cause**: When accumulated drift pushes LaunchTimes from future to past,
the NIC changes its transmit mode. The MODE TRANSITION creates a ~6.5ms gap that
depletes VRX to -829. Within each mode, pacing is excellent.

### FIX: LaunchTime Forward Bias (decoupled from TSC gate)
**Key insight**: Add a forward bias ONLY to ptp_time_cursor (NIC LaunchTime),
NOT to tsc_time_cursor (builder/transmitter rate). This keeps all LTs in the
future while the builder/transmitter pipeline runs unchanged.

```
Without bias:  tsc_cursor = cur_tsc + time_to_tx     (same)
               ptp_cursor = start_tai                 (drifts from future to past)

With bias:     tsc_cursor = cur_tsc + time_to_tx     (unchanged — builder rate)
               ptp_cursor = start_tai + frame_time    (always future, NIC holds in TX ring)
```

Requirements:
- NIC TX ring holds biased-future packets (512 default is sufficient — NIC drains at TRS rate)
- Bias = frame_time (33.3ms) covers full epoch drift cycle
- RTP timestamp must ALSO be biased (+frame_time_sampling) to maintain pkt_ts compliance

### Best result: czwartek11 (no isol, pacing->vrx -= (bulk-1), distribution [5,6,7,8])
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| pkt_ts_vs_rtp_ts | 5.2–6.7µs | <1000µs | **PASS** |
| inter_frame_rtp_ts | 3000 exact | 3000 | **PASS** |
| CINST | 0:28%, 1:25%, 2:25%, 3:21%, tail 4-9 | wide≤16 | **WIDE** |
| VRX | 5:25%, 6:25%, 7:25%, 8:25%, **-1:0.002%** | ≥0 | **FAIL** (2 pkts) |
| TRO | avg=1239µs, range 1238–1240µs | — | excellent |

### czwartek14-isol (CPU isolation, pacing->vrx -= bulk = 4)
**ALL violations from 1 stall + partial frames.** Complete frames 1-28 are perfect.
- ONE 1256.9µs stall at frame 17 pkt 2356 → VRX=-153, CINST=159, pkt_ts=3.1ms
- Without this stall: identical to czwartek13 ([4,5,6,7], 8 stalls 54-79µs)
- CPU isolation CAUSED the ms-class stall (likely SMI or changed interrupt affinity)
- **REMOVE CPU isolation** — it made things worse

### czwartek13 (no isol, pacing->vrx -= bulk = 4, distribution [4,5,6,7])
- VRX: [4,5,6,7] + negatives -3 to -1 (9 pkts). 6 stalls, max 85µs.
- **Verdict: `bulk-1` [5,6,7,8] is BETTER** — 5 TRS headroom vs 4 → fewer violations

---

## Root Cause: NIC TX Scheduling Stalls (DEFINITIVELY IDENTIFIED)

### Pcap Evidence (czwartek13 deep analysis)
- **6 outlier inter-bulk gaps** in 28 frames: 63.2, 68.9, 70.1, 72.0, 80.1, 84.9µs
- Normal inter-bulk gap: 30.0µs (P50), P99.9 = 33.1µs
- **25 negative VRX packets** from these 6 stall events
- Stalls at **random mid-frame positions** (pkt 984, 1040, 2056, 2116, 3156, 3180)
- **NOT correlated with epoch drops** (no epoch drop in this 1-second capture)
- Each stall = ~2× normal gap (NIC delays one descriptor batch by ~30-55µs extra)
- After stall: 8+ packets arrive in rapid succession (NIC catches up)

### Mechanism
```
Normal:  [bulk A, 30µs gap, bulk B, 30µs gap, bulk C, ...]  VRX=[5,6,7,8]
Stall:   [bulk A, 85µs gap, bulk B+C rapid, ...]             VRX drops by 7
```
- 85µs gap = 10.9 TRS. Normal 30µs = 3.9 TRS. Extra drain = 7 TRS.
- From steady-state VRX=5, drops to VRX=-2.

### VRX Compensation Analysis
| Compensation | Distribution | Bottom headroom | Threshold gap | czwartek13 violations |
|---|---|---|---|---|
| `bulk-1` = 3 | [5,6,7,8] | 5 TRS (39µs) | 69µs | **2 events** (80, 85µs) |
| `bulk` = 4 | [4,5,6,7] | 4 TRS (31µs) | 61µs | **6 events** (all) |

**Conclusion: bulk-1 gives 8µs more jitter tolerance. Keeps top at VRX=8 (wide limit, empirically safe).**

### Why this is a hardware limitation
- VRX span with NIC jitter = 11 TRS ([−2, 8] with bulk-1, [−3, 7] with bulk)
- Wide window = 9 TRS ([0, 8])
- 11 > 9 → no VRX compensation can fully eliminate violations with these outlier gaps
- Need: either eliminate NIC stalls (reduce 85µs → <69µs) or accept ~0.002% failure

---

## Hardware & Config

- E830 NIC, DPDK 25.11, 1080p30 (4115 pkts/frame)
- Bresenham pacing: base=60 ticks (7680ns), extra=3100/4115 → exact 32.000ms
- TX ring: 512 descriptors, bulk=4, frame_time=33.333ms, tr_offset=1.274ms
- pcapng_dump = **RX-side HW PTP timestamps** (not TX)

## Key Invariants (Hard-Won)

1. **E830 dual-mode**: Future LTs → batch per doorbell (CINST wide). Past LTs → per-descriptor (CINST narrow)
2. **Future LTs → NIC holds → sends at LT → constant TRO → pkt_ts PASS**
3. **Past LTs → NIC sends immediately → TRO = builder timing → pkt_ts FAIL if stalls**
4. **109µs/frame processing overhead** (PTP=TSC within 30ns, real wall-clock)
5. **Overhead breakdown**: 34µs inter-frame (notify=114µs max, getframe=5µs, sync=14µs) + 75µs intra-frame (scheduler loop × 1029 bulks)
6. **Epoch drop cycle**: 33333µs/109µs ≈ 306 frames ≈ 10.2s → 1 drop/10s. Epoch drops REQUIRED for builder recovery.
7. **Epoch drop suppression DOES NOT WORK**: no recovery mechanism, builder falls behind unboundedly (czwartek21)
8. **TX ring 512 desc is SUFFICIENT for LT bias** — NIC drains at TRS rate, ring stays at ~512-level backpressure
9. **Bulk=4 → VRX distribution spans 4 values** [vrx, vrx+1, vrx+2, vrx+3]
10. **NIC TX stalls: ~6/s, 63-85µs, random positions** — hardware jitter, not software
11. **Wire utilization = 100% of frame_time**: 32ms active + 1.3ms gap = 33.3ms. NO slack for catch-up via faster building.
12. **notify_frame_done is the dominant inter-frame overhead source** (114µs max): 2 mutex ops + app callback + condvar signal in pipeline layer
13. **Monotonic PTP cursor**: ptp_time_cursor advances by exact frame_time (Bresenham integer arithmetic) independent of epoch drops. Only for TSN.

## Failed Approaches (Do NOT Retry)

| Fix | Capture | What Happened |
|-----|---------|---------------|
| TSC compensation | czwartek6 | NIC is rate limiter, no effect |
| Epoch suppression + wall-clock RTP | czwartek7 | Builder fell 500ms behind |
| Past LT bias (-frame_time) | czwartek8 | NIC sent immediately |
| bulk=1 alone | czwartek9 | 4x CPU, stall-catchup bursts |
| bulk=1 + forward LT bias (+frame_time) | czwartek10 | TX ring overflow |
| **pacing->vrx -= bulk** (extra compensation) | **czwartek13** | **Worse: 9 neg vs 2 with bulk-1** |
| **CPU isolation (isolcpus)** | **czwartek14** | **Introduced 1.26ms stall, strictly worse** |
| **Inter-burst gap + ring-full instrumentation** | **czwartek16** | **Captured at bad drift phase; misleading 250ms "stall" reading; REVERTED** |
| **Epoch drop suppression (no snap-forward for TSN)** | **czwartek21** | **Builder fell 457ms behind in 10s. No recovery mechanism. Wire=100% utilized, zero slack. Identical to czwartek7 failure.** |

## Scorecard

| Metric | czwartek6 | czwartek11 | czwartek17 | czwartek18 | czwartek19 | czwartek20 | czwartek21 | Target |
|--------|-----------|------------|------------|------------|------------|------------|------------|--------|
| CINST | wide | wide | fail (68) | **NARROW** | fail (68) | fail (77) | fail(allpast) | narrow≤4 |
| VRX | wide | fail(-1,2) | fail(-829) | fail(-1991)* | fail(-3606) | fail(-658) | fail(allpast) | [0,8] |
| pkt_ts | pass | pass | fail(6.5ms) | fail(-17.8ms)* | fail(-30ms) | fail(-33ms) | fail(allpast) | <1ms |
| rtp_ts | pass | pass | pass | pass | pass | pass | pass | 3000 |
| TRO | 1.24ms | 1.24ms | 1.2/7.7ms | 16.76ms* | 4.6ms | 0/5.2ms | N/A | ~1.27ms |
| Overall | **wide** | **almost** | **diagnostic** | **CINST nar!** | epoch drop | epoch drop | **unbounded** | narrow |

*czwartek18 VRX/pkt_ts failures are purely RTP timestamp misalignment, not wire-level.
On-wire pacing is PERFECT: 0 bad frames, 0 stalls, ±0.7µs TRO jitter.

**Pattern**: czwartek17/19/20 all show epoch drop → NIC mode transition → compliance destroyed.
czwartek21 shows epoch drop suppression → unbounded drift → even worse.
**Solution**: Monotonic PTP cursor (allow epoch drops + smooth wire timing).

---

## Monotonic PTP Cursor (NEW — czwartek21 fix, replaces failed epoch drop suppression)

### Problem
Epoch drops are REQUIRED for builder recovery (wire=100% utilized, no slack).
But epoch drops cause `ptp_time_cursor` to jump by frame_time → wire-time gap → VRX spike → NIC mode transition.
Epoch drop suppression DOES NOT WORK (czwartek21: unbounded drift, czwartek7: same).

### Solution: Decouple PTP cursor from epoch
Track `ptp_time_cursor` as a monotonic counter that advances by exactly `frame_time`
each frame, regardless of epoch drops:

```
Epoch drop:
  cur_epochs:       N → N+2  (snap forward → builder recovery)
  tsc_time_cursor:  resets to ~cur_tsc  (builder resumes normal rate)
  ptp_time_cursor:  base += frame_time  (smooth, no gap → NIC stays in batch mode)
```

Compare epoch-based cursor (broke VRX):
```
  ptp_time_cursor = start_time_tai(N+2) + lt_bias
                  = start_time_tai(N) + 2*frame_time + lt_bias  → +frame_time gap on wire
```

### Implementation
- New struct fields in `st_tx_video_pacing`:
  - `tsn_ptp_cursor_base` (uint64_t): monotonic base, NOT aligned (alignment only on copy)
  - `tsn_ptp_frame_time_int/extra/denom`: Bresenham integer frame_time = `(NS_PER_S * den) / mul`
  - `tsn_ptp_frame_accum`: Bresenham accumulator
  - `tsn_ptp_cursor_init` (bool): seeded from first frame’s start_time_tai + lt_bias_ns
- In `tv_sync_pacing()` for TSN:
  - First frame: `base = start_time_tai + lt_bias_ns`
  - Subsequent: `base += frame_time_int + (accum overflow ? 1 : 0)` (exact Bresenham)
  - `ptp_time_cursor = ALIGN_128(base)` (alignment only on the copy, not base)
- Epoch drops happen normally (builder catch-up works)
- Non-epoch RTP path derives from `ptp_time_cursor` → automatically smooth/sequential
- Epoch-path RTP bias from czwartek19 **REVERTED** (was only needed for suppression approach)

### Precision
For 30fps: `frame_time = 33333333 + 10/30 ns`. Bresenham: int=33333333, extra=10, denom=30.
After 30 frames: 30×33333333 + 10 = 1,000,000,000 ns. **Exact.**
128ns alignment: rounds UP by 0-127ns per frame. Average 64ns/frame. Over 306 frames: ~19.6µs.
Negligible (< 3 TRS units).

### Expected czwartek22 behavior
```
Frames 0-306:  drift accumulates +109µs/frame
               ptp_cursor advances monotonically (+frame_time each)
               All LTs future (base starts at start_tai + frame_time)
               CINST=[0,1,2,3] NARROW, VRX=[5,6,7,8]

Frame ~306:    EPOCH DROP (normal, logged)
               cur_epochs jumps +1, tsc_time_cursor resets to ~cur_tsc
               ptp_cursor: base += frame_time (NO JUMP, smooth)
               Builder recovers naturally over next few frames
               NIC sees continuous future LTs → NO mode transition

Frames 307+:   drift cycle restarts
               Wire pacing: perfectly smooth, no gaps, no stalls
```

Stat will show: `epoch drop 1 (monotonic PTP cursor)` every ~10s.
All metrics should be stable: CINST narrow, VRX=[5,6,7,8], TRO=const.

---

## Open Hypotheses & Issues

### TESTED & CONFIRMED: Epoch drop is the sole remaining compliance blocker
- **Evidence**: czwartek18 (CINST NARROW, 0 bad frames, 0 stalls) proves that WITH LT bias and
  WITHOUT epoch drops (pcap captured in good phase), the stream is narrow-compliant on-wire.
- **czwartek20 confirms**: frames 0-10 are NARROW, frames 11+ fail ONLY due to epoch drop.

### TESTED & DISPROVEN: Epoch drop suppression as recovery mechanism
- **Hypothesis**: TSC gate bypass recovers 1.3ms/frame when behind schedule
- **Reality** (czwartek21): Wire = 100% utilized. NIC drains at wire rate regardless of TSC gate.
  No mechanism to make frames shorter. Builder drift accumulates unboundedly.
- **Status**: DISPROVEN. Epoch drops are the ONLY recovery mechanism.

### UNTESTED: Monotonic PTP cursor (czwartek22)
- **Hypothesis**: Allow epoch drops (builder catch-up) + monotonic ptp_time_cursor (no wire gaps)
  eliminates both the drift problem AND the VRX spike problem.
- **Key risk**: Does ptp_time_cursor drift relative to PTP wall clock matter?
  After many epoch drops, monotonic cursor diverges from epoch-derived cursor by
  N_drops × frame_time. LT bias covers 1 frame_time of divergence. If drops accumulate faster
  than 1 per frame_time of real time (they don’t — it’s 1/10s), bias stays valid.
- **Status**: Implemented, untested. czwartek22 will prove/disprove.

### UNTESTED: RTP timestamp alignment with monotonic cursor
- Non-epoch path derives RTP from ptp_time_cursor (monotonic, includes lt_bias).
- Expected: RTP advances by frame_time_sampling (3000) each frame, regardless of epoch drops.
- pkt_ts = wire_ptp − rtp_ptp = (cursor + pkt_offset) − cursor ≈ tr_offset ≈ 1.27ms. Should PASS.
- **Risk**: czwartek18 had pkt_ts=-17.8ms (wrong). Need to verify the default path is non-epoch.
- **Status**: Will be measured in czwartek22.

### UNTESTED: NIC TX stalls in sustained future-LT mode
- czwartek11 (no LT bias, wide mode): 6 stalls/s, 63-85µs, VRX=-1 to -3
- czwartek18 (LT bias, short capture): 0 stalls. But only 30 frames.
- **Status**: Will be visible in czwartek22 if we get a clean 30-frame capture.

### UNTESTED: Long-term stability of monotonic cursor
- Over hours: monotonic cursor drifts from wall clock by ~109µs × N_drops × frame_time.
  With 1 drop/10s: after 1 hour = 360 drops × 33ms = 11.9s of divergence.
  But lt_bias only covers 33ms. Does this matter?
- **Key**: lt_bias ensures first-pkt LT = cursor_base (which is close to wall clock at first frame).
  After 360 drops, cursor_base = initial + 360×33ms + 360×306×33.3ms = way ahead of wall clock.
  Wait — cursor_base advances by frame_time per frame. After N frames: base = initial + N×frame_time.
  But wall clock advances by N×(frame_time+109µs). So base LAGS wall clock by N×109µs.
  After 306 frames: lag = 33ms. Epoch drop resets cur_epochs but NOT cursor_base.
  So cursor_base keeps lagging. LTs = cursor_base + lt_bias. When lag > lt_bias (33ms),
  LTs become past. This happens after 306 frames — **same cycle as before!**
  BUT: NIC mode transition doesn’t matter because ptp_cursor is smooth (no gap).
  With past LTs, NIC sends per-descriptor (8µs). This IS the mode czwartek18 ran in (NARROW!).
  **IMPORTANT**: czwartek18 was 8µs-mode throughout and was CINST NARROW.
  So even with past LTs from monotonic cursor, narrowness is maintained as long as no GAPS.
- **Status**: Theory says fine. czwartek22 will confirm.

---

## Next Steps (Priority Order)
1. **Test czwartek22 with monotonic PTP cursor** — expected: CINST narrow throughout,
   VRX=[5,6,7,8] or mode-dependent, NO VRX spikes at epoch drops, smooth TRO,
   stat shows "epoch drop 1 (monotonic PTP cursor)" every ~10s
2. **If CINST/VRX pass but pkt_ts fails**: investigate RTP timestamp derivation from monotonic cursor
3. **If VRX shows spikes at epoch drops**: monotonic cursor NOT fully decoupled → debug
4. **Long-term stability test**: 60s+ run to verify cursor divergence doesn’t cause problems

## Code State
- **VRX compensation**: `pacing->vrx -= (s->bulk - 1)` → distribution [5,6,7,8] — OPTIMAL
- **LaunchTime bias**: `pacing->lt_bias_ns = frame_time` for TSN mode
- **Monotonic PTP cursor**: `tsn_ptp_cursor_base` advances by Bresenham frame_time, independent of epoch
- **Epoch drops**: happen normally (snap forward), provide builder recovery
- **RTP timestamp**: non-epoch path derives from `ptp_time_cursor` (monotonic, includes bias)
- **Epoch-path RTP bias**: REVERTED (was wrong approach from czwartek19)
- **Overhead instrumentation**: frame_overhead avg/max, notify/getframe/sync breakdown — ACTIVE
- **Diagnostic logging**: EPOCH DROP PACING, ttx_bands, lt_pkts, time_to_tx, drift, overhead — ACTIVE