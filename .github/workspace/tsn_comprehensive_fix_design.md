# TSN Narrow Compliance — Comprehensive Fix Design

## Goal

Achieve all of the following simultaneously for ST20 pipeline TX on E830:
- **ST 2110-21 narrow compliant** on wire
- **No epoch drops** in steady state
- **No unbounded lag accumulation**
- **No NIC mode-transition gaps**
- **Deterministic long-run behavior**

This design assumes the current application model must stay usable, but internal TX architecture may change.

## Status — Implemented Now

The following subset of the design has been implemented:
- TSN builder is now **work-conserving**: `tv_tasklet_frame()` can build up to 32 bulks per call.
- TSN transmitter is now **work-conserving**: `video_trs_launch_time_tasklet()` can transmit up to 32 bulks per call.
- TSN transmitter no longer does **per-bulk software TSC gating** before every LaunchTime burst.
- New TSN validation statistics were added.

The following are **not yet implemented**:
- async frame-done callback path,
- queue-based pipeline ownership replacing mutex+scan,
- future-LT servo.

So this patch is the first major throughput-oriented step, not the final architecture.

## Status Update — 2026-03-25 after first validation

The first validation run changed the hypothesis materially.

Observed log:
- `tsn_loops build_avg=4.00 ... tx_avg=5.00 ... ring_peak=480/480 ... tx_partial=76591`
- `lt_pkts future=0`
- `epoch drop 2`

### Corrected interpretation
- `ring_peak=480` is the key clue.
- This is not compatible with the intended TSN software ring of 16384.
- Root cause found in code: TSN ring sizing used `s->pacing_way[...]` **before** it was initialized.
- Therefore the TSN session silently stayed on the default ~512-entry ring.

### Impact on previous reasoning
- The previous “NIC queue is the first hard limit” hypothesis may still be partially true,
  but it was tested on a broken setup with the wrong SW ring size.
- So that conclusion was premature.
- First we must retest with the TSN software ring actually expanded.

### Fix applied
- Ring sizing now runs **after** `pacing_way[]` initialization.
- TSN should now get `ring_count = pow2(2 * total_pkts)` as originally intended.

---

## 1. Core Diagnosis

The current TSN path fails because it mixes two incompatible models:

1. **LaunchTime NIC scheduling** wants packets queued early and in batches.
2. **Software TSC pacing** throttles bulk release on the same lcore.
3. **Builder and transmitter share one scheduler thread**, so they serialize through repeated bulk-by-bulk handshakes.
4. **Pipeline control path** adds per-frame mutex/callback overhead.

Result:
- builder does not run far enough ahead,
- LaunchTimes eventually become past,
- epoch drops become necessary for recovery,
- epoch drops create wire discontinuities,
- or suppression creates unbounded lag.

### Root cause in one sentence
The TX path is currently **software-paced at bulk granularity even though hardware LaunchTime exists**, and that destroys the headroom needed to avoid epoch drops.

---

## 2. Required Strategy Change

The fix is to move from:

- **bulk-by-bulk software pacing with hardware timestamps attached**

to:

- **hardware-scheduled transmission with software building ahead of time**

That means:
- NIC LaunchTime becomes the primary pacing mechanism,
- software TSC gating is reduced to a coarse safety function or removed from the hot path,
- builder must be allowed to run ahead in larger chunks,
- frame lifecycle callbacks must be decoupled from the real-time transport path.

---

## 3. The Comprehensive Fix

## 3.1 Replace TSN per-bulk TSC gating with LaunchTime-first transmission

### Current problem
`video_trs_launch_time_tasklet()` still does a TSC gate on the first packet of each bulk.
That forces:
- one dequeue per scheduler turn,
- one bulk held/retried per scheduler turn,
- strong coupling between builder and transmitter,
- permanent ring-pressure ping-pong.

### Fix
For TSN mode, transmit to NIC **as early as possible** as long as LaunchTimes remain sufficiently future.

### New rule
For TSN:
- do **not** gate each bulk on `st_tx_mbuf_get_tsc(pkts[0])`
- instead use only a **coarse future-window guard** based on PTP LaunchTime

