# TSN VRX Compliance Analysis — Root Cause & Solution

## Date: 2025-03-25 (updated 2026-03-25)

## Problem Statement
With `--pacing_way tsn` on E830 NIC, the EBU LIST analyzer reports:
- VRX: not_compliant (VRX=-3817, constant offset)
- CINST: wide (was narrow, degraded to Cinst=0..8)
- packet_ts_vs_rtp_ts: not_compliant (363ms offset)
- On-wire timing is PERFECT (per-packet spacing = ideal TRS, no bursts)

## Phase 1: 363ms RTP Drift (FIXED)

### Root Cause
Epoch drop suppression in `calc_frame_count_since_epoch()` caused `ptp_time_cursor`
to drift unboundedly behind wall-clock time. The builder took ~32ms/frame with
33.33ms period → only 1.33ms slack. Any transient delay accumulated without recovery.

### Fix
Removed epoch drop suppression → always use `frame_count_tai` when behind (Option A).
Verified: 363ms drift eliminated.

---

## Phase 2: Current State (czwartek2 capture, 2025-03-27)

### Analyzer Results
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| CINST | narrow [0,1] | narrow | **PASS** |
| VRX | -1843 (89.5%), -990 (10.5%) | [0, 720] | FAIL |
| pkt_ts_vs_rtp_ts | 7.7ms–14.4ms | 0–1ms | FAIL |
| fps | ~29.9 | 30 | FAIL (1 epoch drop/10s) |

### Pcap Hard Data (mypcap-czwartek2, ~1s capture, 30 frames)

**On-wire timing: PERFECT**
- Inter-frame: 33.333ms ±0.001ms (stdev=0.001ms) — except one anomaly
- Per-packet: avg=7776ns, min=5960, max=9060, std=440ns
- Frame duration: 31.992ms consistently
- Inter-frame idle gap: 1.341ms (= 33.333 - 31.992)

**RTP: 3000 diff between all consecutive frames** (correct for 30fps exactly)

**One anomalous gap at frame 3→4:**
- Gap: 39.967ms (expected 33.333ms, excess: 6.634ms)
- gap_from_prev_last: 7.975ms (normal: 1.341ms, excess: 6.634ms)
- After this gap: all subsequent frames shifted +6.634ms

**Builder lag (capture_time − RTP_as_TAI):**
- Frames 1–3: 7.744ms behind (constant)
- Frames 4–29: 14.377ms behind (constant, shifted by anomaly)
- Range: 6.634ms = exactly one anomaly's worth

**VRX values map directly to lag:**
- VRX=-990 = 990 × 7.776µs = 7.70ms ← frames 1–3 lag
- VRX=-1843 = 1843 × 7.776µs = 14.33ms ← frames 4–29 lag

### Key Insight: Builder Lag Is Stable, Not Growing

Within each segment (pre-anomaly and post-anomaly), the lag is **constant**.
This means:
- Build duration ≈ frame_time − idle_gap = 33.333 − 1.341 = 31.992ms
- Builder keeps up: each frame takes exactly one frame period
- But the **initial offset** after an epoch drop persists forever

### The Anomaly: What Causes the 6.6ms Stall?
Between frame 3 (last pkt at .007636) and frame 4 (first pkt at .015611),
there is a 7.975ms gap instead of 1.341ms. Possible causes:
- Scheduler preemption (another tasklet or interrupt)
- PTP clock correction
- NIC driver latency spike (interrupt, DMA)
- TX ring contention

**This stall is cumulative**: if it happens ~5 times, total lag reaches
~33ms → epoch drop. At 1/10s rate, explains consistent "epoch drop 1" per 10s.

### Why pkt_ts_vs_rtp_ts Fails
- RTP is derived from `ptp_time_cursor` = `transmission_start_time()`
- `ptp_time_cursor` = N × frame_time + tr_offset − vrx × trs
- When builder is 7.7ms behind: `ptp_time_cursor` is 7.7ms in the past
- NIC sends immediately (LaunchTime past) → capture at "now"
- delta = now − ptp_time_cursor = 7.7ms >> 1ms limit

