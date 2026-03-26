# TSN Pacing Analysis - 2026-03-26

## Scope

This note summarizes the current TSN pacing state for the `RxTxApp --pacing_way tsn` path using:

- current workspace code
- local diff versus `origin/main`
- existing `.github/workspace` TSN notes
- local DPDK tree at `script/dpdk-25.11`
- capture/compliance artifacts:
  - `/home/labrat/mkasiew/dumps/mypcap-high.pcap`
  - `/home/labrat/mkasiew/dumps/mypcap-high.json`
- user logs from RL and TSN runs

## What TSN Is In This Repo

In this codebase, TSN pacing means:

- software builds RTP packets ahead of time
- software stamps each packet with a PTP LaunchTime
- the E830 NIC sends each packet at that programmed time using `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP`

Practical consequence:

- software should be work-conserving and submit packets early
- the NIC should do the fine packet spacing
- LaunchTimes must still be in the future when packets reach the NIC

If LaunchTimes are already in the past at submission time, TSN stops behaving like true hardware pacing and the stream becomes non-compliant even if RTP timestamps remain mathematically correct.

## What TSN Should Achieve

For ST 2110-21 narrow/gapped transmission, the TSN path should:

- keep packet departure spacing inside the narrow traffic envelope
- keep the virtual receiver buffer inside the allowed window
- avoid frame-period gaps caused by recovery logic
- preserve exact media clock progression across frames

From the current analyzer output for `mypcap-high.json`:

- `inter_frame_rtp_ts_delta`: compliant, always `3000`
- `2110_21_cinst`: not compliant
- `2110_21_vrx`: not compliant
- `narrow_streams`: `0`
- `not_compliant_streams`: `1`

So the current TSN implementation is already keeping frame-to-frame RTP cadence exact, but it is still failing the actual ST 2110-21 wire-timing checks.

## Repo State Relevant To TSN

Current local repository state:

- branch: `main`
- ahead of `origin/main`: `4` commits
- additional unstaged local edits are present

Files carrying the TSN work are mainly:

- `.github/copilot-docs/mtl-knowledge-base.md`
- `lib/src/dev/mt_dev.c`
- `lib/src/st2110/st_header.h`
- `lib/src/st2110/st_tx_video_session.c`
- `lib/src/st2110/st_video_transmitter.c`
- `lib/src/st2110/pipeline/st20_pipeline_tx.c`
- `tests/tools/RxTxApp/src/tx_st20p_app.c`

This matters because the repo already contains several iterations of TSN fixes. The current problem is not an untouched baseline; it is the remainder after multiple reasonable attempts.

## What Is Already Implemented

The current TSN path already contains these important changes:

1. `mt_dev.c` enables `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP` when the interface reports the feature.
2. `st_tx_video_session.c` sizes the TSN software ring after `pacing_way` is resolved, avoiding the old init-order bug.
3. TSN uses a larger software ring, roughly `2 * total_pkts + 512`, rounded to power-of-two.
4. TSN uses `lt_bias_ns = 2 * frame_time` to keep LaunchTimes forward-shifted.
5. TSN uses integer/Bresenham 128 ns packet stepping for `ptp_time_cursor` instead of raw `uint64_t += double` on the packet path.
6. TSN keeps a separate exact RTP frame clock so inter-frame RTP deltas stay exact even if LaunchTime handling changes.
7. TSN re-anchors LaunchTime on epoch drops only, instead of trying to servo it every frame.
8. TSN builder and TSN transmitter are both work-conserving up to `32` bulks per call.
9. RxTxApp ST20P TX now uses `framebuff_cnt = 8` and `ST20P_TX_FLAG_BLOCK_GET`.

For the current RxTxApp workload, this means:

- callback-decoupling is no longer the main story
- source-side framebuffer starvation has already been mitigated once
- exact RTP progression has already been fixed

## What The Good RL Log Shows

The RL log is a useful control sample:

