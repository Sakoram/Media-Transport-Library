# TSN TX Deep Flow Analysis — Data Path, Timing, Blocking, Synchronization

## 1. Executive Summary

This document re-analyzes the full TX path for ST20 pipeline TX with `--pacing_way tsn` on E830.

### Bottom-line conclusions
- The stream is **throughput-limited**, not just timestamp-limited.
- The end-to-end system currently sustains **less than 30 fps** of *built* frames under TSN.
- The root issue is not one single hot function; it is the **composition** of:
	1. per-frame control-path overhead,
	2. per-bulk scheduler ping-pong between builder and transmitter,
	3. ring/NIC backpressure,
	4. optional NIC mode changes when LaunchTimes become past.
- **Epoch drops are not a bug by themselves**. They are the mechanism by which MTL recovers when the builder misses the next slot.
- Any attempt to “hide” epoch drops purely by changing timestamps will fail unless the system can still produce enough data to fill every wire slot.

### Strongest new insight
There is a **fundamental conservation law** here:

- wire schedule wants **exactly 30 frames worth of packets per second**,
- the current build path often produces **slightly less than that**,
- so the deficit must appear somewhere:
	- as a wire-time gap,
	- as accumulating lag,
	- or as explicit app-level frame drop/repeat/substitution.

Timestamp tricks alone cannot create missing throughput.

---

## 2. Current Log — What It Really Says

Log excerpt analyzed:

```text
MTL: 2026-03-25 14:25:21, calc_frame_count_since_epoch(0), EPOCH DROP: frame_count_tai 53233461633 > next_free 53233461632 (drop 1), prev_epoch 53233461631 cur_tai 1774448721105373676 frame_time 33333333.33
MTL: 2026-03-25 14:25:21, tv_sync_pacing(0), EPOCH DROP PACING: prev_epoch 53233461631 -> new_epoch 53233461633 time_to_tx -4138us start_tai 1774448721101235456 ptp_cursor 1774448721034568704 cur_tai 1774448721105373676
MTL: 2026-03-25 14:25:26, calc_frame_count_since_epoch(0), EPOCH DROP: frame_count_tai 53233461784 > next_free 53233461783 (drop 1), prev_epoch 53233461782 cur_tai 1774448726138526457 frame_time 33333333.33
MTL: 2026-03-25 14:25:26, tv_sync_pacing(0), EPOCH DROP PACING: prev_epoch 53233461782 -> new_epoch 53233461784 time_to_tx -3957us start_tai 1774448726134568704 ptp_cursor 1774448726034568704 cur_tai 1774448726138526457
...
TX_VIDEO_SESSION(0,0): epoch drop 2 (monotonic PTP cursor)
TX_VIDEO_SESSION(0,0): time_to_tx min -30854us max -3878us
TX_VIDEO_SESSION(0,0): drift ptp_avg +200744ns tsc_avg +200721ns per frame (n=297)
TX_VIDEO_SESSION(0,0): ttx_bands future=0 borderline=0 past=297
TX_VIDEO_SESSION(0,0): lt_pkts future=0(0.0%) past=1227170(100.0%)
TX_VIDEO_SESSION(0,0): frame_overhead avg=34us max=129us (notify_max=115us getframe_max=0us sync_max=15us) n=298
```

### Direct interpretation

#### A. Epoch drops still happen regularly
- 2 drops in the 10s stats window.
- The two shown drops are about **5.03s apart**.
- That implies the current mode is missing a frame slot about every **~151 frames**, not every ~303.

#### B. The builder is behind all the time
- `time_to_tx max -3878us`
- `time_to_tx min -30854us`
- There is **no future margin at all** for the first packet of the frame.
- The best case is still ~3.9ms late.

#### C. All LaunchTimes are already in the past
- `ttx_bands future=0 ... past=297`
- `lt_pkts future=0 ... past=1227170`
- So the NIC is never seeing future LaunchTimes in this run.
- That means the TX path has already degraded into “past-LT” behavior for the whole interval.

#### D. Inter-frame overhead did not explode
- `frame_overhead avg=34us`
- `notify_max=115us`, `sync_max=15us`, `getframe_max=0us`
- So the extra missing time is **not** primarily in `get_next_frame()` / `notify_frame_done()` / `tv_sync_pacing()`.
- The missing time is mostly elsewhere in the repeated frame build / enqueue / drain cycle.

#### E. The measured drift increased to ~200us/frame
- `drift ptp_avg +200744ns`
- Earlier “good-ish” runs showed ~109us/frame.
- Therefore the current mode is **materially slower** than the earlier one.
- Most likely reason: once LaunchTimes are always past, the end-to-end drain behavior changes enough to increase effective backpressure and per-frame completion time.

