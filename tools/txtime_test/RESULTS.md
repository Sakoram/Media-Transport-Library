# E830 TX Launch Time (TxTime) Investigation Results

**Date:** 2026-04-02
**NIC:** Intel E830 100G (PCI 0000:ca:00.0, vfio-pci)
**DPDK:** 25.11 (modified, commit e035aa5c76+)
**Kernel driver reference:** ice-2.5.4 out-of-tree
**Machine:** imtl-dev-3, 112 lcores, 2 NUMA nodes

## 1. E830 TX Time Hardware Architecture

### 19-bit Timestamp Window

The E830 uses a **19-bit timestamp** with 128ns resolution embedded in each
TS descriptor. The HW compares this against the lower bits of the PHC:

```
tstamp = (launch_time_ns % 1_000_000_000) >> 7
```

This gives a ~67ms (2^19 × 128ns) window within each second. The HW only
sees the low 19 bits — it cannot distinguish "1ms in the future" from
"999ms in the past" if they map to the same 19-bit value.

### Dual Descriptor Rings

When txtime is enabled, E830 uses **two** descriptor rings per queue:

| Ring | Descriptor type | Register | Ring size |
|------|----------------|----------|-----------|
| TX ring | `ice_tx_desc` (16 bytes) | `QTX_COMM_DBELL` (0x002C0000 + q×4) | `nb_tx_desc` (e.g. 512) |
| TS ring | `ice_ts_desc` (4 bytes) | `E830_GLQTX_TXTIME_DBELL_LSB` (0x002E0000 + q×8) | `nb_ts_desc` (e.g. 544 = 512 + fetch) |

Each TS descriptor maps 1:1 to a TX descriptor and contains:
- Bits [12:0]  `tx_desc_idx` — index of the **next-after-last** TX descriptor
- Bits [31:13] `tstamp` — 19-bit launch time value

### Doorbell Registers

- **QTX_COMM_DBELL** (regular TX tail): triggers DMA fetch of TX descriptors
- **E830_GLQTX_TXTIME_DBELL_LSB** (TS tail): triggers processing of TS descriptors

## 2. Kernel vs DPDK Implementation Comparison

### Kernel ice-2.5.4 (working launch-time scheduling)

**Queue setup** (`ice_base.c:ice_vsi_cfg_txq`):
1. `ice_setup_tx_ctx()` — configure TLAN context (regular TX ring)
2. `ice_ena_vsi_txq()` — AQ command to add TX queue
3. `ring->tail = QTX_COMM_DBELL(q)` — store regular tail pointer
4. `ice_setup_txtime_ctx()` — configure txtime context
5. `ice_aq_set_txtimeq()` — AQ command to configure TS ring
6. `tstamp_ring->tail = E830_GLQTX_TXTIME_DBELL_LSB(q)` — store TS tail

**Enable** (`ice_main.c:ice_offload_txtime`, called via ethtool):
- First enable: full VSI close → rebuild → open
- Runtime toggle: `ice_aq_ena_dis_txtimeq(enable=true)`

**TX path** (`ice_txrx.c:ice_tx_map`):
```c
if (tx_ring->flags & ICE_TX_FLAGS_TXTIME) {
    /* Fill TS descriptor */
    ts_desc->tx_desc_idx_tstamp = ice_build_tstamp_desc(i, tstamp);
    /* Write ONLY the TS doorbell */
    writel_relaxed(tstamp_ring->next_to_use, tstamp_ring->tail);
} else {
    /* Normal: write regular TX tail */
    writel_relaxed(i, tx_ring->tail);
}
```

**Key:** kernel writes **only** the TS doorbell when txtime is active. The
HW processes both TS scheduling and TX descriptor DMA from just the TS
doorbell ring.

### DPDK ice PMD (txtime NOT scheduling)

**Queue setup** (`ice_rxtx.c:ice_tx_queue_start`):
1. TLAN context + `ice_ena_vsi_txq()` (same as kernel)
2. `qtx_tail = E830_GLQTX_TXTIME_DBELL_LSB(q)` — redirect main tail to TS
3. `tx_tail_reg = QTX_COMM_DBELL(q)` — store regular tail separately
4. `ice_aq_set_txtimeq()` — configure TS ring context
5. `ice_aq_ena_dis_txtimeq(enable=true)` — explicitly enable