### Why VRX Fails (Same Root Cause)
The analyzer computes VRX from RTP epoch reference. Packets arrive 7.7ms
before their RTP-implied schedule → VRX = -990. After stall: -1843.

---

## Phase 3: czwartek3 — NIC Bulk Bunching Discovery (2025-03-25)

### Key Finding: E830 NIC LaunchTime Behavior Depends on Past vs Future

**czwartek3** (captured right after epoch drop reset, builder on-time):
| Metric | Value | Status |
|--------|-------|--------|
| CINST | wide [0,3,9] | FAIL |
| VRX | 5-8 | FAIL |
| pkt_ts_vs_rtp_ts | 5.7-6.7µs | **PASS** |
| Inter-frame gap | 33.333ms ± 0.5µs | PERFECT |
| Per-frame delta | 0.006ms (constant) | PERFECT |
| Per-frame drift | 0.0µs ± 0.5µs | ZERO |

**Per-packet pattern (bulk=4 bunching):**
```
pkt 0→1:     0ns (simultaneous)
pkt 1→2:   954ns (PCIe descriptor gap)
pkt 2→3:     0ns
pkt 3→4: 30041ns (inter-bulk gap = 4×TRS - 3×small)
```
- 47.2% gaps = 0ns (same-bulk)
- 27.8% gaps = ~954ns (intra-bulk)
- 25.0% gaps = ~30041ns (inter-bulk)

**czwartek2** (captured mid-cycle, builder 7ms behind):
```
ALL 4114 gaps = 6914-8106ns range → 100% per-packet TRS pacing
```
- 0% gaps = 0ns

### E830 NIC LaunchTime Behavior Model (HARD DATA)

| LaunchTime state | NIC behavior | On-wire result |
|-----------------|--------------|----------------|
| **Future** (time_to_tx > 0) | Batches per doorbell-ring (bulk=4) | 4 pkts burst, then wait → CINST=wide |
| **Past** (time_to_tx < 0) | Processes per-descriptor sequentially | ~TRS spacing per pkt → CINST=narrow |

### The Drift Cycle (121µs/frame)

Stats: `time_to_tx min -30814us max 5337us` over 299 frames (10s).

| Phase | Frame range | time_to_tx | NIC mode | Compliance |
|-------|-------------|-----------|----------|------------|
| After epoch drop | 0-44 | +5337 → 0µs | Future LTs | VRX=5-8, CINST=wide |
| Mid-cycle | 44-275 | 0 → -28000µs | Past LTs | VRX=neg, CINST=narrow |
| Epoch drop | ~275 | < -33333µs | — | fps drops to 29.9 |

Drift rate: **121µs/frame** (consistent across two 10s periods).
Builder cycle time: 33.333ms + 121µs = 33.454ms.

### Implication: NO Operating Point Satisfies All Criteria (without fix)

- Future LaunchTimes → good RTP, bad CINST (bulk=4 NIC bunching)
- Past LaunchTimes → good CINST, bad RTP (builder behind wall clock)
- The drift cycles through both regimes

---

## Phase 4: Drift Root Cause — DEFINITIVE (2025-03-25)

### Instrumented Data (two 10s periods)
```
Period 1: drift ptp_avg +109290ns tsc_avg +109253ns per frame (n=298)
Period 2: drift ptp_avg +110469ns tsc_avg +110432ns per frame (n=298)
```

### Conclusion: Real Wall-Clock Overhead (NOT Clock Drift)
- PTP and TSC agree to within **37ns** → **not a clock domain mismatch**
- **110µs of real processing overhead per frame**
- Accumulated across 1029 bulk iterations (scheduler loop, ring, NIC TX queue)

### Overhead Budget