#### F. The monotonic PTP cursor is accumulating phase lag by whole frames
From the two epoch-drop logs:

1. First drop:
	 - `start_tai 1774448721101235456`
	 - `ptp_cursor 1774448721034568704`
	 - delta = **66,666,752ns ≈ 2 frame_times**

2. Second drop:
	 - `start_tai 1774448726134568704`
	 - `ptp_cursor 1774448726034568704`
	 - delta = **100,000,000ns ≈ 3 frame_times**

This is critical:
- each epoch drop lets the builder jump forward by a frame,
- but the monotonic PTP cursor only advances by one built frame,
- so the wire cursor loses roughly **one full frame of phase per drop**.

That means the monotonic cursor as currently implemented cannot remain future forever. It inevitably drifts into all-past LT.

---

## 3. End-to-End Data Path

This is the actual data path for `st20p` TX in the common pipeline case.

### Stage 0 — App gets a free frame buffer
Code path:
- `st20p_tx_get_frame()` in `lib/src/st2110/pipeline/st20_pipeline_tx.c`

Behavior:
- locks `ctx->lock`
- scans `framebuffs[]` linearly for `ST20P_TX_FRAME_FREE`
- marks buffer `ST20P_TX_FRAME_IN_USER`
- unlocks
- app fills the frame

Sync points:
- `ctx->lock` mutex
- optional `block_get` condition wait if no free frame exists

Potential cost:
- O(`framebuff_cnt`) scan under mutex
- block wait if producer is faster than transport completion

### Stage 1 — App submits the frame
Code path:
- `st20p_tx_put_frame()` in `lib/src/st2110/pipeline/st20_pipeline_tx.c`

Behavior:
- validates state is `IN_USER`
- copies user metadata if present
- transitions buffer to one of:
	- `ST20P_TX_FRAME_CONVERTED` if internal converter / derive path
	- `ST20P_TX_FRAME_READY` if external converter path
- may notify converter

Sync points:
- state mutation on shared `framebuff`
- external conversion notification if conversion is offloaded

Potential cost:
- user metadata copy
- format conversion if internal converter is used

### Stage 2 — Optional conversion stage
Code path:
- `tx_st20p_convert_get_frame()` / `tx_st20p_convert_put_frame()`
- same file: `lib/src/st2110/pipeline/st20_pipeline_tx.c`

Behavior:
- converter thread/session takes `READY`
- transitions to `IN_CONVERTING`
- after conversion transitions to `CONVERTED`

Sync points:
- `ctx->lock` mutex
- converter wake/notify interface

Potential cost:
- can be large if software conversion is active
- but current logs show app/convert are not the main bottleneck (`frame get try 298 succ 298, put 298, drop 0`)

### Stage 3 — Transport session asks pipeline for next frame
Code path:
- `tv_tasklet_frame()` in `lib/src/st2110/st_tx_video_session.c`
- callback into `ops->get_next_frame()`
- for pipeline TX this is `tx_st20p_next_frame()`

Behavior:
- session tasklet enters frame-start path when `st20_pkt_idx == 0`
- `tx_st20p_next_frame()` locks `ctx->lock`
- linearly scans for newest `ST20P_TX_FRAME_CONVERTED`
- marks it `IN_TRANSMITTING`
- unlocks
- returns frame index + metadata

Sync points:
- `ctx->lock` mutex
- linear scan of frame buffer array

Measured cost:
- `getframe_max=0-5us`
- not the dominant cost

### Stage 4 — Pacing sync / epoch assignment
Code path:
- `tv_sync_pacing()` in `lib/src/st2110/st_tx_video_session.c`

Behavior:
- reads current PTP time (`mt_get_ptp_time()`)
- reads current TSC (`mt_get_tsc()`)
- computes `cur_epochs`
- checks for epoch drop in `calc_frame_count_since_epoch()`
- computes `start_time_tai`
- computes `time_to_tx`
- initializes per-frame `tsc_time_cursor` and `ptp_time_cursor`

Sync points:
- PTP MMIO / hardware read
- epoch arithmetic is the logical synchronization with the global media schedule

Measured cost:
- `sync_max=14-15us`
- small compared to total per-frame loss

### Stage 5 — Packet builder loop
Code path:
- still `tv_tasklet_frame()`

