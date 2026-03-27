# TSN State - 2026-03-26

## Goal

Make `RxTxApp --config_file ./config/tx_1v.json --ptp --pacing_way tsn --nb_tx_desc 256`
fully ST 2110-21 compliant on E830 while keeping the diff close to `origin/main`.

## Current Code Intentionally Kept

- TSN ring sizing after `pacing_way` init, current target `4096`
- one-frame `lt_bias_ns`
- RxTxApp `framebuff_cnt = 8`
- exact TSN RTP stepper
- RTP stepper resync when `cur_epochs` is non-consecutive

Current built candidate:

- `revert10` tested TSN exact RTP seed from `transmission_start_time()` instead of raw frame epoch
- result: reject this idea and keep the `revert9` RTP seed model
- reason: it reintroduced a clean `+6.633ms` phase step around frame 5 while transport stayed healthy

Removed again:

- temporary dequeue / retry / outlier TSN instrumentation
- unrelated pipeline wakeup tweaks
- LaunchTime-derived RTP re-anchor logic

## Best Checkpoints

### `revert5`

- first healthy transport baseline
- local pcap clean
- queue near `C:6 T:2`
- `lt_pkts future ~99.3%`
- analyzer still failed with stable phase error:
  - `packet_ts_vs_rtp_ts ~134.57ms`
  - `avg_tro_ns ~1.24ms`

### `revert8`

- transport still clean
- `2110_21_cinst = compliant`
- analyzer stable phase error:
  - `packet_ts_vs_rtp_ts ~124.72ms`
  - `avg_tro_ns ~24.72ms`

### `revert9`

- transport still healthy:
  - `time_to_tx` mostly positive
  - `lt_pkts future ~99.3%`
  - queue still `C:6 T:2`
  - local pcap still clean
- `2110_21_cinst = compliant`
- `inter_frame_rtp_ts_delta = 3000`
- RTP resync fix reduced the old large phase error:
  - `packet_ts_vs_rtp_ts ~14.00ms`
  - `avg_tro_ns ~14.00ms`
- `2110_21_vrx` still fails

### `revert10`

- transport still mostly healthy:
  - `time_to_tx` stayed positive
  - steady-state `lt_pkts future ~99.3%`
  - `2110_21_cinst = compliant`
  - `inter_frame_rtp_ts_delta = 3000`
- analyzer average improved, but stability regressed:
  - `packet_ts_vs_rtp_ts avg ~6.96ms`, range `~1.72ms .. 8.35ms`
  - `avg_tro_ns ~8.19ms`, range `~2.95ms .. 9.59ms`
- local pcap showed the real regression more clearly:
  - clean frames 1-4 near `TRO ~0us`
  - then one `~7.974ms` inter-frame gap at frame 5
  - frames 5+ stayed on a shifted plateau near `TRO ~6.633ms`
- conclusion: seeding TSN RTP from `transmission_start_time()` is not viable on this path

### `revert11`

- built after backing out the `revert10` RTP-seed experiment
- transport returned to a clean wire shape:
  - local pcap clean after the truncated startup frame
  - no phase step, no burst, inter-frame gap stayed `~1341-1342us`
  - local frame TRO stayed near `0us`
- analyzer moved to a new fixed plateau instead of returning to `revert9`:
  - `packet_ts_vs_rtp_ts ~21.729ms` with very tight range
  - `avg_tro_ns ~21.729ms` with very tight range
  - `2110_21_cinst = compliant`
  - `2110_21_vrx = not_compliant`
  - VRX histogram collapsed to a single bucket near `-2630`
- conclusion: the active problem remains a clean but startup-dependent absolute TSN phase / TRO offset on an otherwise healthy wire

### `revert12`

- same code path as `revert11`, but with startup-only TSN trace logs enabled
- startup trace for frames 0-5 was clean and consecutive:
  - no `late_advance`
  - TSN RTP seeded once, then stepped by exact `3000`
  - packet-0 LaunchTime stayed about one frame in the future (`delta_ptp_ns ~33.33ms`)
  - packet-0 software send stayed only `~0.3-4.6us` after target TSC