| Source | Calculation | Est. |
|--------|-----------|------|
| TSC gate overshoot | 1029 × 38ns avg | 39µs |
| NIC TX inflight retries | 1029 × 55% × 76ns | 43µs |
| Ring-full retries | 1029 × 55% × 76ns | 43µs |
| Frame transition | get_next + sync_pacing + alloc | 5-10µs |
| **Total (with overlap)** | | **~110µs** |

### Epoch Drop Prediction
- drift/frame = 110µs, frame_time = 33.333ms
- frames_to_drop = 33333/110 = **303 frames (10.1s)** → matches observed!

---

## Fix Attempt 1: TSC Compensation (czwartek6, 2025-03-25) — WRONG APPROACH

### Hypothesis
Builder cycle = frame_time + 110µs. After 303 frames, lag > frame_time → epoch drop.
If we shorten TSC steps by 120µs/frame, builder finishes faster → stays on-time → 
LaunchTimes stay in future → everything works.

### Implementation
Decouple TSC cursor (builder pacing) from PTP cursor (on-wire LaunchTime):
- **PTP cursor**: unchanged, advances by Bresenham per packet (controls NIC LaunchTime)
- **TSC cursor**: advances by Bresenham − compensation per packet (controls builder speed)
- Compensation = 120µs/frame distributed across 4115 packets via second Bresenham.

### Measured Results (czwartek6)
```
drift ptp_avg +109290ns tsc_avg +109253ns per frame (n=298)  # UNCHANGED
frame_duration=31.857ms (vs czwartek3=31.977ms)
idle_gap=1.476ms (vs czwartek3=1.356ms)
TOTAL CYCLE UNCHANGED AT 33.333ms
```

### Analyzer Results (czwartek6)
| Metric | Value | Status |
|--------|-------|--------|
| Overall | **WIDE COMPLIANT** | ✓ (but wide, not narrow) |
| CINST | wide [0–10] | wide ✓ |
| VRX | wide [0–23] | wide ✓ |
| pkt_ts_vs_rtp_ts | 5.5–6.7 µs | **PASS** ✓ |
| inter_frame_rtp_ts_delta | 3000 exact | **PASS** ✓ |
| TRO | 1.24 ms (≈ default 1.274ms) | ✓ |
| rate | 30.000 | ✓ |

### Why TSC Compensation Had No Effect on Drift
- NIC TX ring backpressure is the real rate limiter, not TSC gating
- Making TSC steps shorter just shifts where builder waits:
  - Before: wait at TSC gate
  - After: wait at ring-full retry
- **Total cycle = NIC-limited ≈ 33.333ms regardless of TSC speed**
- The 110µs overhead confirmed: frame_duration shortened 31.977→31.857ms,
  but idle gap grew correspondingly (1.356→1.476ms)

### Key Observation: czwartek6 = Wide-Compliant Baseline
- CINST=wide because LaunchTimes are in the future → E830 batches per doorbell
- RTP/VRX/pkt_ts_vs_rtp_ts all pass because epoch logic is unmodified
- **This is the best result so far** — full compliance but only at wide level

### Conclusion
TSC compensation is **irrelevant** — NIC controls total cycle. Removed.

---

## Fix Attempt 2: Epoch Suppression + Wall-Clock RTP (czwartek7, 2025-03-25) — FAILED

### Hypothesis
From czwartek2 data: when builder is behind (past LaunchTimes), CINST=narrow.
From czwartek3 data: when builder is on-time (future LaunchTimes), CINST=wide.
**Idea**: Suppress epoch drops (let builder fall unboundedly behind for CINST narrow)
and derive RTP from wall-clock `cur_tai` instead of drifting `ptp_time_cursor`
(to fix VRX + pkt_ts_vs_rtp_ts).

### Implementation
1. `calc_frame_count_since_epoch()`: TSN branch uses `next_free_frame_slot` (no epoch drops)
2. `tv_sync_pacing()`: store `cur_tai` as `pacing->tsn_wall_clock_tai`
3. `tv_update_rtp_time_stamp()`: use `tsn_wall_clock_tai` instead of `ptp_time_cursor`