Behavior per bulk (`bulk=4`):
- allocate 4 header mbufs
- optionally allocate chain mbufs
- build ST20 headers and payload references
- stamp TSC + PTP timestamps into mbuf private data
- advance pacing cursor for each real packet
- enqueue bulk to software ring

Important detail:
- if enqueue fails, the builder stores packets in `inflight[]`, increments `inflight_cnt`, and **returns** from the tasklet
- next scheduler pass must retry

This is a major synchronization point.

### Stage 6 — Software ring between builder and transmitter
Code path:
- ring created in `st_tx_video_session.c`
- TSN ring size forced to at least `2 * total_pkts`, rounded to power-of-2
- for 1080p30: `2 * 4115 = 8230`, rounded to `16384`

Behavior:
- builder is single-producer into ring
- transmitter is single-consumer from ring
- ring fullness is the main backpressure boundary between “build world” and “send world”

Observed evidence of permanent backpressure:
- `inflight 76663:306867` over 298 frames
- second number / frames ≈ `306867 / 298 ≈ 1029.8`
- each frame has `ceil(4115 / 4) = 1029` bulks

Interpretation:
- the enqueue path is effectively backpressured **once per bulk** on average
- the builder is not free-running; it is pace-locked to transmitter/NIC drain

### Stage 7 — TX LaunchTime transmitter tasklet
Code path:
- `video_trs_launch_time_tasklet()` in `lib/src/st2110/st_video_transmitter.c`

Behavior:
1. if `trs_target_tsc` exists, wait until target TSC is reached
2. if previous partial burst left inflight packets, retry those first
3. dequeue one bulk from software ring
4. for each packet:
	 - set LaunchTime dynfield
	 - classify future vs past LT for stats
5. if first packet’s TSC target is still in the future:
	 - save bulk in transmitter inflight array
	 - return
6. otherwise burst to NIC queue immediately

Sync points:
- TSC gate (`target_tsc`)
- ring dequeue
- NIC TX queue availability (`mt_txq_burst()`)

### Stage 8 — NIC TX queue / ICE LaunchTime scheduling
Code path:
- `video_trs_burst()` → `mt_txq_burst()`

Behavior:
- NIC is the final physical rate limiter
- with future LaunchTimes the NIC can hold packets and schedule them later
- with past LaunchTimes the NIC sends immediately / per-descriptor behavior is observed in captures

This is the **ultimate bottleneck**.

### Stage 9 — Frame completion notification back to app
Code path:
- `tv_notify_frame_done()` in `st_tx_video_session.c`
- callback for pipeline case goes to `tx_st20p_frame_done()`

Behavior:
- transport marks pipeline frame `FREE`
- invokes app `notify_frame_done`
- signals `notify_frame_available` / `block_wake_cond`

Sync points:
- `ctx->lock` mutex in `tx_st20p_frame_done()`
- app callback
- `block_wake_mutex` + `block_wake_cond`

Measured cost:
- largest measured inter-frame component
- `notify_max = 114-115us`

---

## 4. State Machines and Synchronization Points

### A. Pipeline frame state machine

```text
FREE -> IN_USER -> READY -> IN_CONVERTING -> CONVERTED -> IN_TRANSMITTING -> FREE
```

Fast paths:
- if derive/internal-converter is used:
	- `FREE -> IN_USER -> CONVERTED -> IN_TRANSMITTING -> FREE`

Synchronization edges:
- `FREE -> IN_USER`: app thread under `ctx->lock`
- `CONVERTED -> IN_TRANSMITTING`: transport tasklet under `ctx->lock`
- `IN_TRANSMITTING -> FREE`: transport completion under `ctx->lock`

### B. Session frame state machine

```text
WAIT_FRAME -> SENDING_PKTS -> WAIT_FRAME
```

Synchronization edges:
- `WAIT_FRAME -> SENDING_PKTS`: `get_next_frame()` + `tv_sync_pacing()`
- `SENDING_PKTS -> WAIT_FRAME`: last packet built for frame

### C. Builder/transmitter handshake

```text
builder bulk build
	-> enqueue to SW ring
		 -> if ring full: save inflight, return

transmitter scheduler pass
	-> dequeue bulk
	-> if first pkt target_tsc in future: hold inflight, return
	-> else tx burst to NIC

next scheduler pass
	-> builder retries next bulk
```

This handshake is the dominant flow-control loop.

---

## 5. What Consumes Time

## 5.1 Measured directly

From instrumentation:
- frame-overhead avg ≈ **34us**
- `notify_frame_done` max ≈ **115us**
- `get_next_frame` max ≈ **0-5us**
- `tv_sync_pacing` max ≈ **14-15us**
- scheduler avg loop ≈ **81ns**