- analyzer improved sharply versus `revert9`/`revert11`:
  - `avg_tro_ns ~2.048ms`
  - `packet_ts_vs_rtp_ts avg ~3.715ms`
  - but still not compliant because of a late outlier (`packet_ts_vs_rtp_ts max ~43.33ms`, `inter_frame_rtp_ts_delta max 6000`)
- local pcap clarified the failure mode:
  - frames 1-16 stayed clean at `TRO ~0us`
  - then a late discontinuity appeared around frames 17-19
  - frame 17 had a `~35.49ms` max internal gap
  - frame 18 jumped to `TRO ~42.09ms`
  - frame 19 then followed with `dRTP = 6000` and a `~1us` inter-frame gap
- conclusion: `revert12` did not select the bad startup plateau in its first traced frames; the active defect in this run is a later one-off frame / RTP discontinuity, not the original constant startup plateau

### `revert13`

- extended trace to 40 startup frames, plus frame-done / last-packet / frame-boundary timing
- analyzer result shifted again:
  - `2110_21_cinst = compliant`
  - `inter_frame_rtp_ts_delta = 3000` exact
  - `avg_tro_ns ~10.57ms`
  - `packet_ts_vs_rtp_ts avg ~10.57ms`
  - `2110_21_vrx = not_compliant`
- local pcap showed a very specific wire pattern:
  - frame 1 starts clean near `TRO ~0us`
  - frame 6 adds `+6.633ms` from one `~7.974ms` frame-boundary gap
  - frames 6-35 stay flat near `TRO ~6.633ms`
  - frame 36 adds another `+6.633ms` from the same `~7.974ms` boundary gap
  - frames 36+ stay flat near `TRO ~13.266ms`
- traced software targets for frames 0-39 stayed normal throughout:
  - no `late_advance`
  - no RTP resync beyond the first init inside the traced window
  - `gap_rtp = 3000` always
  - target frame-boundary gaps stayed `~1.341ms`
  - packet-0 submit stayed about `33.33ms` before LaunchTime
- conclusion: the observed `+6.633ms` wire steps are not created by the traced builder / epoch / RTP schedule itself; they occur downstream of the planned frame-boundary targets

### `revert14`

- same instrumentation as `revert13`, plus continuous post-startup boundary-anomaly logging
- local pcap moved the first clean-to-shifted transition later:
  - frame 1-28 stayed clean near `TRO ~0us`
  - frame 29 added `+6.633ms` from one `~7.974ms` frame-boundary gap
  - frames 29-58 stayed flat near `TRO ~6.633ms`
  - frame 59 added another `+6.633ms` from the same `~7.974ms` boundary gap
- the key software trace change happened earlier than the first wire step:
  - packet-0 submit margin was still healthy at frame 25 (`delta_ptp_ns ~9.11ms`)
  - from frame 26 onward it collapsed to about `2.4-2.8ms`
  - `tv_sync_pacing()` still reported the frame itself being scheduled about `9.0ms` ahead at frame 26, so the lost headroom is consumed after sync and before packet-0 submit
- the new `TSN BOUNDARY ANOMALY[...]` logs did fire, but this comparison is not causal:
  - even in healthy frames, actual previous-last-submit to next-packet-0-submit spacing is only `~10-20us`, not the target `~1.341ms`
  - reason: packet-0 is always submitted much earlier relative to its LaunchTime than the previous frame's last packet
  - treat this specific boundary-gap comparison as a false-positive diagnostic, not proof of the failure point
- conclusion: `revert14` shifts the leading suspect from NIC-only LaunchTime realization to a software-side headroom collapse in the builder/transmitter path before packet-0 submit; the useful next signal is continuous packet submit headroom, not raw submit-gap mismatch

### `revert15`

- important test-method correction from the user:
  - packet capture starts only after about `20s` from app start so PTP is already stable
  - therefore the original `TSN STARTUP ...` frame-0..39 logs are not the right correlation point for the pcap/json artifacts
