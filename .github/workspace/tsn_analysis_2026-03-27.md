# TSN Analysis - 2026-03-27

## Scope

This note analyzes the current TSN pacing behavior for:

- command: `./tests/tools/RxTxApp/build/RxTxApp --config_file ./config/tx_1v.json --ptp --pacing_way tsn`
- artifacts:
  - `/home/labrat/mkasiew/dumps/tsn-piatek.log`
  - `/home/labrat/mkasiew/dumps/tsn-piatek.pcap`
  - `/home/labrat/mkasiew/dumps/tsn-piatek.json`
- code paths:
  - `lib/src/st2110/st_tx_video_session.c`
  - `lib/src/st2110/st_video_transmitter.c`
  - `lib/src/dev/mt_dev.c`
  - `script/dpdk-25.11/drivers/net/intel/ice/ice_rxtx.c`

The focus is latency compliance: actual transmit time on the wire must track the RTP-derived ST 2110-21 packet read schedule, not merely preserve RTP cadence.

## What TSN Means Here

In this tree, TSN is not a generic Layer-2 marketing term. It is the E830 LaunchTime path:

- MTL computes a per-packet target PTP time in `tv_sync_pacing()` and `pacing_forward_cursor()`.
- Each mbuf stores both a TSC gate timestamp and a PTP LaunchTime timestamp.
- `_video_trs_launch_time_tasklet()` copies the PTP time into the DPDK TX timestamp dynfield and sets the `SEND_ON_TIMESTAMP` offload flag.
- The ICE PMD places that timestamp into the E830 TSQ descriptor using 128 ns resolution.

Relevant code:

- `tv_sync_pacing()` in `lib/src/st2110/st_tx_video_session.c`
- `tv_update_tsn_rtp_time_stamp()` in `lib/src/st2110/st_tx_video_session.c`
- `_video_trs_launch_time_tasklet()` in `lib/src/st2110/st_video_transmitter.c`
- `video_trs_burst()` in `lib/src/st2110/st_video_transmitter.c`
- LaunchTime offload enable in `lib/src/dev/mt_dev.c`
- ICE descriptor encoding in `script/dpdk-25.11/drivers/net/intel/ice/ice_rxtx.c`

## What ST 2110-21 Requires

From `st2110-21-2022.pdf`:

- the model is evaluated at the sender egress interface
- compliance depends on actual transmission instants, not on when software wanted to send
- senders must satisfy both:
  - the Network Compatibility Model (`CINST` / `CMAX`)
  - the Virtual Receiver Buffer Model (`VRXFULL` against the packet read schedule)
- the packet read schedule is derived from RTP clock timing plus `TROFFSET`

Practical consequence for this bug:

- exact `dRTP = 3000` is necessary but not sufficient
- narrow `CINST` compliance is necessary but not sufficient
- if packets leave late relative to RTP-derived schedule, the stream is still non-compliant even when packet spacing within a frame looks good

## Current Run Summary

### Analyzer Result

From `tsn-piatek.json`:

- `packet_ts_vs_rtp_ts = not_compliant`
  - min `1.014233 ms`
  - avg `17.215765 ms`
  - max `32.124043 ms`
- `inter_frame_rtp_ts_delta = compliant`
  - exact `3000`
- `2110_21_cinst = compliant`
  - `cmax_narrow = 4`
  - `cmax_wide = 16`
- `2110_21_vrx = not_compliant`
  - `vrx_full_narrow = 8`
  - `vrx_full_wide = 720`
- `rtp_sequence = compliant`
- `avg_tro_ns = 17.215763 ms`
- `tro_default_ns = 1.244444 ms`

This is the key pattern: cadence is correct, bursts are acceptable, but absolute transmit time is late.

### Direct Pcap Result

Running `dumps/analyze_pcap.py` on `tsn-piatek.pcap` shows a stepwise wire-phase error:

- frames `1-6`: `TRO ~= 0 us`
- frame `7`: first plateau at `~4.577 ms`
- frame `37`: second plateau at `~11.210 ms`
- frame `67`: third plateau at `~17.843 ms`
- frame `97`: fourth plateau at `~24.476 ms`
- frame `127`: fifth plateau at `~31.109 ms`