- `fps 29.999962`
- `epoch drop 0`
- `time_to_tx min 0us max 7273us`
- `drift ptp_avg +123ns tsc_avg +95ns per frame`
- `ttx_bands future=297 borderline=2 past=0`
- `framebuffer queue: C:7 T:1`

Interpretation:

- frames are reaching transmit with positive headroom
- packets are not spending long downstream before actual wire send
- only a small number of frames are resident in transmit at once

## What The Current TSN Log Shows

The failing TSN log shows:

- `epoch drop 1`
- `time_to_tx min -31102us max 29490us`
- `drift ptp_avg +109297ns tsc_avg +109250ns per frame`
- `ttx_bands future=144 borderline=0 past=154`
- `lt_pkts future=0(0.0%) past=1230497(100.0%)`
- `tsn_loops build_avg=4.01 build_max=32 tx_avg=5.01 tx_max=32 ring_peak=16352/16352 ring_stop=113037788 tx_partial=76518`
- `framebuffer queue: C:3 T:5`

Interpretation:

1. Frame start is not always late.
   The `time_to_tx` max is strongly positive.

2. Packet submission is still effectively late.
   Even when frame start is future, `lt_pkts future=0%` means that by the time packets are handed to the NIC, their LaunchTimes are already behind wall clock.

3. Downstream residency is very deep.
   The software ring is hitting its ceiling and partial TX is frequent.

4. Too many frames remain in transmit.
   RL sits around `T:1..2`, but TSN sits at `T:5` in the bad sample.

5. Epoch drops are a symptom of backlog recovery, not the root problem.
   They happen because the builder eventually misses the next frame slot.

## What The Capture Says

`mypcap-high.json` confirms the stream is still not compliant:

- `video_streams = 1`
- `narrow_streams = 0`
- `not_compliant_streams = 1`
- `packet_count = 120000`
- `frame_count = 30`
- `packets_per_frame = 4115`
- `inter_frame_rtp_ts_delta = 3000` and compliant
- `2110_21_cinst = not_compliant`
- `2110_21_vrx = not_compliant`

Important nuance:

- `packet_ts_vs_rtp_ts` is also marked not compliant, but that metric is less useful here without a trusted phase-offset interpretation.
- The stronger evidence is that RTP frame cadence is already correct while Cinst and VRX are still failing.

That combination points to a transmission-timing problem, not a media-clock problem.

## DPDK And E830 Constraints That Matter

From the local DPDK tree:

- ICE LaunchTime uses a special TS queue on E830
- the driver writes `(txtime % 1e9) >> 7`
- LaunchTime resolution is `128 ns`
- the descriptor stores a `19-bit` timestamp field
- `19-bit * 128 ns = 67.1 ms` representable window per wrap

Relevant implications:

1. LaunchTime is quantized by hardware at 128 ns.
2. Anything that assumes nanosecond-exact launch without alignment is wrong.
3. A TSN lead close to two frame times at 1080p30 is already very near the hardware timestamp window.
4. If the path also buffers about one frame of packet span downstream, the safety margin gets uncomfortably small.

This does not prove that the current `2 * frame_time` bias is the immediate bug, but it is a real hardware constraint that should stay in scope.

## Root Cause Assessment

### Primary Root Cause: Downstream Residency Eats The Future Margin

This is the strongest current explanation.

Evidence:

- `time_to_tx` at frame start is often positive
- `lt_pkts future=0%` at actual NIC submission time
- TSN ring reaches `16352/16352`
- `ring_stop` is huge
- `tx_partial` is huge
- more frames are stuck in transmit than under RL

Meaning:

- `tv_sync_pacing()` is not the main failing point anymore
- the future margin is being consumed between frame sync and actual descriptor submission
- the dominant queue is the combined `SW ring + NIC TXQ + partial retry` path

### Secondary Root Cause: TSN Lead Is Too Close To The E830 Timestamp Window

Current code biases LaunchTime by about two frame times.

At 1080p30:

- one frame is about `33.33 ms`
- two frames are about `66.67 ms`
- the hardware timestamp field wraps at about `67.1 ms`

