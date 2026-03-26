# TSN ST 2110-21 Compliance — Compressed Analysis

## Current State: czwartek22-pending — Monotonic PTP cursor implemented

### czwartek22 retest (after TSN ring init fix) — ring fix worked, but exposed the REAL bottleneck

**Key log lines:**
```
epoch drop 1
time_to_tx min -32096us max 1196us
drift ptp_avg +223008ns tsc_avg +222993ns per frame (n=297)
ttx_bands future=6 borderline=0 past=291
lt_pkts future=0(0.0%) past=1226322(100.0%)
frame_overhead avg=33303us max=39863us (notify_max=39859us getframe_max=7us sync_max=18us)
tsn_loops build_avg=31.18 build_max=32 tx_avg=5.00 tx_max=32 ring_peak=7740/7740 ring_stop=0 tx_partial=76564
busy as no ready frame from user 298
2 frames are in trans, total 2
```

**What changed vs previous run:**
- `ring_peak=7740/7740` proves the TSN ring-size init bug is fixed. We are no longer stuck on ~512.
- `ring_stop=0` proves the builder is no longer throttled by the SW ring.
- `build_avg=31.18/32` proves the work-conserving builder now runs as intended.

**What did NOT improve:**
- `tx_avg=5.00`, `tx_partial=76564`, `lt_pkts future=0` → the real backpressure is now **downstream of the SW ring**.
- All packets are still transmitted with **past LaunchTimes**.
- `epoch drop 1` remains.
- `drift` got WORSE: ~223µs/frame vs prior ~109µs/frame class.

**Critical new finding:** `frame_overhead avg=33303us`, dominated by `notify_max=39859us`, is NOT scheduler/PTP cost.
This is effectively **one frame period of delay in the frame-done path**.

Interpretation:
- `getframe_max=7us`, `sync_max=18us` remain tiny.
- The entire inter-frame budget is now burned in the path from frame completion to the next frame becoming available.
- `busy as no ready frame from user 298` confirms the builder is usually waiting for the app/pipeline to recycle a frame.
- The TSN ring enlargement allowed much deeper downstream buffering, so frame ownership now returns too late for the pipeline/app cadence.

### New pcap (`mypcap-gpt2`) — one epoch-drop step, but stream is already in past-LT mode

Analyzer summary:
- `CINST`: **NARROW compliant** (`0:98.8%, 1:1.2%`)
- `VRX`: **FAIL** (`-2138:72.1%, -1285:27.9%`)
- `packet_ts_vs_rtp_ts`: **FAIL** (`143.37ms .. 150.00ms`, avg `148.17ms`)
- `TRO`: `11.27ms .. 17.90ms`, avg `16.07ms` (default is `1.274ms`)
- `inter_frame_rtp_ts`: exact `3000`

Deep pcap readout (30 frames):
- Intra-frame packet spacing is stable **6–9µs** with `P50≈7.9µs` for the entire capture.
- So the NIC is already in **per-descriptor / past-LT mode for the whole capture**.
- Frame 8→9 has a single large inter-frame gap: **39.966ms** instead of **33.333ms**.
- Extra gap = **6.632ms**, after which all following frames stay shifted by **+6.632ms**.

That exactly matches the two VRX/TRO plateaus:
- pre-drop plateau: TRO ≈ **11.27ms**, VRX ≈ **-1285**
- post-drop plateau: TRO ≈ **17.90ms**, VRX ≈ **-2138**
- delta: **~6.632ms = ~853 TRS**, and `-1285 - 853 = -2138`

**Important implication:**
- The monotonic PTP cursor avoids wild phase churn, but it does **NOT** keep packets future-LT under the current queue/callback behavior.
- The ring fix did not reveal a wire-rate shortage. It revealed a **frame-lifecycle / wakeup / callback coupling problem** plus downstream TX queue backpressure.

### Revised hypothesis (current best)