Each new plateau is created by one abnormal inter-frame gap of about `8.013 ms`.

Within each plateau:

- `dRTP` stays exactly `3000`
- inter-frame gap stays near `1.380-1.426 ms`
- intra-frame packet spacing is stable
  - p50 gap `~47 us`
  - p99 gap `~47 us`
- no burst clusters larger than 10 packets were detected

Interpretation:

- the wire shape inside a frame is mostly healthy
- the frame start time is slipping late in discrete jumps
- this is a frame-boundary / absolute-phase problem, not a per-packet microburst problem

## Log Timeline

### 1. LaunchTime Path Is Armed Correctly

The current tree is using the TSN path as expected:

- `tv_init_pacing_epoch()` logs TSN startup init
- `tv_sync_pacing()` shows `lt_bias=33333333`
- LaunchTime submission path is active in `_video_trs_launch_time_tasklet()`

This is not a case of falling back to TSC pacing.

### 2. There Were Real Headroom Collapses Before Capture

`tsn-piatek.log` contains `TSN HEADROOM ANOMALY[...]` records around `10:18:45`.

Observed pattern:

- `sync_time_to_tx_ns` falls into the `9.5-16.4 ms` range
- packet-0 submit headroom (`delta_ptp_ns`) shrinks from about `+0.57 ms` to about `-6.24 ms`
- `gap_rtp` remains `3000`
- `dequeue_to_submit_ptp_ns` stays tiny (`~187 us`)

So the missing time is not spent after first dequeue. The time is already lost before or by the moment packet-0 becomes eligible for submit.

### 3. There Were Epoch Resyncs Before the User Capture Window

The log also shows TSN RTP resync events:

- `10:18:48`: `epoch=...1842 prev_epoch=...1840 resync=1`
- `10:18:58`: `epoch=...2142 prev_epoch=...2140 resync=1`

These are real non-consecutive epochs.

Important consequence:

- the pcap starts later, at about `10:18:59.761`
- therefore the pcap/json capture does not include the actual epoch skip event
- instead it shows the steady-state phase error left behind after that event

This explains why the current artifacts can show:

- clean `dRTP = 3000`
- no packet drops
- compliant `CINST`
- but still a very large `packet_ts_vs_rtp_ts` / `VRX` failure

### 4. Stable Window Still Shows a Lagging TX State

Later, the delayed stable trace logs a mode transition:

- `TSN MODE[0]: state=lagging prev=healthy`

At that transition:

- `sync_time_to_tx_ns ~= 24.38 ms`
- `sync_to_deq_ptp_ns ~= 48.84 ms`
- `enqueue_to_deq_ptp_ns ~= 48.66 ms`
- `deq_headroom_ptp_ns ~= -24.46 ms`
- `submit_headroom_ptp_ns ~= +8.69 ms`

In the later stable frames of the same window:

- `time_to_tx_ns` settles around `17.75-17.85 ms`
- submit headroom settles around `1.95-2.06 ms`
- `gap_rtp` still stays `3000`

Interpretation:

- the transmitter is spending almost an extra frame of time between sync and first dequeue
- queue residency after sync is dominating the phase behavior
- once the system enters the lagging mode, it keeps far less LaunchTime margin at submit

## Code-Path Interpretation

### What Is Working

1. RTP stepping logic is mostly fixed.

`tv_update_tsn_rtp_time_stamp()` resyncs on non-consecutive epochs and uses an exact integer stepper. That matches the current evidence: `inter_frame_rtp_ts_delta` is exact even when wire phase is wrong.

2. LaunchTime offload is enabled correctly.

- `lib/src/dev/mt_dev.c` enables `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP`
- the ICE PMD uses the TX dynfield and TSQ only for E830
- the driver stores `(txtime % 1s) >> 7` into the descriptor
- queue context uses `ICE_TXTIME_CTX_RESOLUTION_128NS`

So hardware quantization is `128 ns`, which cannot explain a `4.5-31 ms` error.

3. The shallow TSN ring is still the right direction.

`st_tx_video_session.c` sizes the TSN SW ring to about `0.5 frame + margin`, which is `1024` for this workload (`686 / 2 + 512`, rounded to power of two). The current failure is not the old deep-ring catastrophe.