### Measured Results (czwartek7)
```
epoch drop 4280 (accumulated stat — builder fell ~4280 frames behind)
time_to_tx min -531066us max -464622us  # 464-531 ms behind = ~15 FRAMES
drift ptp_avg +223459ns tsc_avg +223430ns per frame (n=297)
fps 29.799938
```

### Analyzer Results (czwartek7)
| Metric | czwartek6 (baseline) | czwartek7 (this fix) | Status |
|--------|---------------------|---------------------|--------|
| Overall | wide compliant | **NOT COMPLIANT** | ✗ REGRESSION |
| CINST | wide [0–10] | **narrow [0,1]** ← 98.85% at 0 | ✓ IMPROVED |
| VRX | wide [0–23] | -2893 to -1958 | ✗ BROKEN |
| pkt_ts_vs_rtp_ts | 5.5–6.7 µs | 7.2–13.9 **ms** | ✗ BROKEN |
| inter_frame_rtp_ts_delta | 3000 exact | 2997–3595 (jitter) | ✗ BROKEN |
| TRO | 1.24 ms | **18.6 ms** | ✗ BROKEN |
| rate | 30.000 | 30.020 | ✗ |

### Root Cause of Failure
Epoch drop suppression lets builder fall **unboundedly behind** (400–530ms = ~15 frames).
Using `cur_tai` for RTP completely breaks epoch alignment:
- RTP timestamp says "now" but frame was built for an epoch 15 frames ago
- `cur_tai` jitters by ±hundreds of µs → inter_frame_rtp_ts_delta has variance
- TRO computed from RTP is 18ms instead of 1.2ms
- VRX is massively negative because packets arrive "way before" their RTP-implied time

### What czwartek7 Proved (Positive Finding)
**CINST = narrow (98.85% at bucket 0)** when LaunchTimes are in the past.
This definitively confirms the E830 dual-mode model from Phase 3.

### Conclusion
Cannot suppress epoch drops — it destroys all RTP-based compliance metrics.
The RTP/epoch machinery must stay untouched (czwartek6 had it right).

---

## E830 NIC LaunchTime Behavioral Model (Confirmed)

### Hard Data Summary

| Capture | Builder state | LaunchTimes | CINST | VRX | pkt_ts_vs_rtp_ts |
|---------|--------------|-------------|-------|-----|-------------------|
| czwartek3 | on-time | future | wide [0-10] | wide [5-8] | compliant (6µs) |
| czwartek2 | 7-14ms behind | past | **narrow [0,1]** | fail (-990/-1843) | fail (7-14ms) |
| czwartek6 | on-time | future | wide [0-10] | wide [0-23] | compliant (6µs) |
| czwartek7 | 400-530ms behind | past | **narrow [0,1]** | fail (-2893) | fail (7-14ms) |
| czwartek8 | 20ms behind | past (bias) | **narrow [0,1]** | fail (-760/-1613) | fail (6-13ms) |

### Model
```
IF LaunchTime > NIC_current_time (future):
    NIC batches pkts per doorbell-ring (bulk=4)
    → CINST = wide, but RTP/VRX/pkt_ts_vs_rtp_ts = COMPLIANT
ELSE (past):
    NIC sends per-descriptor immediately
    → CINST = narrow, but RTP/VRX/pkt_ts_vs_rtp_ts = BROKEN (if builder drifted)
```

### The Dilemma
- Future LTs → good RTP metrics, bad CINST (wide, never narrow)
- Past LTs via builder drift → good CINST, bad RTP metrics
- No operating point satisfies ALL criteria simultaneously... **unless we decouple**

---

## Fix Attempt 3: LaunchTime Past-Bias (czwartek8, 2026-03-25) — FAILED