### Interpretation
- The inter-frame control path is real, but it is not large enough alone to explain the whole problem.
- `notify_frame_done` is the worst inter-frame hotspot.
- `get_next_frame()` and `tv_sync_pacing()` are small.

## 5.2 Not measured directly, but strongly implied

### A. Per-bulk backpressure round-trip cost
Evidence:
- enqueue inflight count ≈ one per bulk
- ring is effectively saturated continuously

Implication:
- the builder cannot “stream-build” the whole frame in one shot
- it repeatedly stops and waits for transmitter/NIC progress
- this converts the frame build into ~1029 mini handshakes per frame

### B. NIC drain time dominates frame completion time
For 1080p30 TSN:
- packet active span ≈ **32ms**
- inter-frame gap ≈ **1.3ms**
- total wire cycle ≈ **33.3ms**

This means:
- the wire itself already consumes the full frame budget
- any extra overhead pushes frame availability into the next slot

### C. Past-LT mode likely increases effective drain latency
Current log shows:
- 100% past LT
- drops every ~5s, not ~10s
- drift ≈ 200us/frame, not ~109us/frame

Hypothesis:
- once the NIC is operating entirely in past-LT mode, the effective producer/consumer pacing gets worse,
- which increases end-to-end frame time enough to roughly double the drift rate.

This is not yet fully proven by code alone, but it is strongly consistent with logs + previous pcaps.

---

## 6. What Blocks the Flow

### Blocking / throttling point 1 — Pipeline mutex
Functions:
- `tx_st20p_next_frame()`
- `tx_st20p_frame_done()`
- `st20p_tx_get_frame()`

Type:
- mutex serialization (`ctx->lock`)

Effect:
- serializes producer thread, transport tasklet, and completion path on the same frame buffer state table

### Blocking / throttling point 2 — App callback in completion path
Function:
- `tv_notify_frame_done()` → app `notify_frame_done`

Type:
- direct callback on transport hot path

Effect:
- any app-side work here steals time from the next frame start
- this is currently the largest measured inter-frame component

### Blocking / throttling point 3 — SW ring full
Function:
- `tv_tasklet_frame()` enqueue path

Type:
- hard backpressure boundary

Effect:
- builder stops immediately when ring cannot accept the next bulk
- frame completion becomes paced by transmitter/NIC progress

### Blocking / throttling point 4 — TSC gate in transmitter
Function:
- `video_trs_launch_time_tasklet()`

Type:
- explicit scheduling gate

Effect:
- transmitter will not release a future-target bulk until the target TSC is reached
- when already behind, this gate disappears and the NIC becomes the only limiter

### Blocking / throttling point 5 — NIC TX queue / hardware scheduler
Function:
- `mt_txq_burst()` via `video_trs_burst()`

Type:
- hardware queue acceptance and wire drain

Effect:
- the true ultimate rate limiter

### Blocking / throttling point 6 — block-get condition variable (app side)
Function:
- `st20p_tx_get_frame()`

Type:
- condition wait if app wants blocking API

Effect:
- not part of the current transport bottleneck, but a real synchronization point for the producer thread

---

## 7. Why Current Fixes Failed

## 7.1 Epoch drop suppression failed because it removed recovery
Observed in czwartek21.

Reason:
- the builder was slower than the required wire cadence,
- but suppression prevented snapping forward,
- so lag accumulated unboundedly.

## 7.2 Monotonic PTP cursor only smooths wire phase — it does not create throughput
Current logs show:
- wire cursor still falls behind by whole frames,
- all packets become past-LT,
- epoch drops still occur,
- and the path remains throughput-limited.

Most important consequence:
- if one application frame slot is skipped, and you do **not** advance the wire cursor by that skipped frame time,
	you accumulate one frame of phase lag.
- if you **do** advance the wire cursor by the skipped frame time, you create a visible gap unless something is transmitted for that slot.

This is the core trade-off.

---

## 8. Fundamental Constraint: Missing Frames Must Be Paid For Somewhere

This is the deepest conclusion from the re-analysis.

If the system physically completes only 298 real frames in 10 seconds, but the wire schedule wants 300 frame slots in 10 seconds, then one of these must happen:

### Option A — Leave an empty slot / gap on wire
- current epoch-drop behavior
- builder catches up
- wire timing breaks compliance

### Option B — Accumulate lag instead of dropping
- epoch suppression / monotonic-without-recentering
- wire schedule remains smooth for a while
- eventually all timestamps become past and system collapses into permanent lag

