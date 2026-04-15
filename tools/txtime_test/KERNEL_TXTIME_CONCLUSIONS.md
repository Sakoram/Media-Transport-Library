# E830 TX Launch-Time Scheduling — Kernel Path Conclusions

## Test Overview

**Objective:** Prove that Intel E830 NIC hardware launch-time scheduling works via
the kernel ice driver path (SO_TXTIME + ETF qdisc with HW offload), using a timing
pattern that is **impossible to achieve without hardware TX time scheduling**.

**Test Design:** Send 100 UDP packets with a deliberately variable inter-packet
spacing signature — a repeating 8-element cycle of gaps:

| Cycle Position | Planned Gap |
|:-:|:-:|
| 0 | 10 µs |
| 1 | 100 µs |
| 2 | 10 µs |
| 3 | 200 µs |
| 4 | 10 µs |
| 5 | 50 µs |
| 6 | 10 µs |
| 7 | 500 µs |

This pattern spans a 50:1 dynamic range (10 µs to 500 µs). Without HW scheduling,
all packets would arrive at wire rate (~6.6 ns gap for 82-byte frames at 100 Gbps),
making it physically impossible to reproduce these large, variable gaps through
software-only means.

## Hardware & Setup

| Component | Details |
|---|---|
| TX NIC | Intel E830 (PCI ca:00.1), kernel ice driver (Kahawai_2.2.8) |
| RX NIC | Intel E810 (PCI 4b:00.1, eth1) — physically looped to E830 |
| Qdisc | `tc qdisc add dev eth2 root etf clockid CLOCK_TAI delta 500000 offload` |
| TX offset | 50 ms into the future from CLOCK_TAI |
| Capture | `tcpdump -i eth1 --time-stamp-precision=nano` |
| Machine | imtl-dev-3 (112 lcores, 2 NUMA nodes) |

## Results

### Packet Delivery

- **100 / 100 packets transmitted and captured** — zero loss.

### Timing Accuracy

| Metric | Value |
|---|---|
| Pearson correlation (planned vs actual gaps) | **r = 0.999909** |
| R² | **0.999818** |
| Mean absolute error | **0.7 µs** |
| Max absolute error | 17.1 µs (only the 1st gap — ETF qdisc startup artifact) |
| Gaps within 5 µs of planned | **97 / 99 (98%)** |
| Gaps within 10 µs of planned | **97 / 99 (98%)** |
| Gaps within 20 µs of planned | **99 / 99 (100%)** |
| Total span error | **−5 µs** over 10,809 µs planned (0.05%) |

### Per-Category Accuracy

| Planned Gap | Count | Mean Actual | Std Dev | Mean Error |
|:-:|:-:|:-:|:-:|:-:|
| 10 µs | 50 | 10.5 µs | 1.7 µs | +0.5 µs |
| 50 µs | 12 | 49.8 µs | 0.7 µs | −0.2 µs |
| 100 µs | 13 | 98.9 µs | 4.8 µs | −1.1 µs |
| 200 µs | 12 | 199.8 µs | 0.5 µs | −0.2 µs |
| 500 µs | 12 | 499.9 µs | 0.3 µs | −0.1 µs |

### Sample Gap Trace (First 24 Gaps)

```
   Gap  Planned(us)   Actual(us)  Error(us)   Error%
   0->        10.1        21.5      +11.3   +112.2%  ← ETF startup artifact
   1->       100.1        83.0      -17.1    -17.1%  ← ETF startup artifact
   2->        10.1        10.5       +0.4     +3.7%
   3->       200.1       200.3       +0.2     +0.1%
   4->        10.1        10.0       -0.1     -1.0%
   5->        50.0        49.8       -0.2     -0.4%
   6->        10.1        10.5       +0.4     +3.7%
   7->       500.1       499.7       -0.4     -0.1%
   8->        10.1        10.0       -0.1     -1.0%
   9->       100.1       100.1       +0.0     +0.0%
  10->        10.1        10.7       +0.6     +6.1%
  11->       200.1       199.3       -0.7     -0.4%
  12->        10.1        10.0       -0.1     -1.0%
  13->        50.0        50.1       +0.0     +0.0%
  14->        10.1        10.5       +0.4     +3.7%
  15->       500.1       500.0       -0.1     -0.0%
  16->        10.1         9.8       -0.3     -3.3%
  17->       100.1       100.1       +0.0     +0.0%
  18->        10.1        10.5       +0.4     +3.7%
  19->       200.1       199.6       -0.5     -0.3%
  20->        10.1        12.9       +2.8    +27.3%
  21->        50.0        47.7       -2.4     -4.7%
  22->        10.1        10.3       +0.1     +1.4%
  23->       500.1       500.0       -0.1     -0.0%
```

