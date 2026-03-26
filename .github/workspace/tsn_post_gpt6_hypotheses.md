# TSN Post-gpt6 Hypothesis Shortlist

## Already tested / do not repeat blindly

These were already tested and are either failed or not useful for the current RxTxApp workload:

- epoch-drop suppression
- callback-decoupling as the main RxTxApp fix
- larger LT bias by itself (`1 frame` then `2 frames`)
- bounded monotonic catch-up servo on the shared PTP/RTP cursor
- builder/transmitter work-conserving loops as a complete fix
- TSN ring init-order bug (already fixed)
- RxTxApp ST20 TX `framebuff_cnt=2` bottleneck (already fixed to `4`)

## New hypotheses considered after gpt6

### H1 — NIC TXQ is being overfilled too far ahead of LaunchTime (REJECTED AFTER GPT7)
Why it fits:
- SW ring is no longer the first bottleneck.
- `tx_partial` stays huge, so the downstream TXQ path is still saturated.
- E830 txtime field wraps at ~67.1ms; submitting packets too far ahead is unsafe.
- With a 16k SW ring and 8k TXQ, packets can sit long enough to reach hardware already past LT.

Distinct from prior work:
- not TSC gating per bulk
- not more bias tuning by itself
- not builder-side throttling
- this is a **NIC submission horizon** idea: keep hardware queue depth bounded in time, not in packets

Validation result:
- gpt7 showed `tx_wait_lt=0`, `wait_max=0us`
- therefore the horizon never engaged on the real workload
- remove from code and do not retest blindly

### H2 — LaunchTime and RTP need separate clocks (SELECTED)
Why it fits:
- gpt6 proved that modifying the shared monotonic cursor broke RTP cadence (`3045` ticks/frame).
- a future LT-only correction path should not be allowed to perturb RTP.

Distinct from prior work:
- not the failed catch-up servo; this is architectural decoupling so future LT corrections can be safe.

Chosen because:
- gpt7 falsified H1 as a no-op
- gpt6 already proved shared-cursor correction is unsafe
- this is the lowest-risk way to allow LT-only correction while preserving exact RTP cadence

### H3 — two-frame LT bias may violate the E830 67.1ms txtime modulo window
Why it fits:
- `2 * frame_time ≈ 66.67ms`, leaving almost no margin before the 67.1ms wrap.
- once in-frame packet span is included, later packets may exceed the safe window.

Distinct from prior work:
- this is not “increase/decrease bias again” as blind tuning; it is a hardware-format constraint hypothesis.

When to try:
- if TXQ horizon still leaves all packets past-LT.

### H4 — residual epoch drops may come from frame/epoch arithmetic precision
Why it fits:
- packet-level 128ns arithmetic was fixed with Bresenham.
- epoch counting still mixes large absolute timestamps and `double` frame_time.

Distinct from prior work:
- this is an epoch classification correctness hypothesis, not a pacing policy change.

When to try:
- if LT future/past behavior improves but single sporadic epoch drops remain.

## Applied experiments after gpt6

### TSN LaunchTime submission horizon — rejected
Implementation idea:
- keep work-conserving builder and large SW ring
- but do **not** submit a TSN bulk to the NIC if its first packet LaunchTime is more than about one frame ahead of current PTP
- instead hold that bulk in transmitter inflight state until it enters the safe horizon

Current implementation details:
- file: `lib/src/st2110/st_video_transmitter.c`
- horizon: roughly `frame_time - 2ms`
- new diagnostics:
  - `tx_wait_lt`
  - `wait_max`

Expected success signals:
- non-zero `lt_pkts future`
- reduced `tx_partial`
- fewer/no epoch-drop-induced phase steps in pcap
- improved `VRX`

Observed failure signal:
- no activation at all: `tx_wait_lt=0`, `wait_max=0us`

### Separate TSN RTP clock + LT-only catch-up
Implementation idea:
- keep exact RTP cadence independent from the LaunchTime cursor
- use integer+Bresenham per-frame RTP tick accumulation for TSN non-epoch RTP
- keep LT-only bounded positive correction at frame boundaries

Implementation details:
- files:
  - `lib/src/st2110/st_header.h`
  - `lib/src/st2110/st_tx_video_session.c`