### Hypothesis
Create past LaunchTimes by subtracting `frame_time` from LT in the transmitter,
without letting the builder drift. NIC sees past timestamps → per-descriptor → narrow CINST.
RTP/epoch logic stays as czwartek6 → compliant.

### Implementation
In `video_trs_launch_time_tasklet()`: `launch_time = target_ptp - frame_time`

### Measured Results (czwartek8)
```
epoch drop 1 per 10s (same as czwartek6)
time_to_tx min -26087us max 5337us (same range as czwartek6)
drift ptp_avg +111858ns tsc_avg +111834ns per frame (n=298)
```

### Analyzer Results (czwartek8)
| Metric | czwartek6 (baseline) | czwartek8 (LT bias) | Status |
|--------|---------------------|---------------------|--------|
| Overall | **wide** | NOT COMPLIANT | ✗ REGRESSION |
| CINST | wide [0–10] | **narrow [0,1]** 98.87% at 0 | ✓ IMPROVED |
| VRX | wide [0–23] | -1613 (9.7%), -760 (90.3%) | ✗ |
| pkt_ts_vs_rtp_ts | 5.5–6.7 µs | 5.96–12.59 **ms** | ✗ |
| inter_frame_rtp_ts_delta | 3000 exact | **3000 exact** | ✓ same |
| TRO | 1.24 ms | 7.9 ms avg | ✗ |
| rate | 30.000 | 30.000 | ✓ same |

### CRITICAL DISCOVERY: pcapng_dump is RX-side with HW timestamps

The pcap is captured at the **RECEIVER** NIC using hardware PTP timestamps.
This means pcap timestamps = actual on-wire arrival times, NOT builder submission times.

**This explains everything:**

| LaunchTime mode | NIC behavior | On-wire time | pkt_ts_vs_rtp_ts |
|-----------------|--------------|--------------|-------------------|
| Future LT | NIC **holds** until LT → sends at LT | = LaunchTime | ≈ 0 (epoch-aligned) ✓ |
| Past LT (bias) | NIC sends **immediately** | = builder submit time | = builder lag ✗ |

- **czwartek6** (future LTs): NIC held packets until LaunchTime → on-wire at epoch time → 5-7µs ✓
- **czwartek8** (past LTs via bias): NIC sent immediately → on-wire at builder time (20ms behind) → 6-12ms ✗

### Why the bias CANNOT work
The LaunchTime bias forces ALL LaunchTimes into the past, which causes the NIC to
send at builder submission time instead of the intended epoch time. The NIC's
LaunchTime mechanism IS the timing control — bypassing it destroys timing compliance.

### Per-packet data from czwartek8
```
Per-packet gap histogram:
  TRS-like (3-15µs):   116660 (100.0%)   ← NARROW CINST confirmed
  Mean: 7776 ns  Stdev: 451 ns
  Min: 5960 ns  Max: 9060 ns
  
Drift: constant -20.576ms (builder 20ms behind during capture)
RTP delta: all exact 3000 ticks
One anomaly at frame 26→27: 7.97ms stall (same as czwartek2)
```

---

## Fix Attempt 4: bulk=1 for TSN (2026-03-25) — IN PROGRESS

### Root Cause: NIC Batches Per-Doorbell, Not Per-Descriptor

The E830's CINST-wide behavior is caused by **4 descriptors per doorbell ring**.
With `bulk=4`, the transmitter dequeues 4 packets and submits them in one
`rte_eth_tx_burst()` → one doorbell → NIC sees 4 descriptors with future LaunchTimes
→ batches all 4 at the first LT → CINST wide.

Evidence from czwartek3 (future LTs, bulk=4):
```
pkt 0→1:     0ns (simultaneous)  ← same doorbell batch
pkt 1→2:   954ns (PCIe gap)      ← same doorbell batch
pkt 2→3:     0ns (simultaneous)  ← same doorbell batch
pkt 3→4: 30041ns (inter-bulk)    ← next doorbell batch
```

