# TSN on E830: Why ST 2110-21 Compliance Is Hard

## Status

After implementing a TSC floor gate and an RTP timestamp bias fix, the TSN transmitter achieves **narrow cinst compliance** and **correct inter-frame RTP timing** (no dropped frames, no RTP resyncs). Inter-packet spacing on the wire averages 6.9 µs, close to the ideal 7.8 µs for 1080p30. The `packet_ts_vs_rtp_ts` and `vrx` metrics had a 14–28 ms offset caused by a missing `lt_bias_ns` term in the RTP seed — fix applied.

## Why TSN on E830 Is Harder Than RL or TSC Pacing

All three pacing modes (RL, TSC, TSN) share the same software transmitter loop: dequeue a bulk of 4 packets, gate on TSC, burst to NIC. **None have a software safety net for scheduler stalls** — when the scheduler is late, all three burst accumulated packets in rapid 4-packet bulks at wire rate. During stalls, TSN with the TSC floor gate actually degrades to **the same or better** accuracy as TSC (the floor gate enforces `bulk × trs` spacing between bursts, while TSC relies only on implicit tasklet re-entry overhead). RL mode is immune because the NIC hardware rate limiter enforces on-wire spacing regardless.

The real TSN-specific challenge is **absolute timing**: TSN requires correct PTP timestamps on every packet, meaning the software must maintain a precise relationship between three clocks (PTP, TSC, RTP) across frame boundaries. RL/TSC only need correct *relative* spacing. Any misalignment (LaunchTime bias, epoch rounding, clock drift) shifts entire frames in time, causing `packet_ts_vs_rtp_ts` and `vrx` failures.