That leaves almost no representable headroom.

This is probably not the first bug to fix, but it is too close to ignore. Once queue depth is reduced, this should be re-validated instead of assumed safe.

### Secondary Risk: Partial TX Retries Can Age LaunchTimes Further

`video_trs_launch_time_tasklet()` stamps LaunchTime before burst.

If `tx_burst` is partial:

- remaining packets are moved into `trs_inflight[]`
- retry happens later
- the LaunchTime itself is not refreshed

That behavior is not automatically wrong, but with frequent partial TX it can amplify the past-LT problem. It needs direct instrumentation before changing logic.

## What Is Probably Not Worth Re-trying First

These ideas already look exhausted for this workload:

1. Epoch-drop suppression.
   It removes the only recovery mechanism and causes unbounded lag.

2. Per-frame LT catch-up servo.
   It already damaged cadence in prior experiments.

3. Callback-decoupling as the main RxTxApp fix.
   RxTxApp ST20P TX is already using blocking `get_frame` with `framebuff_cnt = 8`.

4. More RTP timestamp work.
   The capture already shows exact `3000` ticks per frame.

## Recommended Next Steps

## Current Patch Under Test

Applied in this workspace before the next validation run:

- `lib/src/st2110/st_tx_video_session.c`
- TSN software ring target reduced from about `2 frames + 512` to about `1 frame + 512`
- rationale: reduce downstream residency so packets reach the NIC with positive LaunchTime lead more often
- non-goal: this patch does not change RTP cadence logic, epoch-drop policy, or LT re-anchor behavior

Results from the first validation sweep:

- `--nb_tx_desc 128`
   - `lt_pkts future` improved sharply versus the old baseline and even reached `100%` early in the run
   - but steady state still drifted back toward mixed future/past LT and repeated epoch drops
   - analyzer result: still `not_compliant` for `2110_21_cinst` and `2110_21_vrx`
- `--nb_tx_desc 256`
   - first clear improvement: `2110_21_cinst` became compliant
   - `2110_21_vrx` still failed
   - `packet_ts_vs_rtp_ts` still failed badly
   - capture summary showed a stable narrow burst shape but wrong absolute phase (`avg_tro_ns ~17.86ms` vs `tro_default_ns ~1.27ms`)

Conclusion from that sweep:

- queue residency was a real blocker and reducing it helped materially
- but the remaining failure after `nb_tx_desc=256` is no longer mainly burst-shape
- the next strongest hypothesis is TSN RTP absolute phase misalignment: exact `3000`-tick frame deltas are preserved, but the TSN exact RTP clock was seeded from `ptp_time_cursor`, which includes `TR_offset`

Second patch now applied before the next validation run:

- keep the exact TSN RTP accumulator
- change its seed from `ptp_time_cursor` to `epoch + lt_bias_ns`
- rationale: preserve exact inter-frame RTP cadence while omitting `TR_offset` from RTP phase, matching the `ST20_TX_FLAG_RTP_TIMESTAMP_EPOCH` semantics and the compliant historical captures where `avg_tro_ns` stayed close to `tro_default_ns`

Result from `tsn-256-rtpfix`:

- `2110_21_cinst`: still compliant
- `2110_21_vrx`: still not compliant
- `packet_ts_vs_rtp_ts`: still not compliant
- but the constant phase error improved again:
   - `avg_tro_ns` moved from about `17.86ms` to about `8.32ms`
   - the local pcap analysis showed zero bad frames and stable per-frame timing after the truncated first frame

Interpretation:

- the stream shape is now narrow-compliant at the packet-burst level
- the remaining failure is a mostly constant phase lag, not burst disorder
- this lag is consistent with residual steady-state LT debt accumulating between epoch drops

Third patch now applied before the next validation run:

- reduce TSN SW ring target again, from about `1 frame + margin` to about `0.5 frame + margin`
- rationale: the first ring reduction approximately halved the constant TRO error class (`~17.86ms -> ~8.32ms`) while keeping CINST compliant at `--nb_tx_desc 256`; the next step is to reduce steady-state downstream residency further without changing RTP or epoch logic again