Example policy:
- define `lt_guard_min_ns` = minimum safe future lead, e.g. 200us to 500us
- define `lt_guard_max_ns` = maximum queue-ahead window, e.g. 1 to 2 frame_times
- transmitter may burst any packet whose LaunchTime satisfies:
  - `target_ptp >= cur_ptp + lt_guard_min_ns`
  - and queue depth / ahead window under limit

Practical effect:
- packets are given to NIC in large bursts,
- NIC holds them until LaunchTime,
- software no longer burns scheduler turns waiting at bulk granularity.

### Why this is safe
E830 LaunchTime already performs the actual wire scheduling.
The TSC gate is redundant for on-wire pacing when LaunchTimes are future.

---

## 3.2 Make the builder work-conserving in TSN mode

### Current problem
`tv_tasklet_frame()` builds only one bulk per handler call.
With `bulk=4`, that means ~1029 scheduler round-trips per frame.

### Fix
For TSN mode, change frame building from:
- **one bulk per tasklet call**

to:
- **build-until-watermark / build-until-frame-done in one call**

### New builder loop
Inside `tv_tasklet_frame()` for TSN:
- after frame start, enter a bounded internal loop
- continue building bulks while all are true:
  - current frame not done,
  - software ring occupancy below high watermark,
  - mempool alloc succeeds,
  - per-call bulk budget not exceeded

Recommended budget:
- `tsn_build_burst_bulks = 32` initially
- tune upward to 64/128 if safe

### Why this matters
If each handler call builds 32 bulks instead of 1:
- scheduler round-trips per frame drop from ~1029 to ~33
- a large portion of the hidden synchronization cost disappears
- the builder can finally run ahead of the transmitter

---

## 3.3 Make the transmitter work-conserving in TSN mode

### Current problem
TSN transmitter also effectively processes one bulk per call.

### Fix
For TSN mode, change `video_trs_launch_time_tasklet()` from single-bulk to looped burst mode:
- dequeue and burst multiple bulks in one invocation
- stop only when:
  - ring empty,
  - NIC queue backpressures,
  - next packet violates future-window guard,
  - per-call budget exceeded

Recommended budget:
- `tsn_tx_burst_bulks = 32` initially

### Result
Builder and transmitter both become work-conserving.
The scheduler stops being the dominant per-bulk synchronization mechanism.

---

## 3.4 Remove app callback from the transport hot path

### Current problem
`tv_notify_frame_done()` calls app `notify_frame_done()` synchronously.
Measured max: ~115us.
This is the single largest measured inter-frame cost.

### Fix
Introduce a **frame-done completion queue**:
- transport pushes completed frame index + meta to a lock-free completion ring
- separate control thread or existing non-real-time context executes callbacks
- wake app from there

### Rules
- hot path does only:
  - mark frame completed,
  - enqueue completion record,
  - signal lightweight event if needed
- app callback never runs on scheduler real-time transport path

### Expected gain
- remove ~34us average inter-frame overhead from critical path
- reduce jitter from app-side callback behavior

---

## 3.5 Replace pipeline frame-state scans with SPSC free/ready queues

### Current problem
Pipeline uses mutex-protected frame array scans for:
- free frame lookup
- converted frame lookup
- completion return

This is not the dominant cost today, but it is unnecessary control-path overhead and contention.

### Fix
Replace state-scan model with explicit queues:
- `free_ring`: frames available to app
- `ready_ring`: frames converted and ready for transport
- optional `done_ring`: completion notifications

### State model
App side:
- pop from `free_ring`
- fill frame
- push to conversion/ready path

Transport side:
- pop from `ready_ring`
- build/transmit
- on completion push back to `free_ring` indirectly through done handler

### Benefits
- no O(n) scans
- no transport/app mutex sharing on hot path
- easier ahead-of-time buffering

---

## 3.6 Increase in-flight frame depth explicitly

### Current problem
Even if packet rings are large, frame-level depth may still be too shallow.
If only 1-2 frame buffers are truly usable, the app/transport cannot absorb jitter.

### Fix
For TSN narrow mode, require:
- pipeline frame buffer count >= 4
- preferred >= 6 for margin