- analyzer and local pcap both improved sharply:
  - analyzer: `avg_tro_ns ~1.327ms`, `packet_ts_vs_rtp_ts avg ~1.327ms`, exact `dRTP=3000`, `cinst=compliant`
  - local pcap: frames 1-58 stay essentially clean near `TRO ~0us`
  - only a late small event remains near frame 59: one `~3.150ms` inter-frame gap and a plateau near `TRO ~1.795ms`
- current early-trace logs still show a real mode switch around frame 32:
  - `time_to_tx_ns` falls from `~9.8-10.0ms` to `~3.1-3.4ms`
  - packet-0 submit headroom follows it down to `~3.3ms`
  - but because this happens long before the user starts capture, it cannot be treated as the direct cause of the later pcap event
- instrumentation correction applied after this note:
  - keep startup traces for low-level debugging
  - add a delayed `TSN STABLE ...` trace window after `20s` from the first TSN sync so logs line up with the user's capture method
  - fix first-bulk enqueue tracing so frames whose first bulk briefly sat in builder inflight no longer show false `enqueue_* = 0`
- conclusion: `revert15` is much closer to a real fix on the wire, but it still does not give enough correlated steady-state evidence to justify a transport behavior change. The next run should use the new delayed stable trace window before proposing a fix.

### `revert16`

- local pcap regressed hard again even with the delayed trace build:
  - frames 1-28 stayed clean near `TRO ~0us`
  - frame 29 added the familiar `+6.633ms` step from one `~7.974ms` boundary gap
  - frame 31 then showed `dRTP = 6000` and the run fell onto a shifted plateau near `TRO ~-32.333ms`
  - the same `dRTP = 6000` pattern repeated again at frame 40
- delayed `TSN STABLE ...` traces did capture the pre-failure mode switch, even though the window still ended a few seconds before the actual packet capture:
  - stable frames 0-32 were healthy: `time_to_tx_ns ~33.2ms`, `sync_to_deq ~31.85ms`, packet-0 submit gap `~1.35ms`
  - at stable frame 33 the path changed abruptly:
    - previous frame last-packet submit was about `4.9ms` late on the TSC schedule
    - next frame first dequeue was delayed by the same amount (`enqueue_to_deq` jumped from `~31.83ms` to `~36.74ms`)
    - packet-0 submit gap collapsed from `~1.35ms` to only `~15us`
    - `time_to_tx_ns` dropped from `~33.2ms` to `~29.6ms`
    - builder enqueue stayed cheap (`~8-105us`), so the lost time is not in `tv_sync_pacing()` or the first enqueue path
- the strongest new correlation is in the later log window that overlaps the pcap failure:
  - `tv_update_tsn_rtp_time_stamp()` logged real non-consecutive epoch recovery at `16:15:10` (`51300 -> 51302`, then `51310 -> 51312`)
  - those exact RTP resyncs line up with the pcap frames where `dRTP = 6000`
- conclusion:
  - `revert16` is not a pure RTP-anchor regression; the wire failure now correlates with actual epoch skips
  - the active lag is still downstream of enqueue and before/deuring first dequeue and packet submit, with the TSN transmitter / inflight / backpressure path burning about `4.9ms` of TSC margin before the epoch-drop recovery becomes visible on the wire
  - this is enough to design a targeted pacing/transmitter fix candidate; more logs are only needed if that first fix attempt fails

Current trace build after `revert14` now adds one tighter per-frame TSN path:

- store per-frame timestamps at sync completion
- log first-bulk enqueue timing (`TSN STARTUP ENQ[...]`)
- log first-bulk dequeue timing (`TSN STARTUP DEQ[...]`)
- replace the noisy post-startup boundary-gap check with `TSN HEADROOM ANOMALY[...]` when packet-0 LaunchTime headroom drops below `5ms`
- anomaly logs now include sync→enqueue, enqueue→dequeue, and dequeue→submit spans so the next run can say whether the missing `~6ms` is spent in builder work, transmitter scheduling, or just before burst submission
- add stable-window `TSN MODE[...]` transition logs keyed on the `revert16` flip signals:
  - first-dequeue headroom relative to `sync_time_to_tx_ns`
  - previous-frame `TX_LAST` lateness
  - packet-0 submit-gap collapse