### What Still Looks Wrong

1. `tv_sync_pacing()` still re-anchors LaunchTime from `transmission_start_time(cur_epochs)` every frame.

That means skipped epochs or late-advance decisions can move absolute LaunchTime phase even when RTP cadence is preserved.

2. `_video_trs_launch_time_tasklet()` still dequeues a bulk without splitting on frame boundaries.

It stops only on dummy packets:

- it scans `pkt_idx`
- it breaks only if `pkt_idx == ST_TX_DUMMY_PKT_IDX`
- it does not break when a new frame starts at `pkt_idx == 0`

That allows a dequeue to contain the tail of frame `N` and the first packets of frame `N+1` together.

When headroom is already low, that is exactly the kind of boundary coupling that produces stepwise late frame starts.

3. The dominant latency debt is between sync and first dequeue, not in `video_trs_burst()` itself.

The logs show:

- sync to enqueue is only about `180 ns`
- dequeue to submit is only about `187 us`
- the large debt is in sync to dequeue / enqueue to dequeue

So the active bottleneck is queue residency plus transmitter gating/backpressure, not the final NIC doorbell.

## Current Diagnosis

The current TSN issue is a two-stage problem.

### Stage A: Phase is Already Wrong Before Capture

Earlier headroom collapse plus non-consecutive epoch recovery happen before the user capture starts. That leaves the later capture on a late absolute phase plateau even though RTP cadence has recovered.

This is why the compliance JSON looks like a pure latency error:

- `packet_ts_vs_rtp_ts` bad
- `VRX` bad
- `dRTP` perfect
- `CINST` good

### Stage B: Frame-Start Phase Keeps Stepping Later

The pcap then shows additional discrete frame-start slips of about `+6.633 ms` produced by abnormal `~8.013 ms` inter-frame gaps.

That points to a boundary-handling problem in the transmit path, not a media-clock problem.

## What To Improve

## 1. First Fix Candidate: Split LaunchTime Dequeue On Frame Boundary

This is the best low-risk next patch.

Target function:

- `_video_trs_launch_time_tasklet()` in `lib/src/st2110/st_video_transmitter.c`

Current behavior:

- a dequeue bulk may contain `[tail of frame N] + [packet-0 of frame N+1]`

Recommended behavior:

- if `pkt_idx == 0` is seen after at least one packet already belongs to the current bulk, stop there
- burst only the preceding same-frame packets
- save the boundary packet and later packets into inflight for the next tasklet call

Why this is the right first change:

- it directly targets frame-boundary timing
- it does not touch RTP timestamp logic
- it matches the already-proven RL design idea described in the repo memory
- it is the smallest change most likely to remove the `+6.633 ms` plateau steps

## 2. Second Fix Candidate: Preserve LaunchTime Phase Across Epoch Recovery

This is the next issue after dequeue splitting.

Do not change RTP seeding again. That was already disproved by earlier experiments.

Instead, inspect only the LaunchTime phase path:

- `calc_frame_count_since_epoch()`
- `tv_sync_pacing()`
- `transmission_start_time()`
- `ptp_time_cursor`
- `late_advance`

The likely requirement is:

- recover scheduler slot / epoch accounting when the builder falls behind
- but do not hard-reset the absolute LaunchTime phase in a way that permanently moves wire timing late relative to the RTP-derived PRS

In other words: preserve transmit phase independently from RTP cadence.

## 3. Do Not Blame Hardware Timestamp Resolution

The DPDK/ICE side is not the active limiter here:

- TSQ is enabled only for E830, as expected
- timestamp resolution is `128 ns`
- descriptor stores `(txtime % 1s) >> 7`

That matters for sub-microsecond rounding, but it cannot create the observed millisecond plateaus.

## 4. Keep The Current RTP Stepper Model

Current evidence says RTP is not the active bug in this run.

Do not reintroduce:

- LaunchTime-derived RTP re-anchoring
- `transmission_start_time()`-seeded RTP experiments

The capture already proves that the wire can be wrong while `dRTP` is still exact.

## Validation Criteria For The Next Patch

The next candidate should be judged by all of these, not just one:

1. `packet_ts_vs_rtp_ts` must collapse below the analyzer limit, not merely improve average TRO.
2. `2110_21_vrx` must pass, not just `2110_21_cinst`.
3. Local pcap must stop showing staircase plateaus at `~4.6 / 11.2 / 17.8 / 24.5 / 31.1 ms`.
4. No later `~8.013 ms` inter-frame gap should appear after startup settles.
5. After warmup, log should avoid:
   - `TSN HEADROOM ANOMALY`
   - repeated `resync=1`
   - `TSN MODE ... state=lagging`

## Bottom Line

TSN is already good enough in this run to prove that:

- the media clock is stepping correctly
- the NIC LaunchTime path is enabled
- narrow burst shape is achievable

What is still broken is absolute phase selection and preservation at frame boundaries.

The first patch from this note is now in the tree: LaunchTime dequeue is split on `pkt_idx == 0` so one dequeue no longer mixes the tail of frame `N` with packet-0 of frame `N+1`.

## Follow-Up: `tsn-piatek2`

The user reran the same workload after the frame-boundary split and captured:

- `/home/labrat/mkasiew/dumps/tsn-piatek2.log`
- `/home/labrat/mkasiew/dumps/tsn-piatek2.pcap`
- `/home/labrat/mkasiew/dumps/tsn-piatek2.json`

### What Changed

From `tsn-piatek2.json`:

- `packet_ts_vs_rtp_ts` got worse:
  - min `20.692348 ms`
  - avg `38.638061 ms`
  - max `53.858042 ms`
- `inter_frame_rtp_ts_delta` stayed exact at `3000`
- `2110_21_cinst` stayed compliant
- `2110_21_vrx` still failed

From direct pcap analysis:

- frames `1-7`: `TRO ~= 0 us`
- frames `8-37`: plateau near `6.633 ms`
- frames `38-67`: plateau near `13.266 ms`
- frames `68-97`: plateau near `19.899 ms`
- frames `98-127`: plateau near `26.532 ms`
- frames `128+`: plateau near `33.165 ms`

Each plateau step is again created by one `~8.013 ms` frame-boundary gap, so the boundary symptom remains. But the new logs show a more dominant mechanism behind it.

### New Dominant Symptom

The delayed stable traces in `tsn-piatek2.log` show that the transmitter is not recovering once it falls behind:

- the TSN SW ring sits almost full: `ring_count=1016`, `ring_peak=1020/1020`
- `TSN STABLE DEQ[...]` often happens `~48-49 ms` after sync
- `TSN MODE` spends most samples in `lagging`
- the periodic summary shows:
  - `tsn_loops build_avg=1.00 build_max=1 tx_avg=1.00 tx_max=1`
  - `tx_partial` large
  - `tsc_floor` very large (`~51k` hits per period)

At the same time, packet-0 is already late in absolute PTP time when it is dequeued:

- `TSN STABLE TX[0..20]`: `delta_ptp_ns ~= -13.8 ms .. -15.1 ms`
- later stable frames drift to `-20 ms`, `-26 ms`, `-33 ms`

But the same log lines show the synthetic TSC gate still holding those already-late bulks in the future by only `~186-196 us`.

That means:

- LaunchTime is already missed
- the ring is already backed up
- yet the transmitter still serializes recovery to roughly one bulk per invocation via the TSC floor

This explains the new behavior better than RTP math does:

- exact `dRTP=3000` survives
- `CINST` survives
- the wire walks later in discrete frame-sized plateaus because the transmitter cannot drain backlog once late
- skipped epochs then show up as `resync=1` / `gap_rtp=6000`, which create the next staircase step

### Updated Diagnosis

For `tsn-piatek2`, the dominant blocker is now transmit-side recovery throttling:

- the frame-boundary split removed one cross-frame coupling path
- but the TSC floor in `_video_trs_launch_time_tasklet()` still throttles already-late LaunchTime bulks
- because the wrapper only advances while a pass actually sends packets, this floor effectively collapses catch-up to one bulk at a time once the stream is late
- the ring remains full, packet-0 stays late, and the run never recovers phase

### Next Fix

The next low-risk change is to keep the TSC floor only for bulks whose LaunchTime is still in the future.