Expected success signals from the next run:

- `lt_pkts future` becomes non-zero
- `ring_peak` drops materially below the old TSN ceiling
- `ring_stop` and `tx_partial` decrease
- `T` frames in transmit drops toward the RL baseline
- `2110_21_cinst` stays compliant at `--nb_tx_desc 256`
- `2110_21_vrx` improves further, ideally to compliant
- `packet_ts_vs_rtp_ts` falls from the current `~8.32ms` TRO class error toward the historical compliant `~1.2ms` class

## Latest Validated State

Subsequent validation runs changed the conclusion above.

What is now validated:

- the smaller TSN ring mattered more than any RTP change
- the `4096`-class TSN ring was the first configuration that restored healthy steady-state transport behavior
- the later LaunchTime-derived RTP re-anchor experiment was a regression and should not be reused as the default next step

Key recent checkpoints:

1. `tsn-256-revert5`
    - TSN ring: `4096`
    - `epoch drop 0`
    - steady-state `lt_pkts future = 99.3%`
    - queue depth dropped to about `C:6 T:2`
    - local pcap looked clean after the truncated first frame
    - analyzer still failed `packet_ts_vs_rtp_ts` with a large stable offset around `134.572ms`

2. `tsn-256-revert6`
    - changed TSN RTP anchoring to frame LaunchTime-derived TAI
    - this was worse, not better
    - local pcap showed a clear `+6.632ms` phase step and later burst behavior
    - analyzer regressed badly (`avg_tro_ns ~22.5ms`)

3. `tsn-256-revert7`
    - backed out the failed `revert6` RTP logic
    - logs returned to the healthy `revert5` transport regime:
       - TSN ring `4096`
       - no visible epoch drops
       - steady-state `lt_pkts future` back up to `99.3%`
       - queue depth near `C:6 T:2`
    - but the run was still not fully clean:
       - local pcap showed one isolated event around frames `15-16`
       - frame 15 stretched to about `36.142ms`
       - frame 16 compressed to about `29.169ms`
       - one intra-frame gap reached about `6.887ms`, followed by immediate burst clusters
    - analyzer result:
       - `2110_21_cinst`: not compliant
       - `2110_21_vrx`: not compliant
       - `packet_ts_vs_rtp_ts`: still around `134.721ms`

4. `tsn-256-revert8`
   - kept the `revert5`/`revert7` transport shape and temporarily added targeted TSN instrumentation:
       - `tsn_deq`
       - `tsn_retry`
       - `tsn_frame_outlier`
    - logs stayed in the healthy shallow-queue regime:
       - `time_to_tx min 2957us .. 4514us max 33201us .. 33202us`
       - `ttx_bands future=298 borderline=0 past=0`
       - `lt_pkts future=99.3% past=0.7%`
       - framebuffer queue stayed near `C:6 T:2`
    - dequeue instrumentation showed packets were usually still far in the future when they left the SW ring:
       - `tsn_deq lead avg ~ +24.2ms .. +25.0ms`
       - `ring_age avg ~33.276ms`
       - occasional `lead min ~ -3.3ms .. -4.8ms`
    - retry instrumentation showed many partial retries, but not dominant retry aging:
       - `partial ~1.5M`, `gate ~151k`
       - `wait_avg ~103us .. 107us`
       - `lead avg ~ +17.0ms .. +18.6ms`
    - local pcap returned to a clean wire pattern after the truncated first frame:
       - no bad frames
       - no burst clusters
       - per-frame durations back near `31.992ms .. 31.993ms`
    - analyzer result:
       - `2110_21_cinst`: compliant
       - `2110_21_vrx`: not compliant
       - `packet_ts_vs_rtp_ts`: stable around `124.722ms`
       - `avg_tro_ns`: stable around `24.722ms`
       - `vrx histogram`: `[-3015, 100]`

Interpretation:

- `revert7` is much closer to `revert5` than to `revert6`, but `revert8` is the more important checkpoint now because it removed the visible burst event again
- the old deep-residency failure mode is no longer the dominant explanation on this path
- `revert8` shows that dequeue age and partial retries are not the active blocker in the healthy `4096`-ring regime: packets usually still reach dequeue and retry with large positive LaunchTime lead
- the remaining failure is now a stable absolute phase mismatch between wire time and the analyzer's RTP/VRX reference, not a globally bursty transport shape

## Current Minimal Fix In Code

The current branch now takes the smallest phase-correction step that still matches the evidence:

- keep the healthy `4096` TSN ring and one-frame LaunchTime bias
- keep the exact TSN RTP frame-step logic
- do not derive RTP from LaunchTime / `ptp_time_cursor`
- re-seed the TSN RTP stepper when `cur_epochs` is non-consecutive, so startup or epoch-drop frame slips do not become permanent RTP phase errors

At the same time, the temporary `tsn_deq` / `tsn_retry` / `tsn_frame_outlier` instrumentation used for `revert8` diagnosis has been removed again to keep the runtime diff smaller.

So the next TSN step should preserve the current transport path and target absolute TSN phase alignment directly, for example by checking:

- how the current TSN RTP anchor relates to `TR_offset`, `VRX`, and `transmission_start_time()`
- whether the analyzer-visible `packet_ts_vs_rtp_ts ~124.7ms` offset is an integer frame-count phase error plus a fixed TRO term
- whether current TSN seeding is preserving exact cadence but using the wrong absolute epoch for RTP/VRX conformance

It should not reopen the LaunchTime-derived RTP re-anchor idea without new evidence.

### Step 1: Run A Pure Queue-Depth Sweep

Do this without changing timestamp math.

Variables to sweep:

- `nb_tx_desc`: try `128`, `256`, `512`, `1024`
- TSN SW ring target: try smaller than current `~2 frames + 512` target

Goal:

- find the point where `lt_pkts future` becomes non-zero
- reduce `tx_partial`
- reduce `ring_peak`
- reduce `ring_stop`
- reduce `T` frames in transmit
- eliminate epoch drops without touching RTP/LT arithmetic

Reason:

- the current evidence points to excessive residency, not incorrect frame-start calculation

### Step 2: Instrument Submission Age Directly

Add narrow diagnostics in the TSN transmitter for:

- `target_ptp - cur_ptp` at dequeue time
- `target_ptp - cur_ptp` on retry of inflight packets
- first-packet and last-packet lead per bulk
- age of packets that undergo partial TX retry

This will tell you whether the real loss happens:

- before dequeue from SW ring
- during NIC backpressure
- during retry handling

### Step 3: Re-check Bias Against Hardware Window

Only after Step 1.

If smaller queue depth still leaves packets past LT, re-check whether `lt_bias_ns = 2 * frame_time` is too close to the E830 representable horizon.

Directionally, safer options are:

- a smaller bias
- or explicit wrap-aware validation

But this should be a second-stage change, not mixed into the queue-depth sweep.

### Step 4: Only Then Revisit Epoch Arithmetic

If the queue sweep produces future LT packets and compliance improves, but rare epoch drops remain, then revisit:

- `calc_frame_count_since_epoch()` precision
- any remaining `double` epoch math on large absolute timestamps

That is a correctness cleanup step, not the first-order throughput fix.

## Working Conclusion

The current TSN implementation is no longer failing because of RTP cadence, framebuffer depth, or deep downstream residency in the healthy `4096`-ring configuration.

The strongest remaining issue after `revert8` is this:

- software now computes a sensible future schedule at frame start
- packets usually still reach dequeue and retry with positive LaunchTime lead
- local wire timing is narrow-shaped and `2110_21_cinst` is compliant again
- but the stream still carries a large constant `VRX` / `packet_ts_vs_rtp_ts` phase offset

So the next serious TSN experiment should be a phase-alignment experiment that keeps the current transport path intact, not another residency sweep and not another LaunchTime-derived RTP re-anchor.