The first hard limit is now:
1. Builder fills the large SW ring correctly.
2. Transmitter/NIC remain backpressured (`tx_partial` huge), so descriptors accumulate downstream.
3. Frame buffers are not recycled promptly because frame completion depends on the last packet DMA completion.
4. `tv_notify_frame_done()` / pipeline frame-done callback path becomes frame-period-scale.
5. App/pipeline wakeup for the next frame is therefore delayed by ~1 frame, seen as `notify_max≈39.9ms` and `busy as no ready frame from user 298`.
6. Packets stay past-LT, and an epoch drop still inserts a one-time +6.6ms wire shift.

### Design consequence

The next fix should focus on **frame-lifecycle decoupling**, not bigger rings:
- decouple/block-proof the frame-done callback path
- avoid making next-frame availability wait on potentially blocking app callbacks
- bound downstream queue depth so RTP/wire latency does not balloon into 100ms-class pkt_ts error
- then revisit a future-LT servo only after frame recycling is no longer frame-period-blocked

### Applied fix (2026-03-25, frame lifecycle decoupling — phase 1)

Implemented in:
- [lib/src/st2110/pipeline/st20_pipeline_tx.c](lib/src/st2110/pipeline/st20_pipeline_tx.c)
- [lib/src/st2110/pipeline/st22_pipeline_tx.c](lib/src/st2110/pipeline/st22_pipeline_tx.c)
- [lib/src/st2110/pipeline/st30_pipeline_tx.c](lib/src/st2110/pipeline/st30_pipeline_tx.c)
- [lib/src/st2110/pipeline/st40_pipeline_tx.c](lib/src/st2110/pipeline/st40_pipeline_tx.c)

Change:
- when a TX pipeline frame transitions to `FREE` in `frame_done` / `late_frame_drop`,
  the internal blocking `get_frame()` waiter is now woken **immediately**
- app `notify_frame_done` / `notify_frame_late` still runs afterward
- existing `notify_frame_available` callback path remains in place for compatibility

Purpose:
- break the hidden dependency: `frame reusable` ⇒ wait for app callback return
- for `block_get` apps (RxTxApp path), next frame reuse should no longer be delayed by a slow callback executed on the tasklet context

Expected next-log signals if this is the right fix:
- `frame_overhead avg` drops sharply from ~33ms
- `notify_max` drops sharply from ~40ms
- `busy as no ready frame from user` drops a lot or disappears
- more non-past `ttx_bands`
- some non-zero future `lt_pkts`
- fewer or zero epoch drops

### Result of phase-1 frame lifecycle decoupling

**No observable effect** on RxTxApp TSN run.