**TX path** (`ice_rxtx.c:ice_xmit_pkts`, end_of_tx):
```c
/* TS doorbell first */
ICE_PCI_REG_WRITE(txq->qtx_tail, ts_id);
rte_wmb();
/* Regular TX tail second */
ICE_PCI_REG_WRITE(txq->tsq->tx_tail_reg, tx_id);
```

**Problem:** writing `QTX_COMM_DBELL` bypasses the launch-time scheduler
and triggers immediate DMA. But writing only the TS doorbell (without
`QTX_COMM_DBELL`) gives **opackets=0** — no DMA at all.

### Side-by-side Comparison

| Aspect | Kernel (scheduling works) | DPDK (scheduling broken) |
|--------|--------------------------|--------------------------|
| TX doorbell in txtime path | TS doorbell ONLY | TS + QTX_COMM_DBELL (both) |
| `ice_aq_ena_dis_txtimeq` | Via ethtool (runtime) | At queue_start |
| First-time enable | Full VSI rebuild | No VSI rebuild |
| Result: TS-only doorbell | Packets TX with scheduling | opackets=0 |
| Result: dual doorbell | N/A | Packets TX, no scheduling |

### Exhaustive Field-by-Field Context Comparison

All of these are **identical** between DPDK and kernel:

- `ice_txtime_ctx_info` packing table (bit-field widths and offsets)
- `base` address calculation (iova >> 7)
- `qlen` (TS ring count = TX count + fetch descriptors)
- `pf_num`, `src_vsi`, `vmvf_type` (all derived from same FW values)
- `txtime_ena_q = 1`
- `ts_res = ICE_TXTIME_CTX_RESOLUTION_128NS`
- `drbell_mode_32 = ICE_TXTIME_CTX_DRBELL_MODE_32`
- `ts_fetch_prof_id = ICE_TXTIME_CTX_FETCH_PROF_ID_0`
- AQ command struct layouts (`ice_aqc_set_txtimeqs`, `ice_aqc_set_txtimeqs_perq`)
- TS descriptor format (`ICE_TXTIME_TX_DESC_IDX_M` 13-bit, `ICE_TXTIME_STAMP_M` 19-bit)
- `desc_tx_id` value (next_to_use, i.e. next-after-last descriptor index)
- TS ring DMA alignment (128-byte via `ICE_RING_BASE_ALIGN`)

## 3. Doorbell Configuration Matrix

We tested 5 different doorbell configurations:

| Version | Init writes | ena_dis_txtimeq | TX path doorbells | opkts | Scheduling |
|---------|-------------|-----------------|-------------------|-------|------------|
| v6 | none | yes | TX-first, TS-second | 10 ✓ | NO |
| v7 | none | yes | TS-only | 0 ✗ | — |
| v8 | both=0 | yes | TS-first, TX-second | 10 ✓ | NO |
| v9 | none | no | TS-only | 0 ✗ | — |
| Original DPDK | TS=0 | no (only disable on stop) | TS-only | 0 ✗ | — |

**Conclusion:** In DPDK, writing only the TS doorbell never triggers TX
DMA. A `QTX_COMM_DBELL` write is always required, but it bypasses the
launch-time scheduler.

## 4. Test Results (v8 — best working configuration)

All 10 test cases + sanity test pass. Every packet is transmitted and
captured on the wire. No packets are dropped, stalled, or reordered.

### Per-test timing from pcap analysis

