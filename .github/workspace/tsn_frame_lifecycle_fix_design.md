# TSN Frame Lifecycle Decoupling Fix Design

## Problem Summary

Latest TSN retest after the ring-size fix proved:

- builder side is no longer the first bottleneck (`build_avg=31.18/32`, `ring_stop=0`)
- TX still runs almost entirely with past LaunchTimes (`lt_pkts future=0`)
- `frame_overhead avg=33303us`, dominated by `notify_max=39859us`
- `getframe_max` and `sync_max` remain only a few microseconds
- app/pipeline reports `busy as no ready frame from user` every frame

Conclusion: frame ownership return is coupled too tightly to app callbacks.

## Root Cause

Current pipeline TX completion path is:

1. transport finishes last packet DMA for a frame
2. `tv_frame_free_cb()` invokes `tv_notify_frame_done()`
3. pipeline `tx_st20p_frame_done()` marks the frame `FREE`
4. pipeline invokes app `notify_frame_done`
5. only after that does it wake blocked `st20p_tx_get_frame()` / call `notify_frame_available`

This ordering means:

- the frame is logically free before the app callback runs
- but a blocked producer thread cannot reuse it until after the callback returns
- because callbacks run from the tasklet context, even a slow callback stretches the whole frame recycle loop
- under TSN deep buffering this becomes frame-period scale (~33ms), which is exactly what the logs show

The same ordering pattern exists in ST20/ST22/ST30/ST40 TX pipeline completion/drop paths.

## Design Goals

- unblock the next `get_frame()` as soon as the frame becomes `FREE`
- preserve current public API and callback signatures
- keep callback execution outside the pipeline lock
- keep changes minimal and low-risk for immediate testing
- generalize the fix across TX pipeline types with the same pattern

## Options Considered

### Option A — full async callback worker
- move `notify_frame_done` / `notify_frame_available` onto a separate thread or ring
- best long-term isolation from blocking apps
- but larger change: queues, lifecycle, teardown, ordering rules, more validation needed

### Option B — reorder all app callbacks before wakeups
- wake app availability callback before frame-done callback
- helps callback-driven apps too
- but changes callback ordering semantics more broadly

### Option C — immediate internal wake for blocking getters, keep app callback order
- as soon as frame status becomes `FREE`, signal the blocking `get_frame()` condition variable
- then run the app callback(s)
- leave existing `notify_frame_available` call in place afterwards
- minimal semantic change, directly targets the measured bottleneck

## Chosen Fix

Implement **Option C** now.

### Exact behavior change

For TX pipeline `frame_done` and `late_frame_drop` paths:

- after setting frame status to `FREE` and releasing `ctx->lock`
- if `ctx->block_get` is enabled, call the internal block wake helper immediately
- then run app `notify_frame_done` / `notify_frame_late`
- keep the existing `notify_frame_available` call afterwards for compatibility

### Why this is safe

- the frame has already transitioned to `FREE`
- `st20p_tx_get_frame()` rechecks state under `ctx->lock`, so early wake is race-safe
- duplicate wakeups are harmless because condition-variable wake is advisory
- app callback ordering remains unchanged relative to `notify_frame_available`
- this specifically breaks the internal dependency between frame reuse and callback return time

## Files To Change

- `lib/src/st2110/pipeline/st20_pipeline_tx.c`
- `lib/src/st2110/pipeline/st22_pipeline_tx.c`
- `lib/src/st2110/pipeline/st30_pipeline_tx.c`
- `lib/src/st2110/pipeline/st40_pipeline_tx.c`

## Expected Effect

If the hypothesis is correct, next run should show:

- much lower `frame_overhead avg`
- much lower `notify_max`
- fewer or no `busy as no ready frame from user`
- more future/borderline `time_to_tx`
- some non-zero future `lt_pkts`
- reduced likelihood of epoch drops

## Deferred Follow-ups

If this phase is insufficient, next steps are:

1. async callback/offload queue for `notify_frame_done`
2. explicit cap/servo on downstream queued depth for TSN latency control
3. queue-based pipeline free/ready tracking instead of scans
4. future-LT servo only after frame recycle latency is bounded