Why the hypothesis was wrong:
- RxTxApp TX path uses `ST20P_TX_FLAG_BLOCK_GET` and a dedicated producer thread in
  [tests/tools/RxTxApp/src/tx_st20p_app.c](tests/tools/RxTxApp/src/tx_st20p_app.c#L65-L109)
- it does **not** register `notify_frame_done` / `notify_frame_available` callbacks
- so the earlier callback-decoupling patch is effectively a no-op for this workload

Also, the `frame_overhead` metric was over-interpreted:
- `stat_frame_done_tsc` is captured at the **end of frame build/enqueue**, not at final wire completion
- so `notify_max≈40ms` mostly reflects the app waiting for the next reusable frame with only 2 TX framebuffers, not callback execution time

### Revised root cause after gpt3

- TSN builder path is healthy (`build_avg=31.18`, `ring_stop=0`)
- pcap still shows one epoch-drop-induced **+6.63ms** phase step
- all LaunchTimes are still past (`lt_pkts future=0`)
- RxTxApp has `2 frames are in trans`, and the SW ring peaks at ~1.9 frames

**Current best hypothesis**:
- one-frame LT bias is no longer enough once the work-conserving TSN path can keep almost two frames buffered downstream
- packets spend too long in `SW ring + NIC TXQ`, so they still reach the NIC with past LaunchTimes
- once everything is past-LT, the stream stays in the same per-descriptor mode and epoch drops still cause the +6.63ms step

### Applied fix (2026-03-25, TSN downstream lead phase)

Implemented in [lib/src/st2110/st_tx_video_session.c](lib/src/st2110/st_tx_video_session.c):
- increased TSN `lt_bias_ns` from **1 frame** to **2 frames**
- increased TSN SW ring target from `2 * pkts` to `2 * pkts + 512 margin` (1080p30 -> 16384)
- changed diagnostics to report combined downstream queue budget: `sw_ring + txq`

Purpose:
- keep LaunchTimes future despite nearly two frames already buffered downstream
- give the NIC enough future lead to remain in the intended LaunchTime mode

What to check next:
- startup should show larger TSN bias and larger TSN ring
- `lt_pkts future` should become non-zero, ideally dominant
- `ttx_bands future/borderline` should improve materially
- pcap should stop showing the +6.63ms phase step
- `packet_ts_vs_rtp_ts` and `VRX` should improve if future-LT is restored

### New finding after gpt4

The two-frame LT lead also had **no effect**. This exposed a more basic source-side limit:

- RxTxApp hardcodes `ops.framebuff_cnt = 2` in
  [tests/tools/RxTxApp/src/tx_st20p_app.c](tests/tools/RxTxApp/src/tx_st20p_app.c#L301-L309)
- logs consistently show `2 frames are in trans, total 2`
- builder also reports `busy as no ready frame from user 298`

Interpretation:
- the producer thread has zero source-side slack
- while two frames are in transmit, the app cannot prepare a third
- so the builder waits for frame recycling every frame, and epoch drops accumulate over time
- this also explains why the monotonic PTP cursor keeps falling multiple frames behind wall clock

### Applied fix (2026-03-25, source-side pipeline depth)

Updated RxTxApp ST20 pipeline TX to use **4** framebuffers instead of 2 in
[tests/tools/RxTxApp/src/tx_st20p_app.c](tests/tools/RxTxApp/src/tx_st20p_app.c).

Goal:
- allow the producer to prepare the next frame while up to two frames are still downstream
- eliminate constant `no ready frame from user` stalls
- reduce or eliminate epoch drops before further LT tuning

### Result of source-side depth increase (gpt5)

This change produced a **real improvement**, but also revealed the next precise flaw:

- `ring_peak` grew to ~`15960/15960`
- pcap improved dramatically:
  - `TRO avg ≈ 4.51ms` instead of 28ms/16ms-class failures
  - `VRX` improved to `-151` / `-1004`
  - frames 1..20 are phase-stable within about `±1us`
- but there is still a single `+6.63ms` phase step at frame 21, and then the stream stays shifted

Most important discovery from the log:
- `start_tai 1774457582467901952`
- `ptp_cursor 1774457581101235328`
- difference = **1.366666624s = exactly 41 frame times**

This proves the monotonic TSN PTP cursor was accumulating roughly **one frame of lag per historical epoch drop**.
Pure monotonic stepping avoided large wire-time gaps, but every epoch drop left the PTP/RTP cursor one frame behind wall clock forever.
After enough drops, the cursor inevitably fell back into past-LT territory again.

### Applied fix (2026-03-25, bounded monotonic catch-up servo)

Implemented in [lib/src/st2110/st_tx_video_session.c](lib/src/st2110/st_tx_video_session.c):

- TSN monotonic `ptp_time_cursor` still advances frame-by-frame
- but now each frame compares the monotonic cursor against the current epoch-derived target
  `start_time_tai + lt_bias_ns`
- when the monotonic cursor is behind, it adds a bounded catch-up correction
  (currently capped at `500us` per frame)

Goal:
- preserve smooth TSN wire timing
- prevent epoch drops from accumulating permanent whole-frame PTP/RTP lag
- pull LaunchTime/RTP back toward wall clock gradually instead of by a single 33ms jump

New validation hook:
- epoch-drop log now includes `ptp_lag` in microseconds

What to check next:
- `ptp_lag` should shrink over time instead of staying at multi-frame values
- `time_to_tx` should move toward zero / future
- `lt_pkts future` should become non-zero and increase
- pcap should keep the good early-frame stability while removing the `+6.63ms` step
- `packet_ts_vs_rtp_ts` should fall sharply if RTP/LaunchTime realign to wall clock

### Result of bounded monotonic catch-up servo (gpt6) — FAILED, REMOVED

This solution made the stream **worse** and has been removed from code.

Observed failures:
- `fps` fell to **29.299929**
- `epoch drop` increased to **6** per 10s window
- `drift` exploded to **+727081ns/frame**
- `ptp_lag` did **not** shrink; it grew from `259166us` to `311833us`
- `lt_pkts future` stayed **0**
- `inter_frame_rtp_ts_delta` in pcap became **3045** ticks instead of **3000**

Interpretation:
- the servo changed the monotonic PTP cursor step away from exact frame_time
- RTP timestamps are derived from that cursor in the default path, so RTP cadence became wrong
- this violated a hard invariant: RTP frame-to-frame delta must remain exact even while experimenting with LaunchTime correction

Action taken:
- removed the bounded catch-up servo from `st_tx_video_session.c`
- kept `ptp_lag` only as a **diagnostic**

Workflow note:
- per user instruction, failed fixes should not remain active after validation
- current code no longer contains the tested catch-up servo

### New hypothesis set after gpt6 (subagent-assisted)

I explicitly screened for **new** ideas that are materially different from already tested ones.
Shortlist is recorded in:
- [.github/workspace/tsn_post_gpt6_hypotheses.md](.github/workspace/tsn_post_gpt6_hypotheses.md)

Most promising selected hypothesis:

**LaunchTime and RTP need separate clocks.**

Evidence:
- SW ring is no longer the first bottleneck.
- `tx_partial` remains very high.
- E830 txtime field wraps every ~67.1ms.
- With large SW ring + large TXQ, packets can sit downstream long enough to reach hardware already past LT.

This is distinct from earlier fixes:
- not TSC-gating each bulk
- not builder throttling
- not another pure LT bias change
- not another RTP/epoch workaround

### Applied fix (2026-03-25, TSN NIC submission horizon) — REJECTED AFTER GPT7

Implemented in:
- [lib/src/st2110/st_video_transmitter.c](lib/src/st2110/st_video_transmitter.c)
- [lib/src/st2110/st_header.h](lib/src/st2110/st_header.h)
- [lib/src/st2110/st_tx_video_session.c](lib/src/st2110/st_tx_video_session.c)

Outcome from gpt7:
- `tx_wait_lt=0`, `wait_max=0us`
- `lt_pkts future=0(0.0%) past=1226322(100.0%)`
- hypothesis was effectively a no-op on this workload
- experiment removed from code after validation

Conclusion:
- do not retry the NIC submission horizon idea unless a future workload shows non-zero `tx_wait_lt`
- next distinct fix must preserve exact RTP cadence while allowing LT-only correction

### Applied fix (2026-03-25, separate TSN RTP clock + LT-only correction)

Implemented in:
- [lib/src/st2110/st_header.h](lib/src/st2110/st_header.h)
- [lib/src/st2110/st_tx_video_session.c](lib/src/st2110/st_tx_video_session.c)

Behavior:
- TSN keeps an exact frame-to-frame RTP progression separate from the LaunchTime cursor.
- RTP advances by exact media-clock frame ticks using integer+Bresenham accumulation.
- TSN LaunchTime may now use bounded positive frame-boundary catch-up without perturbing RTP cadence.
- Epoch-path RTP still derives from exact epoch time; only the default TSN non-epoch path changed.

What to check next:
- `inter_frame_rtp_ts_delta` must stay exactly `3000`
- `ptp_lag` should stop growing without reintroducing RTP drift
- `lt_pkts future` should become non-zero if LT-only catch-up helps
- pcap should avoid the late-run `+6.63ms` phase step
- `VRX` / `packet_ts_vs_rtp_ts` should improve materially

### Result of separate TSN RTP clock + LT-only correction (gpt8) — PARTIAL RTP SUCCESS, LT SERVO FAILED

What improved:
- `inter_frame_rtp_ts_delta` stayed exact at `3000`
- so the dedicated TSN RTP clock did its job and should stay

What failed:
- `fps` fell to **29.399976**
- `epoch drop` increased to **7** per 10s window
- `drift` rose to **+726346ns/frame**
- `lt_pkts future` stayed **0%**
- `ptp_lag` remained huge and still grew (`~9.61s -> 9.68s` in the shown logs)
- pcap frame-start cadence became **~33.833ms/frame** after startup, with one later
  **40.466ms** gap causing a permanent extra **+7.13ms** phase step

Interpretation:
- decoupling RTP from LT was the correct architectural step for RTP cadence only
- but the LT-only bounded positive catch-up still changed the LT frame step away from exact
  `frame_time`
- that directly slowed wire cadence by about **+500us/frame**, matching the pcap and log fps

Action taken:
- remove the LT-only catch-up servo from code
- keep the separate exact TSN RTP clock

New strongest bottleneck signal from gpt8:
- `busy as no ready frame from user 294`
- `4 frames are in trans, total 4`
- `ring_peak=16120/16120` with `4115` packets/frame means the SW ring alone can hold about
  **3.9 frames**
- therefore `framebuff_cnt=4` is still mathematically too small for this workload once TXQ
  backpressure is included

Next distinct fix:
- increase RxTxApp ST20 pipeline TX depth again (target **8** framebuffers)
- this is not another pacing servo; it addresses frame ownership starvation now that gpt8 proved
  all 4 current framebuffers can be retained downstream simultaneously

### New finding after deeper RxTxApp TX depth

Increasing RxTxApp ST20 TX depth to `8` fixed the producer starvation symptom:
- no more `busy as no ready frame from user`
- `5 frames are in trans, total 8`
- pipeline queue now shows slack: `C:3 T:5`

But the transport log still shows the remaining root cause clearly:
- `epoch drop 2`
- `drift ptp_avg +223043ns/frame`
- `lt_pkts future=0(0.0%)`
- `ptp_lag` grows from `166666us` to `266666us`

Interpretation:
- source-side depth was necessary, but not sufficient
- the monotonic LT cursor is still accumulating exactly one extra frame of lag per epoch drop
- now that RTP is decoupled, the next distinct log-focused experiment is to keep exact per-frame
  LT stepping normally, but **re-anchor LT to the epoch-derived target only on epoch-drop
  boundaries**

Why this is distinct from failed servos:
- not per-frame catch-up
- does not alter normal frame-to-frame LT cadence
- only removes the permanent one-frame LT debt introduced by each recovery drop

Expected log improvements:
- `ptp_lag` should stop increasing by ~`33333us` per epoch drop
- `lt_pkts future` should become non-zero if accumulated LT debt was the blocker
- `time_to_tx max` should stay positive without reintroducing the gpt8 `29.4fps` regression

### Result of epoch-drop-only LT re-anchor (2026-03-26 morning)

This experiment fixed the old LT-debt accumulation cleanly:
- epoch-drop log now shows `ptp_lag 0us`
- long-run `fps` returned to the older baseline (`29.899873`)
- `drift` returned to the older baseline too (`+109294ns/frame`)

But the key TSN failure remains:
- `epoch drop 1`
- `ttx_bands future=144 ... past=154`
- `lt_pkts future=0(0.0%) past=1230497(100.0%)`
- `ring_peak=16352/16352`, `ring_stop=113018483`, `tx_partial=76544`
- queue depth still sits at `C:3 T:5`

## TSN vs RL comparison — strongest current root cause hypothesis

Good RL baseline on the same workload shows:
- `fps 29.999889`
- `epoch drop 0`
- `time_to_tx min 3547us max 7263us`
- `drift ptp_avg +227ns/frame`
- `ttx_bands future=299 past=0`
- queue depth only `C:6 T:2`

### Most likely remaining TSN root cause

The remaining TSN/RL gap is now best explained by **downstream residency/backpressure in the
TSN packet path**:

- frame-start `time_to_tx` is often positive in TSN, so frames begin with real future margin
- but by the time packets reach the transmitter, **all** packet LaunchTimes are already past
  (`lt_pkts future=0%`)
- therefore the future margin is being **consumed downstream** between `tv_sync_pacing()` and
  actual NIC submission

### Why this fits the logs

- `time_to_tx` is a **frame-start** measurement
- `lt_pkts future/past` is a **packet-at-transmitter-submission** measurement
- the apparent contradiction is exactly what a too-deep queue/residency problem looks like
- `ring_peak` at the max, enormous `ring_stop`, and high `tx_partial` all point to combined
  `SW ring + NIC TXQ` backlog, not timestamp math
- `frame_overhead avg=34us` remains tiny, so the dominant problem is no longer frame callback or
  producer latency

### Current best next distinct experiment

Do a **pure buffering-depth sweep** for TSN while keeping the current logic unchanged:
- keep exact TSN RTP clock
- keep epoch-drop-only LT re-anchor
- keep `framebuff_cnt=8`
- sweep **downstream queue depth** (especially `nb_tx_desc`, and if needed the TSN SW-ring target)

Success signal to look for:
- `lt_pkts future` becomes non-zero
- `T` moves down toward the RL baseline (`2` instead of `5`)
- `ring_peak`/`ring_stop`/`tx_partial` reduce materially
- `epoch drop` disappears without reintroducing LT debt

### NEW finding (2026-03-25, after first work-conserving patch)
The first TSN work-conserving validation exposed a **configuration/initialization bug**:

- Log showed: `tsn_loops ... ring_peak=480/480 ... tx_partial=76591`
- This strongly indicates the TSN SW ring was still effectively **~512 entries**, not 16384.
- Root cause found in code: `s->ring_count` was sized for TSN using `s->pacing_way[...]`
  **before `s->pacing_way[...]` was initialized** in `st_tx_video_session.c`.
- So the previous run did **not** actually test the intended large-ring TSN design.

**Implication**: The hypothesis "work-conserving loops are insufficient" was premature.
The run was bottlenecked by a ring init bug, so it must be re-tested after the fix.

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
- **LaunchTime bias**: `pacing->lt_bias_ns = 2 * frame_time` for TSN mode in current code
- **Monotonic PTP cursor**: `tsn_ptp_cursor_base` advances by Bresenham frame_time, independent of epoch
- **Epoch drops**: happen normally (snap forward), provide builder recovery
- **RTP timestamp**: TSN non-epoch path now uses a dedicated exact RTP frame-tick accumulator
- **Epoch-path RTP bias**: REVERTED (was wrong approach from czwartek19)
- **Catch-up servo**: REMOVED again after gpt8; exact TSN RTP clock kept, no LT servo active
- **LT resync experiment**: active only on epoch-drop boundaries; exact LT frame_time stepping remains between drops
- **NIC submission horizon**: REMOVED after gpt7 proved it was a no-op (`tx_wait_lt=0`)
- **Overhead instrumentation**: frame_overhead avg/max, notify/getframe/sync breakdown — ACTIVE
- **Diagnostic logging**: EPOCH DROP PACING, ttx_bands, lt_pkts, time_to_tx, drift, overhead — ACTIVE