| Test | Description | Pkts | Burst spread (µs) | Scheduling? |
|------|-------------|------|--------------------|-------------|
| 0 | Sanity (no txtime flag) | 3 | 0.0 | N/A |
| 1 | Future +1ms | 10 | 31.9 | NO |
| 2 | Past -1ms | 10 | 25.0 | NO |
| 3 | Alternating ±1ms | 10 | 23.1 | NO |
| 4 | Past -50ms | 10 | 21.9 | NO |
| 5 | Past -500ms | 10 | 28.8 | NO |
| 6 | Alternating +1ms/-500ms | 10 | 23.8 | NO |
| 7 | Offset = 0 (PHC now) | 10 | 23.1 | NO |
| 8 | Future +1s | 10 | 21.0 | NO |
| 9 | Alternating +1s/-1ms | 10 | 30.0 | NO |
| 10 | Graduated +1ms to -999ms | 10 | 21.9 | NO |

All recovery packets (+1ms future) also transmitted successfully.

### Key observations

1. **No scheduling at all.** Burst spreads of 20-32µs across all tests
   are consistent with pure wire serialization time for 10× 82-byte
   packets at 100Gbps. The HW launch-time scheduler is not engaged.

2. **Past-time packets are NOT dropped or stalled.** Even -999ms past
   offsets transmit immediately and identically to +1s future offsets.

3. **No HW errors.** All tests show oerr=0. No MDD events on queue 0.

4. **Recovery always works.** After every test (including extreme
   past/future offsets), a +1ms recovery packet transmits normally.

## 5. Bugs Found in DPDK ice PMD

### Bug 1: Original code — TS-only doorbell gives opackets=0

The original DPDK ice PMD (before our changes) redirects `qtx_tail` to
`E830_GLQTX_TXTIME_DBELL_LSB` and never writes `QTX_COMM_DBELL`. This
matches the kernel's approach, but in DPDK the TS doorbell alone does not
trigger TX descriptor DMA. Possible root causes:

- VFIO/IOMMU state differences from kernel driver
- Missing FW-level per-queue state that the kernel's VSI rebuild establishes
- HW requires a specific initialization sequence only performed by the
  kernel's `ice_ena_vsi_txq` + `ice_vsi_close/open` cycle

### Bug 2: Missing `ice_aq_ena_dis_txtimeq(enable=true)` at queue start

The original DPDK code only calls `ice_aq_ena_dis_txtimeq(enable=false)`
during queue stop. It never enables the txtimeq. While the kernel relies on
`txtime_ena_q=1` in the context for first-time enable, the DPDK path may
need the explicit AQ enable since it doesn't do a VSI rebuild.

### Workaround applied: Dual-doorbell write

Writing both doorbells makes packets flow but bypasses the scheduler:

```c
/* ice_rxtx.c end_of_tx: */
ICE_PCI_REG_WRITE(txq->qtx_tail, ts_id);      /* TS doorbell */
rte_wmb();
ICE_PCI_REG_WRITE(txq->tsq->tx_tail_reg, tx_id); /* regular tail */
```

This is a **functional workaround** that at least allows TX with txtime
offloading configured, but launch-time scheduling is not active.

## 6. Other Issues Encountered

### IOVA mode

DPDK VA mode causes DMAR faults with vfio-pci on this platform. The test
tool injects `--iova-mode=pa` into EAL args as a workaround.

### System library mismatch

The test tool initially linked against a system-installed DPDK at
`/usr/local/lib/` instead of our modified build. Fixed by installing the
modified DPDK system-wide with `sudo ninja -C build install && ldconfig`.

### Link-up time

E830 takes ~21 seconds to bring the link up. The test tool waits up to 30s.

## 7. Open Questions

1. **Why does TS-doorbell-only work in kernel but not DPDK?** The code is
   functionally identical. The difference must be in HW/FW state established
   during VSI lifecycle management that DPDK doesn't replicate.

2. **Can MTL's approach work?** MTL uses the same DPDK PMD and also reports
   txtime issues ("permanently stalls the txtime HW scheduler on E830").

3. **Is a DPDK upstream fix possible?** The PMD may need to replicate the
   kernel's VSI rebuild cycle when enabling txtime, or use a different
   doorbell strategy.

## 8. Files

| File | Description |
|------|-------------|
| `txtime_test.c` | Test tool: 10 test cases + sanity, self-describing payloads |
| `parse_txtime_pcap.py` | Pcap decoder: groups by test, shows timing analysis |
| `meson.build` | Build integration |
| `RESULTS.md` | This file |