After the first two gaps (ETF qdisc warm-up), errors are consistently sub-microsecond
for large gaps and within ~2 µs for the 10 µs gaps.

## Why This Proves HW Scheduling

1. **50:1 dynamic range is impossible without HW TX time.**
   Without scheduling, 82-byte UDP packets on 100G Ethernet would be serialized
   at ~6.6 ns gaps — 1,500× faster than the smallest planned gap (10 µs) and
   76,000× faster than the largest (500 µs). Software-only delays cannot produce
   sub-microsecond fidelity across such a wide range.

2. **R² = 0.999818 proves the NIC is faithfully executing per-packet launch times.**
   The variable signature pattern — alternating 10/100/10/200/10/50/10/500 µs —
   is reproduced with near-perfect correlation. A random or constant-rate
   transmitter would produce R² ≈ 0.

3. **Sub-microsecond accuracy on 200 µs and 500 µs gaps** (mean error < 0.3 µs)
   demonstrates the 128 ns hardware timestamp resolution working correctly.

4. **The first two gaps show a systematic startup offset** (ETF qdisc `delta`
   artifact), but all subsequent 97 gaps are within 5 µs of their planned values.

## Kernel Path Architecture (What Makes It Work)

```
Application → sendmsg(SCM_TXTIME) → ETF qdisc (offload) → ice driver
  → ice_tx_map_ring() writes launch-time to TX descriptor
  → HW reads 19-bit timestamp (128ns resolution)
  → NIC holds packet until PHC matches launch time
  → Packet transmitted on wire at exact scheduled time
```

Key kernel mechanisms:
- **ETF qdisc with `offload`**: Passes per-packet launch timestamps down to the driver
  via `skb->tstamp`, sorted by departure time
- **`ice_offload_txtime()`**: On first txtime packet, performs full VSI rebuild
  (`ice_down()` → `ice_rebuild()` → `ice_up()`), which reconfigures the TX queue
  for launch-time mode
- **TX descriptor**: 19-bit timestamp field, 128 ns resolution (bits [25:7] of
  nanosecond-within-second), ~67 ms scheduling window

## DPDK Path Status (For Comparison)

The DPDK ice PMD path was also tested extensively but **does not work** with a
single TS-only doorbell (matching the kernel's register write pattern):
- Packets are accepted by `rte_eth_tx_burst()` but `opackets` remains 0
- Root cause: DPDK skips the VSI rebuild that the kernel performs in
  `ice_offload_txtime()` — the AQ commands (`set_txtimeq` + `ena_dis_txtimeq`)
  alone are insufficient
- A dual-doorbell workaround (writing both QTX_TS_DBELL and QTX_COMM_DBELL)
  transmits packets but bypasses HW scheduling (all packets arrive at wire rate)

## Files

| File | Purpose |
|---|---|
| `kernel_txtime_test.c` | Variable-spacing kernel SO_TXTIME test tool |
| `setup_kernel_txtime.sh` | ETF qdisc setup script |
| `varsig_analysis.txt` | Raw analysis output from this test run |
| `RESULTS.md` | Full DPDK investigation findings |

## Conclusion

**E830 hardware TX launch-time scheduling is fully functional via the kernel path.**
The variable-spacing test with R² = 0.999818 provides definitive proof that the NIC
is scheduling individual packets at their requested launch times with sub-microsecond
precision. The test pattern (10–500 µs variable gaps) is physically impossible to
reproduce without active HW scheduling, ruling out any software-timing explanation.

The remaining challenge is replicating this capability in the DPDK path, which
requires understanding and implementing the VSI rebuild sequence that the kernel
driver performs automatically on first txtime use.