### Why
Need simultaneous room for:
- app currently filling frame
- converter frame
- frame being packetized / queued to NIC
- one or more future frames already ready

Without frame-level depth, packet-level ahead-of-time building still stalls on frame ownership.

---

## 3.7 Use future-window servo, not epoch servo

### Current problem
Epoch-based cursor logic creates binary behavior:
- either snap to current epoch and risk gaps,
- or stay monotonic and accumulate phase error.

### Fix
In TSN mode, control a **future LaunchTime lead window**, not strict epoch identity.

### Target window
Maintain first-packet LT lead approximately in:
- min: 0.5ms
- target: 4ms to 10ms
- max: 1 frame_time

### Control logic
At frame start:
- compute `lt_lead = ptp_cursor_start - cur_ptp`
- if lead below target band, advance `ptp_cursor_base` faster for one frame
- if lead above target band, hold normal advancement

This is a bounded phase servo on top of LaunchTime scheduling.

### Important constraint
This servo is only valid **after throughput has been fixed** by Sections 3.1–3.4.
It is a stabilizer, not a primary cure.

---

## 3.8 Keep epoch drops as an emergency-only statistic, not normal behavior

### Desired behavior
After the throughput fix:
- `stat_epoch_drop == 0` in steady-state runs
- if nonzero, it should indicate a real overload or app starvation event

### Meaning
“No epoch drops” becomes realistic only once the path actually sustains 30fps with headroom.
It cannot be enforced by timestamp math alone.

---

## 4. Concrete Implementation Plan

## Phase 1 — Remove bulk-granularity scheduler ping-pong

### Code changes
1. `lib/src/st2110/st_tx_video_session.c`
   - TSN-only internal loop in `tv_tasklet_frame()`
   - build multiple bulks per invocation until ring watermark or frame done

2. `lib/src/st2110/st_video_transmitter.c`
   - TSN-only loop in `video_trs_launch_time_tasklet()`
   - burst multiple bulks per invocation
   - replace per-bulk TSC gate with coarse future-window PTP guard

### Success criteria
- `inflight_cnt / frames` drops dramatically below ~1029
- `drift ptp_avg` drops well below 100us/frame
- `time_to_tx` starts showing future margin again
- `lt_pkts future` becomes dominant again

---

## Phase 2 — Decouple frame completion callback

### Code changes
1. `st_tx_video_session.c`
   - replace direct callback in `tv_notify_frame_done()` with enqueue-to-completion-ring
2. pipeline TX layer
   - completion consumer thread/callback worker

### Success criteria
- `frame_overhead avg` drops materially below 34us
- `notify_max` removed from transport critical path

---

## Phase 3 — Replace pipeline scan/mutex path with rings

### Code changes
1. `lib/src/st2110/pipeline/st20_pipeline_tx.c`
   - free/ready queue architecture
   - remove hottest frame-state scans from transport interaction path

### Success criteria
- `get_next_frame` cost becomes near-constant and tiny
- less lock contention between app and transport

---

## Phase 4 — Add LT lead servo

### Code changes
- TSN-only lead controller in `tv_sync_pacing()`
- maintain first-packet future LT target band

### Success criteria
- `ttx_bands future` stable high percentage
- no mode flips due to drift cycle
- narrow compliance stable over longer runs

---

## 5. Why This Design Can Actually Eliminate Epoch Drops

Epoch drops happen because the frame production path misses the next slot.
This design attacks the actual causes:

### It removes waste from the hot path
- no synchronous app callback on completion
- fewer mutex/scan interactions

### It removes artificial scheduler serialization
- no one-bulk-per-loop builder
- no one-bulk-per-loop transmitter

### It uses the NIC the way LaunchTime hardware wants to be used
- queue ahead in bursts
- let hardware schedule wire timing
- stop software from micro-pacing what hardware already paces better

If this works, steady-state frame completion time becomes:
- build/copy/reference cost + occasional queue pressure
- instead of build cost + ~1000 scheduler handshakes + callback stalls

That is the only plausible path to true zero-drop steady-state.

---

## 6. Risks and Trade-offs