- TSN pacing now stores dedicated RTP frame-step state
- TSN RTP updates no longer derive from the monotonic/corrected LT cursor

Expected success signals:
- exact `inter_frame_rtp_ts_delta = 3000`
- `ptp_lag` no longer grows without bound
- `lt_pkts future` becomes non-zero
- better VRX / pkt-ts behavior in late-run pcaps

Observed result (gpt8):
- exact RTP delta was restored (`3000` ticks/frame)
- but LT-only catch-up still failed: wire cadence slowed to about `33.833ms/frame`, fps fell to
  `29.4`, `epoch drop` rose to `7`, and `lt_pkts future` stayed `0%`
- therefore keep the separate TSN RTP clock, but remove LT-only catch-up

### H5 — downstream frame residency exceeds current pipeline depth (SELECTED AFTER GPT8)
Why it fits:
- log shows `4 frames are in trans, total 4`
- `ring_peak=16120` with `4115` packets/frame means the SW ring alone can retain about `3.9`
  frames before counting TXQ backlog
- builder still reports `busy as no ready frame from user` on nearly every frame

Distinct from prior work:
- this is not callback-decoupling
- not another LT bias/servo change
- not the original `2 -> 4` tweak blindly repeated; gpt8 shows the real downstream residency now
  reaches the new ceiling, so depth must exceed `4`

Chosen because:
- gpt8 falsified LT-only catch-up as a transport fix
- exact RTP is already preserved, so the active blocker is frame ownership starvation
- next low-risk test is to raise RxTxApp ST20 TX `framebuff_cnt` to `8`

Follow-up result:
- depth `8` removed the producer-starvation symptom (`C:3 T:5`, no user-busy line)
- but `epoch drop 2`, `lt_pkts future=0%`, and growing `ptp_lag` remained
- so H5 was necessary, but not the final fix

### H6 — re-anchor LT only when epoch recovery actually happens (SELECTED AFTER DEPTH-8)
Why it fits:
- logs now cleanly show `ptp_lag` rising by about one frame per epoch drop (`166666us -> 200000us -> 233333us -> 266666us`)
- RTP is already decoupled, so LT can be corrected without breaking `3000`-tick cadence
- the failed gpt8 servo changed LT cadence every frame; this idea changes LT only at drop boundaries

Distinct from prior work:
- not the failed shared-cursor servo
- not the failed LT-only per-frame catch-up
- not another bias change; normal LT step stays exact `frame_time`

Implementation idea:
- in TSN mode, keep exact monotonic LT stepping between drops
- but when `calc_frame_count_since_epoch()` reports an epoch drop, re-anchor the LT cursor to the
  current epoch-derived target for that frame boundary only

Expected success signals:
- `ptp_lag` stops growing by `~33333us` per drop
- `lt_pkts future` becomes non-zero
- no repeat of the gpt8 `33.833ms/frame` cadence regression

Observed result:
- `ptp_lag` reset succeeded (`0us` at epoch drop)
- `fps`/`drift` returned to the older baseline instead of the gpt8 regression
- but `lt_pkts future` stayed `0%`, so H6 was necessary but not sufficient

### H7 — TSN downstream residency is consuming all frame-start future margin (SELECTED AFTER RL COMPARISON)
Why it fits:
- TSN frame-start `time_to_tx` is often positive, but packet-level `lt_pkts future` is still `0%`
- RL baseline keeps all frames future and only `T:2`, while TSN sits at `C:3 T:5`
- TSN also shows `ring_peak=16352/16352`, huge `ring_stop`, and high `tx_partial`

Distinct from prior work:
- not a timestamp math hypothesis
- not another LT servo or bias change
- not retrying the removed TX horizon idea; this is a **queue residency depth** hypothesis for the
  whole `SW ring + TXQ` path

Implementation / validation direction:
- do a pure depth sweep for TSN (`nb_tx_desc`, and possibly smaller TSN SW ring target)
- keep current pacing logic fixed during the sweep so only residency changes

Expected success signals:
- non-zero `lt_pkts future`
- fewer frames in transmit (`T` drops toward RL baseline)
- reduced `ring_peak`, `ring_stop`, and `tx_partial`
- zero epoch drops without LT-debt accumulation