### Hypothesis: bulk=1 Gets Narrow CINST with Future LaunchTimes

With `bulk=1`:
- Transmitter dequeues 1 packet → 1 descriptor + 1 doorbell
- NIC processes 1 descriptor → has only 1 packet to send
- NIC waits until LaunchTime → sends that single packet → **narrow CINST**
- **AND** NIC still controls on-wire timing → pkt_ts = LaunchTime → **compliant pkt_ts_vs_rtp_ts**

### Implementation
```c
// In tv_init_pacing(), TSN branch:
s->bulk = 1;  // was: pacing->vrx -= (s->bulk - 1);
```

LaunchTime bias REVERTED — using original unbiased `ptp_time_cursor` as LaunchTime.

### Why This Should Work (Theory)
1. **CINST narrow**: 1 pkt per doorbell → NIC can't batch → narrow ✓
2. **pkt_ts_vs_rtp_ts**: Future LTs → NIC sends at LT → on-wire = epoch-aligned ✓
3. **VRX**: Same epoch/RTP logic as czwartek6 ✓
4. **inter_frame_rtp_ts_delta**: Same epoch logic → exact 3000 ✓
5. **fps**: Same drift cycle → ~1 epoch drop/10s (same as czwartek6)

### Risk Assessment
- **Performance**: 4x more doorbell rings (4115/frame vs 1029). Doorbell overhead ≈ ~50ns
  each → 4115 × 50ns = ~200µs. Current 110µs/frame overhead may increase.
- **Drift**: If overhead increases significantly, epoch drops may become more frequent
- **NIC behavior**: The hypothesis that NIC batches per-doorbell (not per-queue-depth)
  is supported by czwartek3 data showing exact 4-packet grouping matching bulk=4.
  But needs confirmation with bulk=1.

### Expected Result
| Metric | czwartek6 (bulk=4) | czwartek9 (bulk=1, predicted) |
|--------|-------------------|-------------------------------|
| CINST | wide | **narrow** |
| VRX | wide [0-23] | narrow [0-8]? |
| pkt_ts_vs_rtp_ts | 5-7 µs ✓ | 5-7 µs ✓ |
| inter_frame_rtp_ts | 3000 ✓ | 3000 ✓ |
| Overall | **wide** | **narrow** ? |

### Status: TESTED — see results below

### Measured Results (czwartek9-bulk1)
```
cpu busy 1.02 (was 0.24 with bulk=4 — 4x increase as predicted)
epoch drop 1 per 10s (same pattern)
time_to_tx min -30262us max 5313us
drift ptp_avg +108905ns tsc_avg +108863ns (same ~110µs, bulk overhead didn't increase)
inflight 534505:0 (vs 190867:307672 with bulk=4)
```

### Analyzer Results (czwartek9-bulk1)
| Metric | czwartek6 (bulk=4) | czwartek9 (bulk=1) | Status |
|--------|-------------------|-------------------|--------|
| Overall | **wide** | NOT COMPLIANT | ✗ |
| CINST | wide [0–10] | wide [0–12] 97.86% at 0 | mixed — mostly at 0 |
| VRX | wide [0–23] | -141 (26%), 8 (73.7%) | ✗ bimodal |
| pkt_ts_vs_rtp_ts | 5.5–6.7 µs ✓ | 4–**1161 µs** (max just over 1ms) | ✗ barely |
| inter_frame_rtp_ts | 3000 ✓ | 3000 ✓ | ✓ same |
| TRO | 1.24 ms (±0.5µs) | 1.53 ms (1.22–2.37) | ✗ variable |
| rate | 30.000 | 30.000 | ✓ |

### Per-Packet Data (czwartek9)
```
Per-packet gap histogram:
  Simultaneous (<500ns):     16  (0.0%)   ← almost zero!
  TRS-like (3-15µs):    118175  (99.9%)   ← near-perfect
  Mean: 7776 ns  Stdev: 710 ns
  Min: 0 ns  Max: 97036 ns
```