- add periodic `tsn_mode[port]` summaries so the next run shows whether the lagging state is a single transition or a persistent regime

## Current Interpretation

Solved:

- deep-residency / all-past-LT failure mode
- burst-collapse is not the active `revert9` issue
- permanent multi-frame RTP phase slip was mostly removed by RTP resync

Remaining issue:

- fixed transmit-phase / TRO error of about `14ms`
- `revert10` disproved the `transmission_start_time()` TSN RTP seed: it reduced the average
  analyzer offset but reintroduced the classic clean phase-step pattern, so the next fix should
  return to the `revert9` baseline and target actual transmit phase instead of RTP reseeding
- `revert11` strengthens that conclusion: even with a clean wire and no phase step, the analyzer can
  still land on a different constant plateau (`~21.7ms` instead of `~14.0ms`), so the remaining bug
  looks like startup-dependent absolute TSN phase selection, not RTP cadence or burst collapse
- `revert12` narrows that again: the first traced startup frames were healthy and near-zero TRO on the
  local wire, so at least this run's failure is dominated by a later one-off frame discontinuity
  (`~35.5ms` internal gap, then `TRO ~42ms`, then `dRTP=6000`) rather than by initial phase selection
- `revert13` strengthens the downstream hypothesis: in the traced 40-frame window, software frame
  targets and RTP cadence stayed exact while the wire still showed repeated `+6.633ms` frame-start
  slips. That points to late packet-0 submission or NIC-side LaunchTime realization, not a wrong
  epoch/RTP decision in the logged builder path
- `revert14` narrows that split again: the frame schedule is still exact, but packet-0 submit
  headroom collapses from about `9.1ms` to `2.5-2.8ms` around frame 26, a few frames before the
  first `+6.633ms` wire step. That strongly suggests the slip is created in software between
  `tv_sync_pacing()` and packet-0 submission, not purely inside NIC LaunchTime execution

## Next Direction

Keep the current transport shape and inspect the fixed phase between:

- `transmission_start_time()`
- `vrx` / `tr_offset`
- `lt_bias_ns`
- `tsc_time_cursor`
- `ptp_time_cursor`

Current tree now adds startup-only TSN trace logs for the first few frames at:

- `tv_init_pacing_epoch()`
- `tv_sync_pacing()`
- `tv_update_tsn_rtp_time_stamp()`
- `tv_tasklet_frame()`
- first packet-0 send in `video_trs_burst()`

Current tree also extends that trace window to the first `40` TSN frames and now logs:

- frame build completion in `tv_tasklet_frame()`
- last-packet transmit for each traced frame in `video_trs_burst()`
- packet-0 frame-boundary deltas versus the previous frame's last packet
- any later TSN RTP resync after initialization, even if it happens beyond the normal trace window

Likely next instrumentation extension:

- keep the startup logs
- stop relying on the current planned-gap vs submit-gap anomaly check; it naturally fires even on
  healthy frames because first-packet and last-packet submit lead times differ by about the full
  boundary gap
- instead, keep continuous per-frame headroom logs for:
  - packet-0 `delta_ptp_ns` / `delta_tsc_ns`
  - previous-frame last-packet `delta_ptp_ns` / `delta_tsc_ns`
  - a trigger when packet-0 headroom drops by multiple milliseconds versus the steady startup level
- use the new ENQ/DEQ/SUBMIT breakdown first before changing transport behavior; this should be the shortest path to a root-cause fix
- because the user captures only after PTP settles, prefer the delayed `TSN STABLE ...` window over the startup window when correlating with pcap/json results

Validation bar for the next change:

- clean local pcap
- `2110_21_cinst = compliant`
- `inter_frame_rtp_ts_delta = 3000`
- no burst / gap regression
- lower `packet_ts_vs_rtp_ts` and `avg_tro_ns`