### Option C — Maintain wire schedule by repeating/substituting content
- duplicate previous frame or send a deliberate filler frame during skipped slot
- wire timing can remain compliant
- application semantics change (content drop/repeat instead of wire gap)

### Option D — Actually reduce end-to-end cost below budget
- the only way to avoid the trade-off entirely
- means making frame production truly sustain 30fps with margin

This is why timestamp-only solutions keep failing: they choose between A and B, but not C or D.

---

## 9. Where the Real Missing Time Probably Is

Based on all evidence, the largest unmeasured chunk is here:

```text
builder bulk -> enqueue fail / ring full -> return
	next scheduler pass
transmitter dequeue -> TSC gate / NIC drain
	next scheduler pass
builder retries next bulk
```

The path is effectively synchronized per bulk by ring pressure.

### Why this matters
- `frame_overhead` only measures **frame_done -> next sync**
- it does **not** measure the repeated per-bulk waiting cost during the frame
- that is where most of the wall-clock frame time lives

### Therefore
If we want to fix throughput, the most likely leverage is not in `get_next_frame()` or `tv_sync_pacing()`, but in reducing the number/cost of builder↔transmitter↔NIC synchronization cycles.

---

## 10. Candidate Fix Directions (Ordered by Technical Plausibility)

## 10.1 Reduce true frame production cost below 33.333ms
Best outcome if possible.

Likely places:
- move `notify_frame_done` off the hot path
- reduce pipeline mutex traffic
- avoid O(n) frame scans under mutex
- reduce per-bulk handshakes / allow deeper ahead-of-time build
- reduce NIC-induced backpressure or change pacing mode

Risk:
- may still not be enough; the system already operates with almost zero slack

## 10.2 Controlled frame repeat / frame substitution on dropped epochs
If the app can tolerate content duplication, this may be the only clean compliance-preserving option.

Idea:
- when an epoch would be dropped, transmit a duplicate or filler frame in the skipped wire slot,
- then transmit the newly built frame in the next slot,
- wire stays continuous,
- builder catches up,
- app loses content fidelity instead of transport compliance.

This is the first option that satisfies the conservation law without demanding impossible throughput.

## 10.3 Phase servo / bounded cursor correction
Instead of raw epoch-based or raw monotonic cursor:
- keep a monotonic cursor,
- but apply bounded phase correction each frame to keep LT lead inside a target band.

Warning:
- this only helps if the system is close enough to sustainable.
- it does **not** solve the missing-throughput problem by itself.

## 10.4 Deeper instrumentation inside per-bulk loop
Needed if we keep optimizing software path.

Recommended next measurements:
- time from enqueue fail to next successful enqueue
- time transmitter spends with `target_tsc` pending
- time spent in `mt_txq_burst()` / partial TX behavior
- current ring occupancy over time
- number of scheduler passes per frame and per bulk

---

## 11. Proven / Disproven / Open Hypotheses

### Proven
- `notify_frame_done` is the largest measured inter-frame overhead
- `get_next_frame()` is not the dominant cost
- epoch-drop suppression does not work
- current path is throughput-limited and permanently backpressured
- timestamp manipulation alone cannot solve the deficit

### Strongly supported
- the bulk of missing time sits in repeated builder↔transmitter↔NIC synchronization, not frame-start control path
- all-past LT mode is worse than mixed/future LT mode for end-to-end drift

### Open
- can software-path optimization alone bring actual build cadence above 30fps?
- can a different NIC pacing mode avoid the per-bulk backpressure pattern?
- is controlled frame repeat acceptable for this workload?
- does a servoed cursor help enough once all-past LT is reached?

---

## 12. Practical Reading of the Current Situation

The current system is not just “a little off in timestamps.”

It is doing this:
- app provides frames fine,
- pipeline hands them over fine,
- frame-start overhead is moderate,
- builder then spends almost the entire frame budget trapped in a repeated synchronization loop with the transmitter and NIC,
- so it misses the next wire slot often enough that recovery/delay decisions become unavoidable.

That is the real problem.

---

## 13. Most Important Takeaways

1. **The data path is controlled by backpressure, not by free-running build speed.**
2. **The dominant hidden cost is per-bulk synchronization with the transmitter/NIC.**
3. **Epoch drops are a symptom of throughput deficit, not just bad timestamp math.**
4. **Suppressing or smoothing timestamps cannot manufacture missing wire slots.**
5. **A correct fix must choose one of two real strategies:**
	 - make the path actually sustain 30fps with margin,
	 - or explicitly decide how to preserve wire timing when content production cannot keep up.