### CRITICAL FINDING: Stalls Cause Bursts

**Every burst is preceded by a builder stall (55-97µs gap)**:
```
Frame 10: ...8106ns → 8106ns → 94891ns STALL → 954→0→954→0→954→0→954→0→1192→5960ns→8106ns...
Frame 14: ...7868ns → 97036ns STALL → 0→954→954→954→1192→954→0→954→954→0→954→1192→1907→8106ns...
```

Pattern: normal TRS → STALL (55-97µs) → NIC catchup burst (0/954ns) → gradual return to TRS.

The 16 simultaneous gaps are NOT from bulk batching — they're from **NIC catching up** after
the builder stalls. When builder stalls, packets queue up past their LaunchTimes. NIC sends
them rapidly to catch up.

### Why CINST Is Still "Wide" (Barely)

97.86% at CINST=0 is excellent. The 2.14% at CINST 1-12 comes entirely from the
8 stall events (8 stalls × ~6 burst packets each ≈ 48 packets contributing to >0 CINST).
The analyzer marks the entire stream "wide" even though it's overwhelmingly narrow.

### ROOT CAUSE: Builder Stalls + Past LaunchTimes

The czwartek9 capture was taken when builder was **8ms behind** (time_to_tx = -8ms).
This means LaunchTimes were 8ms **in the past** → NIC sent immediately.

When LaunchTimes are past and a stall happens:
1. Builder pauses for 55-97µs
2. NIC has nothing to send (no new descriptors during stall)
3. Builder resumes → submits burst of packets with past LTs
4. NIC sends them all rapidly → burst → TRO shift

**Key contrast with czwartek6**: czwartek6 was captured when builder was near on-time
(time_to_tx ≈ +5ms). Most LaunchTimes were **future** → NIC held packets → even when
builder stalled, NIC sent at correct LaunchTime → no TRO variation → 5-7µs stable.

### Why pkt_ts_vs_rtp_ts Almost Passes
- 28 frames at constant -8.134ms drift → consistent TRO ≈ 1.2ms ← these PASS
- 1 stall shifts TRO by 1.156ms → max delta exceeds 1ms limit by just 161µs
- Average = 324µs (mostly good, one outlier)

**The fix is NOT bulk=1 alone — we need LaunchTimes to stay in the future
so the NIC absorbs builder stalls.**

---

## Fix Attempt 5: bulk=1 + Forward LaunchTime Bias (2026-03-25) — FAILED

### Hypothesis
Add +frame_time (~33.3ms) to every LaunchTime so they always stay in future → NIC always holds → absorbs builder stalls. Combined with bulk=1.

### czwartek10 Results (mypcap-czwartek10.pcap)

| Metric | Value | Limit | Result |
|--------|-------|-------|--------|
| CINST | 89.66%@0, tail to 840 | narrow≤4 | **NOT COMPLIANT** |
| VRX | peaks at 8 (88.5%), -152 (4.9%), -153 (2.1%), -1005 (3.5%) | narrow≤8 | **NOT COMPLIANT** |
| pkt_ts_vs_rtp_ts | min=4µs, max=**41,219µs** (41ms!), avg=4,036µs | max 1000µs | **NOT COMPLIANT** |
| inter_frame_rtp_ts | min=3000, max=**6000**, avg=3107 | exact 3000 | **NOT COMPLIANT** |
| TRO | avg=1.8ms, min=1.2ms, max=**9.1ms** | — | terrible |
| fps | 29.9 | 30.0 | marginal |

Logs:
- `time_to_tx min -31719us max 5313us` — confirms +33ms bias applied (min ≈ -frame_time)
- `epoch drop 1`
- `drift ptp_avg +109289ns tsc_avg +109260ns` — same builder drift
- `cpu busy 1.015891` — same as bulk=1
- `inflight 530027:0`