If a bulk is already late in PTP time, waiting again on a synthetic TSC floor cannot improve compliance; it only preserves backlog. The right behavior for that case is bounded catch-up via the existing multi-pass TSN wrapper, while still keeping the normal floor for healthy future-dated bulks.

## Follow-Up: `tsn-piatek3`

The user reran again after that late-bulk floor change and captured:

- `/home/labrat/mkasiew/dumps/tsn-piatek3.log`
- `/home/labrat/mkasiew/dumps/tsn-piatek3.pcap`
- `/home/labrat/mkasiew/dumps/tsn-piatek3.json`

### What Changed Relative to `tsn-piatek2`

From `tsn-piatek3.json`:

- `packet_ts_vs_rtp_ts` stayed essentially unchanged on average:
  - min `6.088018 ms`
  - avg `38.619012 ms`
  - max `58.644056 ms`
- `avg_tro_ns` moved to `17.614445 ms`
- `inter_frame_rtp_ts_delta` regressed to `not_compliant`
  - range `[3000, 6000]`
- `2110_21_cinst` regressed to `not_compliant`
- `2110_21_vrx` still failed

So the late-bulk floor change did **not** solve the main latency offset, and it reintroduced an actual transport discontinuity.

### Direct Pcap Result

`analyze_pcap.py` shows three phases:

1. frames `1-13`: clean baseline, `TRO ~= 0 us`
2. frames `14-135`: the same staircase pattern as before
   - plateau near `6.633 ms`
   - then `13.266 ms`
   - then `19.899 ms`
   - then `26.532 ms`
   - then `33.165 ms`
   - each step still created by one `~8.013 ms` frame-boundary gap
3. frame `136+`: a new hard discontinuity
   - frame `136` has `dRTP=6000`
   - inter-frame gap collapses to `~1 us`
   - `max_burst=111` packets at frame start
   - `TRO` then walks negative from about `-1.5 ms` down to about `-19.4 ms`

This is materially worse than `tsn-piatek2`: the staircase stayed, and a real skipped-frame / burst event came back.

### What The Stable Logs Say

The delayed stable window changed in one important way and stayed the same in another.

What improved:

- packet-0 submit headroom (`TSN STABLE TX ... delta_ptp_ns`) is now consistently positive
  - `tsn-piatek2`: about `-20.46 ms .. +33.33 ms`, average `~2.47 ms`
  - `tsn-piatek3`: about `+21.09 ms .. +27.82 ms`, average `~24.94 ms`

So the late-bulk floor change really did stop the transmitter from submitting packet-0 already past its LaunchTime target.

What did **not** improve:

- first dequeue is still very late
  - `tsn-piatek2`: `sync_to_deq_ptp_ns ~48.61 ms` average
  - `tsn-piatek3`: `sync_to_deq_ptp_ns ~49.11 ms` average
- the ring still sits pinned near full
  - `ring_count=1016`
  - `ring_peak=1020/1020`
- periodic loop stats are still stuck at one bulk per send call
  - `tsn_loops ... tx_avg=1.00`
- `TSN MODE` still spends most samples in `lagging`

The logs around the stable window make the tradeoff explicit:

- frames `0-21` in the stable trace have `delta_ptp_ns ~+27-28 ms`
- at stable frame `22`, the whole run shifts by `~6.63 ms`
  - `time_to_tx_ns` drops from about `43.6 ms` to about `36.98 ms`
  - `TSN STABLE DEQ` jumps from the usual `~49.0 ms` to `55.47 ms`
  - `TSN STABLE TX` headroom drops from about `27.7 ms` to about `21.2 ms`
- that exactly matches the next staircase step seen in pcap

In other words: packet-0 is no longer submitted late, but the system still burns an extra `~6.63 ms` between sync and first dequeue when it falls behind, and that still becomes a permanent wire-phase plateau.

### New Interpretation

`tsn-piatek3` disproves the simplified `tsn-piatek2` theory that the main blocker was just “TSC floor throttles already-late LaunchTime bulks.”

The floor relaxation changed one symptom:

- software submit headroom became strongly positive again

But it did **not** change the deeper one:

- the transmitter still reaches packet-0 dequeue about `49-55 ms` after sync with the SW ring pinned full

And because the root lag remained, the run eventually fell back into a real epoch skip / burst event (`dRTP=6000`, frame-start burst 111 packets).