### Risk 1 — NIC queue saturation
Bursting too aggressively may overflow effective NIC queue headroom.
Mitigation:
- use bounded burst budget
- use future-window guard
- monitor partial TX and queue occupancy

### Risk 2 — Larger ahead-of-time buffering increases latency
True.
But latency is already intentionally increased by LT bias.
The goal here is compliance and continuity.

### Risk 3 — Application callback order/latency changes
Async completion changes callback timing semantics slightly.
Need documented guarantee:
- completion callback means frame is reusable,
- not “callback executed on same scheduler turn as TX completion.”

### Risk 4 — This may still be insufficient if actual build cost is too high
If after removing scheduler ping-pong and callback overhead the path still misses 30fps,
then software optimization alone is not enough.
At that point the only compliance-preserving fallback is **frame substitution/repeat**.

---

## 7. Fallback Design if Throughput Still Misses Budget

If zero-drop steady-state remains impossible after Phases 1–4:

### Controlled wire-slot preservation mode
On would-be epoch drop:
- transmit a duplicate previous frame or explicit filler frame for the missing slot
- keep LaunchTime continuity exact
- deliver the newly built frame on the next slot

This preserves:
- narrow wire compliance
- continuous packet schedule
- no VRX cliff

But sacrifices:
- perfect application content cadence

This is still better than random wire gaps if transport compliance is the top priority.

---

## 8. Recommended Success Metrics

A design is only accepted if all are met in a 60s run:

### Transport metrics
- `epoch drop == 0`
- `fps >= 29.999`
- `time_to_tx` mostly positive or mildly future for frame starts
- `lt_pkts future` dominant, ideally >95%
- `frame_overhead avg < 10us`

### Pcap metrics
- CINST narrow compliant for full capture
- VRX within `[0,8]`
- no multi-ms inter-frame gaps
- no transition from good phase to bad phase

### Stability metrics
- no drift growth trend over 60s
- no callback-induced jitter spikes
- no unbounded ring-pressure oscillation

## 8.1 New Validation Logs Added by This Patch

The patch adds a new periodic log line:

```text
TX_VIDEO_SESSION(m,i): tsn_loops build_avg=X.YY build_max=A tx_avg=B.CC tx_max=D ring_peak=E/F ring_stop=G tx_partial=H
```

### Meaning
- `build_avg` — average bulks built per `tv_tasklet_frame()` call
- `build_max` — max bulks built in one builder call
- `tx_avg` — average bulks transmitted per TSN transmitter call
- `tx_max` — max bulks transmitted in one transmitter call
- `ring_peak=E/F`
   - `E` = max SW ring occupancy seen by builder
   - `F` = max SW ring occupancy seen by transmitter
- `ring_stop` — number of times builder stopped because ring headroom was low
- `tx_partial` — number of partial `tx_burst()` events in TSN transmitter

### What “good” should look like
- `build_avg` should rise well above `1.00`
- `tx_avg` should rise well above `1.00`
- `ring_stop` should drop relative to old behavior
- `lt_pkts future%` should increase
- `drift ptp_avg` should decrease materially from ~200us/frame toward or below ~109us/frame
- `time_to_tx max` should move toward zero and ideally positive territory

### What “bad” still looks like
- `build_avg ≈ 1.00`, `tx_avg ≈ 1.00` → patch did not break scheduler ping-pong
- `ring_stop` still very high → builder still hard-backpressured
- `tx_partial` very high → NIC queue remains dominant choke point
- `lt_pkts future=0` → LaunchTimes are still entirely past

---

## 9. Final Recommendation

The comprehensive fix is:

1. **Make TSN builder and transmitter work-conserving**
2. **Stop TSC-gating each TSN bulk**
3. **Rely on LaunchTime hardware as the primary pacer**
4. **Move frame-done callbacks out of the real-time path**
5. **Replace pipeline scan/mutex handshakes with queue-based ownership**
6. **Add a bounded future-LT servo only after throughput is fixed**

This is the only design that has a credible path to achieving both:
- **narrow compliance**, and
- **zero epoch drops**

without relying on mathematically impossible timestamp-only tricks.