### Failure Analysis

1. **CINST degraded** from 97.86%@0 (czwartek9) to 89.66%@0 — forward bias made CINST WORSE
2. **pkt_ts_vs_rtp_ts max=41ms** — catastrophic. The +33ms forward bias shifted on-wire arrival by ~33ms creating huge pkt_ts vs RTP delta
3. **inter_frame_rtp_ts=6000** — doubled frames appeared, likely from epoch drop interaction with forward bias
4. **VRX scattered** across -1005 to +8 — the TRO swing from 1.2ms to 9.1ms destroys VRX
5. **max TRO=9.1ms** (vs 1.2ms nominal) — the bias creates variable NIC hold times

### Why It Failed
The forward bias creates a ~33ms NIC hold window. But the NIC TX ring is only 512 entries (≈4ms of packets). When the ring fills with 33ms-future packets, the NIC can't drain them fast enough → backpressure → variable release timing → TRO variance → everything breaks. The pkt_ts_vs_rtp_ts of 41ms proves the NIC was holding packets far too long. The epoch drop at the wrong time caused a doubled RTP timestamp (6000 ticks).

### Key Lesson
**Do NOT add large forward bias to LaunchTimes.** The NIC TX ring (512 desc) cannot absorb 33ms of buffering. The NIC was not designed for this much forward-looking scheduling.

### Code Reverted
Reverted both bulk=1 and forward LT bias back to czwartek6 (wide-compliant) state.

---

## Compliance Target Scorecard

| Metric | czwartek3 | czwartek6 | czwartek7 | czwartek8 | czwartek9 | czwartek10 |
|--------|-----------|-----------|-----------|-----------|-----------|------------|
| CINST | wide | wide | **narrow** | **narrow** | wide(97%@0)| fail(89.6%@0) |
| VRX | fail | **wide** | fail | fail | fail | fail(-1005/8) |
| pkt_ts | **pass** | **pass** | fail | fail | fail(1.16ms)| fail(41ms!) |
| rtp_ts | **pass** | **pass** | fail | **pass** | **pass** | fail(6000) |
| Overall | not_comp | **wide** | not_comp | not_comp | not_comp | not_comp |

---

## Key Invariants (Hard-Won Knowledge)

1. **pcapng_dump captures at RECEIVER with HW PTP timestamps** (not sender-side)
2. **Future LTs**: NIC holds packet → sends at LT → on-wire = epoch time → good pkt_ts_vs_rtp_ts
3. **Past LTs**: NIC sends immediately → on-wire = builder time → bad when stalls happen
4. **E830 batches per-doorbell**: With bulk=4, 4 descs per doorbell → simultaneous send
5. **bulk=1**: mostly eliminates batching, but stall=catchup bursts create residual CINST>0
6. **Forward LT bias (+frame_time) FAILS** → 512-entry TX ring can't buffer 33ms → TRO=9ms, pkt_ts=41ms
7. **110µs/frame processing overhead is REAL** (PTP=TSC within 37ns, not clock drift)
8. **Epoch drop cycle**: 33333µs / 110µs ≈ 303 frames ≈ 10.1s per drop
9. **RTP/epoch logic must remain untouched** — czwartek7 proved modifying it is catastrophic
10. **TX ring = 512 descriptors** — inflight mechanism handles backpressure naturally

## Open Questions
1. E830 NIC TX ring = 512 desc = ~4ms of packets. Large forward bias exceeds ring capacity.
2. How to achieve narrow CINST without breaking pkt_ts_vs_rtp_ts?
3. czwartek6 (wide) is the best achieved so far. What minimal change gets to narrow?

## Current Code State
**Reverted to czwartek6 (wide-compliant) baseline.** No bulk=1, no LT bias.
3. Can we reduce the 110µs/frame drift to avoid epoch drops entirely?
4. What causes the builder stalls (55-97µs)? Scheduler? Interrupts? PTP sync?