### Updated Diagnosis After `tsn-piatek3`

The dominant remaining issue is still downstream of sync/enqueue and still transport-side, but it is **not** solved by relaxing the late-bulk TSC floor alone.

Current evidence says:

- the frame-boundary split was still the right fix to keep
- the late-bulk floor relaxation improved submit headroom but did not reduce backlog residency
- the true limiter is the persistent dequeue / TXQ / backpressure regime that leaves packet-0 waiting `~49-55 ms` before first dequeue and periodically forces non-consecutive epoch recovery

So the next fix should **not** continue loosening the TSC floor in isolation. The next candidate must target the persistent lagging regime itself:

- why the TSN transmitter still sees `tx_avg=1.00` with the ring pinned full
- why first dequeue remains almost `1.5 frames` after sync even when packet-0 LaunchTime is still far in the future
- whether the effective limiter is partial `mt_txq_burst()` acceptance / TXQ residency or insufficient catch-up work per scheduling slice

## Follow-Up: `tsn-piatek5`

Artifacts analyzed:

- `/home/labrat/mkasiew/dumps/tsn-piatek5.log`
- `/home/labrat/mkasiew/dumps/tsn-piatek5.pcap`
- `/home/labrat/mkasiew/dumps/tsn-piatek5.json`

### Summary

The first MTL-side retry fix was active, but it did not solve the dominant TSN pacing problem.

What changed:

- `tsn_txdiag[0] ... cleanup=` became large immediately, proving that the launch-time retry path now calls `mt_txq_done_cleanup()` before inflight retries

What did not change:

- `zero=` stayed almost equal to `cleanup=`
- the SW ring stayed pinned at `1020/1020`
- `tx_avg` stayed `1.00`
- `TSN TX PASS_BUDGET[...]` still appeared
- stable trace still showed a real `gap_rtp=6000`
- the pcap still showed the classic `+6.633 ms` staircase latency plateaus

### JSON Results

From `tsn-piatek5.json`:

- `packet_ts_vs_rtp_ts`: `not_compliant`, range about `1.015 ms .. 25.738 ms`, average `~13.733 ms`
- `inter_frame_rtp_ts_delta`: `compliant`, exact `3000`
- `2110_21_cinst`: `compliant`
- `2110_21_vrx`: `not_compliant`
- `avg_tro_ns`: `13,733,351 ns`
- `tro_default_ns`: `1,244,444 ns`

So this run again looks like a pure absolute-phase / VRX failure for most of the capture window, until the log shows a real RTP discontinuity.

### Pcap Results

The pcap reintroduced the same staircase structure seen in earlier bad runs:

- frames `1-9`: `TRO ~0 us`
- frame `10`: step to about `4.823 ms`
- frame `40`: step to about `11.455 ms`
- frame `70`: step to about `18.089 ms`
- frame `100`: step to about `24.722 ms`

Those steps are again created by repeated `~8.013 ms` inter-frame gaps.

Analyzer-script summary:

- `119` frames detected
- `88` bad frames
- `TRO range`: `0.0 us .. 24722.3 us`
- inter-frame gap `P99 = max = 8013.0 us`

### Log Diagnostics

The new counters proved the cleanup fix is not enough.

Representative windows:

- `cleanup=1290507`, `zero=1261192`
- `cleanup=36060191`, `zero=36019679`
- `cleanup=28944476`, `zero=28900841`

So reclaiming completed descriptors does happen, but it almost never converts a zero-progress retry into forward progress.

The stable trace also captured a real discontinuity:

- `TSN STABLE TX[39]`: `gap_rtp=6000`
- `gap_ptp_ns ~34.713 ms`
- `delta_ptp_ns ~+14.772 ms`

The log also still contains non-consecutive RTP resyncs:

- `epoch ...80010 -> ...80012`
- `epoch ...80310 -> ...80312`

So this is still not just a benign fixed latency offset.

### Updated Diagnosis After `tsn-piatek5`

`tsn-piatek5` rejects the narrower `tsn-piatek4` fix hypothesis that descriptor cleanup before retry would be sufficient.

What this run proves:

- retry cleanup is useful instrumentation and probably still a good behavior to keep
- but stale completed descriptors are **not** the dominant remaining cause of zero-progress retries
- the TSN path still saturates the downstream admission path with future LaunchTime packets, so tiny inflight tails remain starved even after cleanup

The next fix must therefore preserve explicit downstream admission headroom under TSN backpressure instead of continuing to dequeue fresh future packets into a saturated path.

## Applied Fix After `tsn-piatek5`

The next candidate now implemented in the tree is a bounded LaunchTime admission window inside `_video_trs_launch_time_tasklet()`.

Design:

- do not hand a TSN bulk to the NIC just because its software `target_tsc` is ready
- also require its packet-0 LaunchTime headroom to be within a bounded future window
- size that window from downstream queue geometry instead of a magic constant:
  - about one TXQ worth of packet time: `nb_tx_desc * trs`
  - capped at `frame_time / 8`
  - with a small minimum floor so the gate still leaves scheduler margin

Behavioral change:

- fresh dequeues now stay in `trs_inflight[]` until they are both TSC-ready and within the PTP admission window
- inflight retry paths (`trs_inflight[]` and `trs_inflight2[]`) use the same admission test before retrying, so the tasklet no longer hammers a `0/3` remainder while its LaunchTime is still about one frame in the future
- periodic stats now expose this path explicitly with `admit_wait=` and `admit_save=` in `tsn_txdiag[...]`

Why this is the best current candidate:

- it targets the concrete `tsn-piatek4/5` failure mode directly: the NIC queue is being occupied by future-dated LaunchTime packets, starving small inflight tails even after descriptor cleanup
- it keeps the validated exact TSN RTP stepper unchanged
- it keeps the frame-boundary split and cleanup-before-retry fixes that are already proven useful
- it moves backlog residence back into the software ring, where MTL can still reorder work cooperatively, instead of using scarce downstream TX admission for packets that are still tens of milliseconds early

## Follow-Up: `tsn-piatek6`

The user reran against that admission-window candidate and it failed immediately.

Artifacts analyzed:

- `/home/labrat/mkasiew/dumps/tsn-piatek6.log`

### What Broke

The regression was not a subtle compliance miss. The new gate destabilized the startup path badly enough to trigger NIC-side protection.

Early log pattern:

- `TSN STARTUP DEQ[...]` jumped to about `78-112 ms` after sync
- repeated `TSN TX PASS_BUDGET[...]` appeared while the ring stayed near full
- packet-0 submit moved deeply late (`delta_ptp_ns ~ -24 ms .. -64 ms`)
- non-consecutive RTP resyncs returned almost immediately
- `calc_frame_count_since_epoch()` started dropping whole runs of frames (`drop 28`, `drop 29`)

Then the path escalated into hard failures:

- repeated `dev_pkt_valid(... invalid nb_segs 3)` errors on the TX queue
- ICE driver `Malicious Driver Detection` events on TX queues
- `video_trs_burst_fail(...), hang duration 1000 ms`

Periodic stats confirmed that the new gate was active and harmful:

- `tsn_txdiag[0] ... admit_wait=8169125`
- but only `admit_save=86`
- plus `epoch drop 116`

So the candidate did not create a healthy software-side admission buffer. It mostly created a long startup stall, filled the SW ring, and then forced the rest of the path into catastrophic recovery.

### Root Cause Of The Regression

The bounded admission-window idea was directionally reasonable, but on this path it was placed at the wrong layer.

- It delayed packet-0 handoff to the NIC while the builder kept running with one-frame LaunchTime bias.
- That let the SW ring fill early during startup.
- Once the ring was pinned, dequeue residence exploded, epoch recovery began skipping whole frame runs, and the TX path started to see invalid/unsafe packet state.

In other words, this candidate did not preserve downstream headroom. It converted headroom control into a startup backlog amplifier.

### Action Taken

The admission-window candidate has been removed from the code.

Kept in tree:

- frame-boundary split on TSN dequeue
- late-bulk floor relaxation
- TSN stall / pass-budget diagnostics
- cleanup-before-retry and recoverable zero-progress handling

Removed again:

- the bounded LaunchTime admission-window gate
- its `admit_wait` / `admit_save` counters

This run should be treated as a hard rejection of that design, not as something to tune.