# DPDK TXTIME HW Scheduling — Wire-Level Analysis Results

**Date**: 2026-04-09  
**Machine**: imtl-dev-3 (112 lcores, 2 NUMA nodes)  
**TX NIC**: Intel E830 100G (PCI `ca:00.0`, vfio-pci, DPDK 25.11 + MTL patches)  
**RX NIC**: Intel E810 100G (PCI `4b:00.0` = eth0, kernel ice) — looped to E830  
**DPDK**: v25.11 with 9 MTL patches including patch 0008 (doorbell init write fix)  
**State**: Fresh reboot, all tests pass (opackets>0), FW state clean

---

## 1. Executive Summary

**DPDK TXTIME packets transmit but are NOT scheduled.** All 12 tests pass (opackets>0 after Finding 23 doorbell fix), but HW-timestamped pcap analysis shows packets arrive immediately at wire speed, regardless of their requested launch time offset. The TXTIME HW scheduler is not holding packets to their specified launch time.

| Metric | Kernel (ETF qdisc) | DPDK (TS-only doorbell) |
|--------|-------------------|-------------------------|
| **R²** | **0.999818** | **0.000000** |
| Packets transmit | Yes | Yes |
| HW scheduling active | **Yes** — sub-µs accuracy | **No** — immediate TX |
| Inter-packet gaps | Match planned offsets | Wire-speed bursts |

---

## 2. Methodology

### 2.1 Capture Setup

```
E830 (ca:00.0)  ── 100G cable ──  E810 (4b:00.0 = eth0)
   DPDK vfio-pci                     kernel ice driver
   txtime_test.c                     tcpdump -j adapter_unsynced
```

1. Reboot machine for clean FW state
2. Set up hugepages (2048 × 2MB), bind `ca:00.0` to vfio-pci
3. Start tcpdump on eth0 with **HW timestamps** (`-j adapter_unsynced`):
   ```bash
   sudo tcpdump -i eth0 -j adapter_unsynced --time-stamp-precision=nano \
       -w /tmp/txtime_hwts.pcap udp port 12345
   ```
4. Run `txtime_test` — 12 tests (0–11), 213 packets total
5. Parse pcap with `parse_txtime_pcap.py`

### 2.2 HW Timestamp Verification

E810 NIC confirmed to support `SOF_TIMESTAMPING_RX_HARDWARE` with PTP HW Clock 0:
```
$ ethtool -T eth0
Time stamping parameters for eth0:
        SOF_TIMESTAMPING_RX_HARDWARE
        SOF_TIMESTAMPING_RAW_HARDWARE
```

The `-j adapter_unsynced` flag captures raw NIC hardware timestamps (not software/kernel timestamps), providing sub-µs precision.

### 2.3 Payload Format

`txtime_test.c` embeds a self-describing payload in each UDP packet (magic `0x54585431` "TXT1"):
- `test_id`: which test (0–11)
- `seq`: packet sequence within test
- `offset_ns`: requested offset from PHC at send time
- `launch_time`: absolute launch time written to TS descriptor
- `phc_at_send`: PHC value when packet was submitted

---

## 3. Results — All Tests

### 3.1 Burst Tests (Tests 0–9): No Scheduling

All 10-packet burst tests arrive within 1–2µs regardless of offset:

| Test | Offset | Spread (µs) | Observation |
|------|--------|-------------|-------------|
| 0 | +1ms (no txtime flag) | 0.7 | Control — no TXTIME, expected |
| 1 | +1ms future | 1.7 | All arrive together |
| 2 | -1ms past | 1.9 | All arrive together |
| 3 | alternating +1ms/-1ms | 1.7 | No difference between + and - |
| 4 | -50ms past | 1.4 | All arrive together |
| 5 | -500ms past | 1.4 | All arrive together |
| 6 | alternating +1ms/-500ms | 1.7 | No difference |
| 7 | 0 (PHC now) | 1.9 | All arrive together |
| 8 | +1s future | 1.9 | NOT held — arrives immediately |
| 9 | alternating +1s/-1ms | 1.9 | NOT held — arrives immediately |

**Key observation**: Test 8 (offset=+1s into the future) should have been held by the HW scheduler for ~1 second. Instead, all 10 packets arrive in 1.9µs. **The TXTIME engine is not delaying packets.**

### 3.2 Graduated Test (Test 10): Anomalous Gaps

Test 10 uses graduated offsets from +1ms to -999ms:

| Seq | Offset | Arrival gap from seq 0 | Notes |
|-----|--------|----------------------|-------|
| 0 | +1ms | 0.0µs | baseline |
| 1 | -1ms | 0.7µs | immediate |
| 2 | -10ms | 1.0µs | immediate |
| 3 | -30ms | 1.0µs | immediate |
| 4 | -50ms | **16,109µs** | ~16ms gap |
| 5 | -67ms | 16,110µs | same group |
| 6 | -100ms | **33,218µs** | ~33ms gap |
| 7 | -250ms | 33,219µs | same group |
| 8 | -500ms | 33,219µs | same group |
| 9 | -999ms | 33,219µs | same group |

The ~16ms and ~33ms gaps correlate with the 19-bit timestamp wrapping (67ms window). Offsets that wrap into a different 67ms epoch may trigger the HW to briefly hold the packet. However, this is not intentional scheduling — it's an artifact of timestamp aliasing.

### 3.3 MTL-Style Stream (Test 11): R²=0.000

100 packets with planned 7936ns step (62×128ns), 5ms future offset, bulk=4:

```
Inter-Packet Gap Analysis:
  Planned step     : 7936 ns
  Mean actual gap  : 7699 ns
  Mean |error|     : 11364 ns
  Max |error|      : 23297 ns
  Within 1µs       : 0/99 (0%)
  Within 5µs       : 0/99 (0%)
  Total span planned: 785.7 µs
  Total span actual : 762.2 µs
  Pearson r         : 0.000000
  R²                : 0.000000
  Verdict           : NO SCHEDULING
```

**Pattern observed**: Packets arrive in groups of 4 (matching `g_stream_bulk=4`):
```
Group pattern: [0ns, 0ns, 0ns, ~31µs, 0ns, 0ns, 0ns, ~31µs, ...]
```

- Within each 4-packet group: 0–1µs gaps (wire speed)
- Between groups: ~31µs gap (software refill time)
- The ~31µs inter-group gap is DPDK userspace TX ring replenishment time, not HW scheduling
- Mean gap ≈ (3×~0 + 1×31µs)/4 ≈ 7.7µs — coincidentally close to planned 7936ns, but the distribution is completely wrong

---

## 4. Edge-Case Tests (32 tests via `txtime_edge_test.c`)

Separate test with 32 edge cases (past, future, zero, extreme values, micro-offsets, mixed bursts):

- **32/32 tests PASS** — all packets transmit
- **NO scheduling** — all packets arrive immediately regardless of offset
- Past (−1ms to −999ms), future (+1ms to +1s), zero, extreme values — all transmitted instantly
- No queue poisoning from bad/extreme timestamps

---

## 5. Analysis & Conclusions

### 5.1 What Works
- TXTIME engine processes TS descriptors (int_q_state TAIL/HEAD advance — Finding 14)
- Data DMA is triggered (opackets>0, HEAD advances)
- No MDD events (Finding 23 fix)
- All timestamp values are correct (Finding 22)
- All context fields match kernel (Findings 18, 13)

### 5.2 What Doesn't Work
- **HW scheduler does NOT hold packets to their launch time**
- Packets with +1s future offset arrive immediately (should be held ~1 second)
- Inter-packet gaps show zero correlation with planned timing (R²=0.000)
- The TXTIME engine appears to pass TS descriptors through to data DMA without applying any time-based hold

### 5.3 Root Cause Hypothesis

The TXTIME HW scheduler requires **additional FW-internal state** beyond what's visible in TXTIME/TLAN contexts. The kernel's `ice_vsi_cfg_txtime()` → `ice_vsi_close()` → `ice_vsi_rebuild()` → `ice_vsi_open()` full VSI reconstruction establishes this state. DPDK's incremental queue setup (AQ 0x0C30 + 0x0C35) creates valid contexts but misses the scheduling linkage.

Evidence:
- Kernel achieves R²=0.999818 on same hardware → HW is capable
- DPDK creates identical TXTIME/TLAN contexts (verified via MMIO readback) → contexts are correct
- DPDK TXTIME int_q_state advances → TS descriptors are processed
- But data DMA fires immediately → the "hold until launch time" logic is bypassed

### 5.4 Comparison: DPDK vs Kernel Scheduling

| Aspect | Kernel (R²=0.999818) | DPDK (R²=0.000) |
|--------|---------------------|------------------|
| VSI setup | Full close→rebuild→open | Incremental ice_tx_queue_start |
| Scheduler tree | Full tear-down + rebuild via ice_vsi_cfg_txtime | ice_rm/cfg_vsi_lan_cfg (no TXTIME-specific) |
| Queue contexts | Via ice_vsi_cfg_txq (handles both TLAN+TXTIME) | Separate AQ 0x0C30 + 0x0C35 |
| TXTIME context | Same fields | Same fields |
| TLAN context | Same fields | Same fields |
| TS descriptor format | Same 32-bit layout | Same 32-bit layout |
| Doorbell writes | TS-only (tstamp_ring->tail) | TS-only (TXTIME_DBELL_LSB) |
| **Result** | **Scheduled** | **Immediate** |

---

## 6. Next Steps

1. **PCIe trace comparison**: Capture MMIO + AQ traces for both kernel and DPDK init paths, diff to find the missing register write or AQ command
2. **DPDK deinit audit**: Verify ice_dev_stop()/close() properly cleans up TXTIME FW state to prevent stale-state regressions
3. **Kernel-first test**: Bind to kernel, enable TXTIME, unbind, then run DPDK — test if residual FW state enables scheduling

---

## 7. Reproduction

```bash
# 1. Reboot machine
sudo reboot

# 2. Setup (after reboot)
sudo bash -c 'echo 2048 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages'
sudo dpdk-devbind.py -b vfio-pci ca:00.0

# 3. Start HW-timestamped capture on E810 RX port
sudo tcpdump -i eth0 -j adapter_unsynced --time-stamp-precision=nano \
    -w /tmp/txtime_hwts.pcap udp port 12345 &

# 4. Run txtime_test
cd /home/gta/mkasiew/repos/mtl-txtime/Media-Transport-Library/tools/txtime_test
sudo ./builddir/txtime_test --vdev=net_af_xdp -- -p ca:00.0

# 5. Stop capture, parse
kill %1
python3 parse_txtime_pcap.py /tmp/txtime_hwts.pcap
```

---

## 8. Files

| File | Purpose |
|------|---------|
| `txtime_test.c` | DPDK TXTIME test tool (12 tests, self-describing payloads) |
| `parse_txtime_pcap.py` | Pcap parser with R² gap analysis |
| `txtime_edge_test.c` | 32 edge-case tests (in `../dpdk-report/`) |
| `parse_edge_pcap.py` | Edge test pcap parser (in `../dpdk-report/`) |
| `/tmp/txtime_hwts.pcap` | HW-timestamped capture (213 packets) |
| `/tmp/txtime_hwts_analysis.txt` | Full analysis output |
