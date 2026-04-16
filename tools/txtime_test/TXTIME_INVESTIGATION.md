# E830 TX Time Investigation

**Updated**: 2026-04-13 (session 8 — **SOLVED**)  
**NIC**: Intel E830 100G (ca:00.0 DPDK/vfio-pci, ca:00.1 kernel/eth2)  
**DPDK**: 25.11 (`dpdk-25.11/`), **Kernel**: ice-2.5.4 (`ice-update/ice-2.5.4/`)
**FW**: 7.9.5 api 1.7.11 nvm 1.20

---

## 1. Problem Statement

E830 HW launch-time scheduling works via the **kernel** ice driver but **not via DPDK** without a workaround.

**SOLVED in session 8**: A `dev_stop → dev_start` cycle with scheduler rebuild in `ice_dev_start()` achieves **R²=1.000000** (103ns mean error, sub-µs per-packet scheduling). See §16 for details.

**Root cause**: DPDK's incremental queue setup skips the `queue disable → scheduler rebuild → queue re-add` AQ sequence that the kernel performs during `ice_vsi_cfg_txtime()`. The FW requires this sequence to establish the internal TXTIME→data DMA linkage.

**Kernel HW scheduling IS confirmed real** — R²=0.999818 accuracy, sub-µs precision. Software scheduling cannot achieve this. The HW TXTIME engine works; it's the DPDK integration that's broken.

## 2. Hardware Architecture

Dual-ring per queue: **data ring** (QTX_COMM_DBELL `0x002C0000+q*4`) and **TS ring** (E830_GLQTX_TXTIME_DBELL_LSB `0x002E0000+q*8`). Each TS descriptor (32-bit): bits[12:0]=`tx_desc_idx`, bits[31:13]=`tstamp` (128ns units, 19-bit → ~67ms window).

HW scheduler reads TS descriptors, waits for launch time, then DMAs data descriptors.

## 3. Kernel Path (WORKS — R²=0.999818 scheduling accuracy)

1. `ice_offload_txtime()` → `ice_vsi_cfg_txtime()`: full VSI close → rebuild → open
2. Rebuild: `ice_rm_vsi_lan_cfg()` removes scheduler tree, `ice_cfg_vsi_lan()` re-adds it
3. `ice_vsi_cfg_txq()` programs TLAN + TXTIME contexts for ALL queues (non-TXTIME get `txtime_ena_q=0`)
4. TX path: writes **ONLY** TS doorbell — HW handles data DMA automatically
5. `ice_aq_ena_dis_txtimeq()` (0x0C37) is NEVER called for fresh setup — only for runtime toggle

## 4. DPDK Path (BROKEN — opackets=0 with TS-only doorbell)

### Current code state

**`ice_dev_start()`** order:
1. Clear stale MDD: GL + PF level (`GL_MDET_TX_PQM/TCLAN`, `PF_MDET_TX_PQM/TCLAN/TDPU`)
2. `ice_timesync_enable()` — PHC before queue start (was after — fixed)
3. Read `E830_GLTXTIME_TS_CFG` — verify global TXTIME_ENABLE=1 (always was 1)
4. Program TXTIME fetch profile and WRR registers (see Finding 8)
5. Scheduler tree rebuild: `ice_rm_vsi_lan_cfg()` → `ice_cfg_vsi_lan()` (both return 0)
6. Clear stale QTX_COMM_HEAD via direct write (see Finding 7)
7. Per-queue `ice_tx_queue_start()` → `ice_ena_vsi_txq()` + `ice_aq_set_txtimeq()`
8. No doorbell writes during setup (matching kernel)
9. Post-start MDD check

**`ice_xmit_pkts()`**: writes ONLY TXTIME doorbell (`ts_id`). No COMM_DBELL.

### What's verified correct
- TXTIME context readback: `hw_base` MATCH, `txtime_ena_q=1`, `drbell32=1`, `ts_res=7`
- PHC ticking: delta ~1ms between reads
- `E830_GLTXTIME_TS_CFG` TXTIME_ENABLE=1 (already set by FW)
- Scheduler rebuild: both `ice_rm_vsi_lan_cfg` and `ice_cfg_vsi_lan` return 0
- AQ `ice_aq_set_txtimeq` returns 0
- `timer_num=0`, `tmr_index_owned=0`, `tmr_index_assoc=0` — all consistent
- QTX_COMM_HEAD cleared to 0 (see Finding 7)
- Data path works (dual doorbell → HEAD advances, opkts>0) — queue itself is functional

### Doorbell configuration matrix (from RESULTS.md)

| Config | TX path doorbells | opackets | Scheduling |
|--------|-------------------|----------|------------|
| TS-only | TXTIME_DBELL only | 0 | — |
| TX-first, TS-second | COMM then TXTIME | >0 | NO |
| TS-first, TX-second | TXTIME then COMM | >0 | NO |
| TS-only + ena_dis_txtimeq | TXTIME_DBELL only | 0 | — |

**Conclusion**: COMM_DBELL always bypasses scheduler. TS-only never triggers DMA.

## 5. Key Findings (2026-04-07)

### Finding 1: TXTIME engine IS active — context changes after doorbell
```
TXTIME POST: ctx3=0xc0003e11 → 0x40003e11 → 0x80003e11 → 0x00003e11
```
Upper bits of context data[3] cycle through 4 states between TX calls. The TXTIME engine receives TS doorbell writes and processes them. But it never triggers data descriptor DMA.

### Finding 2: QTX_COMM_HEAD was stuck at 0xd4 — NOW CLEARED (see Finding 7)
Was stale from previous DPDK runs (CORER-reset-only, PFR doesn't clear it). Direct `wr32(HEAD, 0)` works. **Even with HEAD=0, TS-only doorbell still gives opackets=0.** HEAD is NOT the root cause.

### Finding 3: DBELL readback = 0xdeadbeef
```
DBELL_LSB=0xdeadbeef DBELL_MSB=0xdeadbeef
```
TXTIME doorbell registers are **write-only** — readback is hardware default garbage. Confirms registers are BAR-mapped correctly (not unmapped/faulting), but we can't verify the written value.

### Finding 4: MDD eliminated after HEAD clearing
With stale HEAD=0xd4: MDD MAL_TYPE=28 fires during `ice_tx_queue_start()`.
With HEAD cleared to 0: **No MDD events at all** — clean setup and clean TX.
MDD was caused by stale HEAD state, not by the TXTIME context itself.

### Finding 5: DPDK `ice_sched_add_node()` missing `txq_id` parameter
Kernel passes 5 args to `ice_sched_add_node()` including `txq_id`; DPDK passes 4 (no `txq_id`). Kernel stores `node->tx_queue_id` in leaf nodes; DPDK doesn't. This is a SW-side-only field — the FW AQ command doesn't include it. Unlikely to be the root cause since FW doesn't see it.

### Finding 6: desc_tx_id wrap bug (fixed)
Original DPDK code: `desc_tx_id = (tx_id == 0) ? txq->nb_tx_desc : tx_id` — passes nb_tx_desc (e.g. 512, **out of bounds**) when tx_id wraps to 0. Kernel passes `i` directly (=0 on wrap). **Fixed to `desc_tx_id = tx_id`.**

### Finding 7: QTX_COMM_HEAD is WRITABLE — direct clearing works
```
TXTIME QRESET: q=0 reg_idx=1 HEAD=0xd4 before reset
TXTIME QRESET: after wr32(HEAD,0): HEAD=0x0 CLEARED
```
Despite being documented with reset source CORER, `QTX_COMM_HEAD` can be directly written via `wr32()`. Writing 0 clears it. This eliminates MDD events but does NOT fix the TS-only doorbell issue.

### Finding 8: Data path works — dual doorbell confirms queue is functional
```
(dual doorbell test) HEAD advances: 0x0 → 0x2 → 0xc → 0xd → 0x17
opkts=3, opkts=10, opkts=10... all packets transmitted
```
With both COMM_DBELL and TXTIME_DBELL written, HEAD advances normally and packets transmit. The data queue, TLAN context, DMA addresses, and descriptors are all correct. The problem is specifically that the TXTIME engine does not trigger data DMA from TS descriptors alone.

### Finding 9: GLTXTIME_FETCH_PROFILE reads 0 — but NOT the root cause
```
TXTIME GLOBALS: fetch_prof[0,0]=0x00000000 (FETCH_TS_DESC=0 FETCH_FIFO_THRESH=0)
```
The TXTIME fetch profile register reads all zeros. We programmed it to `FETCH_TS_DESC=32, FIFO_THRESH=16` — the write sticks (reads back 0x2020). **But TXTIME still doesn't work.**

This is NOT the root cause. The kernel on the SAME NIC (ca:00.1) also sees `FETCH_PROFILE=0` (it's a global register shared across PFs) yet achieves perfect HW scheduling. The kernel's `ice_calc_ts_ring_count()` reads this register purely for **TS ring size calculation** (uses default=8 if zero). The HW TXTIME engine has its own internal fetch behavior independent of this register.

### Finding 10: WRR registers are read-only / FW-controlled
```
TXTIME WRR: CREDITS=0x0 WEIGHTS=0x0
TXTIME WRR: programmed CREDITS=0x0 WEIGHTS=0x0  ← writes didn't stick!
```
`E830_GLTXTIME_DBL_COMP_WRR_MAX_CREDITS` and `E830_GLTXTIME_DBL_COMP_WRR_WEIGHTS` read 0 and **cannot be written by SW** — writes are silently ignored. These are FW-controlled registers. The kernel also sees 0 and works fine, so this is not an issue.

### Finding 11: No doorbell writes during setup — matching kernel
Removed the initial `ICE_PCI_REG_WRITE(qtx_tail, ...)` and `ICE_PCI_REG_WRITE(tx_tail_reg, ...)` during queue start. The kernel never writes doorbells during setup. **No effect on the problem.**

### Finding 12: AQ 0x0C37 (ena_dis_txtimeq) enable call — no effect
```
ice_aq_ena_dis_txtimeq(enable) q=1 err=0
```
Added explicit `ice_aq_ena_dis_txtimeq(hw, reg_idx, 1, true, ...)` after `ice_aq_set_txtimeq`. Call succeeds (err=0) but **opackets still 0 with TS-only doorbell**. The kernel never calls 0x0C37 during fresh TXTIME setup — only for runtime toggle. Calling it doesn't help.

### Finding 13: AQ 0x0C35 buffer hex dump — context verified correct
```
0x0C35 buf (40 bytes):
0000: 00 00 00 00 00 00 00 00 00 00 00 00 77 a9 9f 74
0010: 00 00 00 00 80 08 00 00 11 3e 00 00 00 00 00 00
```
Hex dump of the raw AQ buffer sent for `ice_aq_set_txtimeq` (0x0C35). Decoded: `txtime_ena_q=1`, `drbell_mode_32=1`, `ts_res=7`, `base` matches DMA address. All fields correct. The AQ command payload itself is not the problem.

### Finding 14: int_q_state proves TXTIME engine reads TS descs but never triggers data DMA (CRITICAL)
```
int_q_state=0x0_0000e00000000003  (after ts_id=3 doorbell)
int_q_state=0x0_0000e0000000000d  (after ts_id=13 doorbell)
ctx[4] advances: 0 → 3 → 3 → 6 → 6   (TXTIME internal HEAD)
```
Full 7-word TXTIME context readback via `E830_GLTXTIME_QTX_CNTX_CTL/DATA` after each TX batch:
- `int_q_state` is a 70-bit field at TXTIME context bits [126:195], spanning data[3..6]
- **Low bits of int_q_state match ts_id values exactly** — this is the TXTIME internal TAIL pointer
- **ctx[4] advances** (0→3→6) — this is the TXTIME internal HEAD, proving the engine processes TS descriptors
- **But QTX_COMM_HEAD stays at 0** — data descriptor DMA is never triggered

**Conclusion**: The TXTIME engine receives doorbell writes, fetches TS descriptors, updates its internal HEAD/TAIL pointers, but **fails to initiate the data DMA** that would actually transmit packets. The disconnect is between the TXTIME engine's TS processing and the data queue's DMA trigger.

### Finding 15: COMM_DBELL-only transmits — proves dual-doorbell success is NOT TXTIME-scheduled
```
(COMM_DBELL only, no TXTIME_DBELL)
HEAD advances: 0x0 → 0x3 → 0xd → ...
opkts=10 for all tests
```
Disabled TXTIME_DBELL, kept only COMM_DBELL. Packets still transmit at full rate. This proves that the dual-doorbell (Finding 8) transmits via the **regular TX engine**, not via TXTIME HW scheduling. The TXTIME engine is completely bypassed when COMM_DBELL is used.

### Finding 16: Kernel TXTIME context scan — ALL kernel queues have txtime_ena_q=1
```
TXTIME context scan (queues 0-1024):
  q=1  (DPDK):  txtime_ena_q=1  data={0x749fa977 0x0 0x880 0x3e11 0x0 0x3800 0x0}
  q=2  (kernel): txtime_ena_q=1  data={0x640144a4 0x0 0x880 0xc0003c41 0x207 0xc3000 0x0}
  q=3  (kernel): txtime_ena_q=1  ...
  q=4  (kernel): txtime_ena_q=1  ...
  q=5  (kernel): txtime_ena_q=1  ...
  q=6  (kernel): txtime_ena_q=1  ...
```
Scanned TXTIME contexts for all queues 0-1024 via MMIO register read. Only q=1 (DPDK) and q=2-6 (kernel PF1) have `txtime_ena_q=1`. Kernel queue 2 (the active TXTIME queue) shows int_q_state in ctx[4]=0x207, ctx[5]=0xc3000 — active internal state. DPDK queue 1 shows ctx[4]=0x0, ctx[5]=0x3800 — TS descs processed but no active data-side state.

**Key difference**: Kernel active queue has `ctx[3]=0xc0003c41` vs DPDK `ctx[3]=0x3e11`. Both have same static fields but kernel ctx[3] upper bits have `0xc000xxxx` pattern indicating active scheduling state. DPDK's `0x00003e11` has zero upper bits — the scheduling engine is processing TS descs but is NOT in "active scheduling" mode.

### Finding 17: MMIO write mechanism verified correct
`ICE_PCI_REG_WRITE` → `writel` → `rte_write32` — plain 32-bit volatile store. Not defined in the ice driver specifically; uses DPDK's generic `rte_write32()`. Functionally correct — same semantics as kernel's `writel()`. BAR mapping covers all registers (first 8MB). **MMIO writes are NOT the issue.**

### Finding 18: TLAN context readback — DPDK and kernel IDENTICAL
```
TLAN[q=1 DPDK]:  raw={0x749fa9db 0x0 0x04088000 0x0 0x03010000 0x10 0x01000000 0x0 0x0 0x80000000}
TLAN[q=2 kernel]: raw={0x6401513c 0x0 0x04088000 0x0 0x05010000 0x10 0x08000000 0x0 0x0 0x80000000}
```
Read TLAN (data queue) contexts from HW via `GLCOMM_QTX_CNTX_CTL/DATA` (10 dwords). Both DPDK q=1 and kernel q=2 have:
- `qlen=512`, `tso_ena=1`, `legacy_int=1`, `gsc_ena=0`, `tsyn_ena=1` (from TLAN bit 90)
- Only differences: `base` (different ring addresses), `pf_num`, `src_vsi` (different PFs)
- **`gsc_ena=0` for BOTH** — the TLAN context does NOT have a TXTIME association bit. The TS→data link is NOT in the TLAN context.

### Finding 19: Programming TXTIME context for all queues — no effect
```
TXTIME ALLQ: q=0 reg_idx=0 set_txtimeq err=0
TXTIME SCAN: q=0 ENABLED data={0x33999600 0x0 0x880 0x3e02 0x0 0x2000 0x0}
```
Programmed TXTIME context (txtime_ena_q=1) for queue 0 (the non-TX queue at base_queue-1) via dummy TS ring. AQ returned success. TXTIME scan confirmed q=0 now has txtime_ena_q=1. **Still opackets=0 with TS-only doorbell.**

### Finding 20: Port stop→start cycle — no effect
```
dev_stop ret=0
dev_start ret=0
opkts=0 (all tests)
```
Added `rte_eth_dev_stop()` → `rte_eth_dev_start()` after initial port_init. This triggers `ice_dev_stop()` (which calls `ice_dis_vsi_txq` / AQ 0x0C31 to disable queues) then `ice_dev_start()` (which re-creates queues via AQ 0x0C30 + 0x0C35). The stop/start cycle does NOT replicate the kernel's full VSI rebuild — the kernel does `ice_vsi_close()` → `ice_vsi_rebuild(ICE_VSI_FLAG_NO_INIT)` → `ice_vsi_open()` which destroys and recreates all ring arrays and resets FW-internal per-VSI state.

## 6. Context Field Comparison

### TLAN context — IDENTICAL
All fields match: `base`, `port_num`, `qlen`, `cgd_num`, `pf_num`, `vmvf_type`, `src_vsi`, `tsyn_ena=1`, `tso_ena=1`, `tso_qnum`, `legacy_int=1`. Only `quanta_prof_idx` differs (kernel uses `ring->quanta_prof_id`, DPDK uses 0) — benign.

### TXTIME context — IDENTICAL
All fields match: `base`, `qlen`, `txtime_ena_q=1`, `pf_num`, `vmvf_type`, `src_vsi`, `ts_res=7`, `drbell_mode_32=1`, `ts_fetch_prof_id=0`, `timer_num=0`.

## 7. What We Tried (all still opackets=0 with TS-only doorbell)

| # | Attempt | Result |
|---|---------|--------|
| 1 | Match kernel AQ sequence (ena_vsi_txq → set_txtimeq, no ena_dis) | No effect |
| 2 | Enable PHC before queue start | PHC ticking, no effect |
| 3 | Clear all MDD registers (GL + PF level) | MDD still fires (was stale HEAD) |
| 4 | Scheduler tree rebuild (`ice_rm_vsi_lan_cfg` + `ice_cfg_vsi_lan`) | Both ret=0, no effect |
| 5 | Check `E830_GLTXTIME_TS_CFG` global enable | Already enabled (=1) |
| 6 | Fix `desc_tx_id` wrap bug | Fixed, no effect on TS-only |
| 7 | Set `timer_num = tmr_index_assoc` | Was already 0=0 |
| 8 | Align TX ring to stale HEAD=0xd4 | No effect |
| 9 | Disable queue before enable | Failed, queue not in tree |
| 10 | **Clear HEAD via wr32(HEAD,0)** | ✅ HEAD=0, MDD gone, still opackets=0 |
| 11 | **Dual doorbell (COMM+TS)** | ✅ Packets TX, HEAD advances — data path works! |
| 12 | **No doorbell writes during setup** | Matches kernel, no effect |
| 13 | **Program FETCH_PROFILE to 32** | Write sticks, no effect |
| 14 | **Program WRR credits/weights** | Writes DON'T stick (read-only), no effect |
| 15 | **AQ 0x0C37 enable call** | err=0, no effect — kernel never calls it during fresh setup |
| 16 | **AQ 0x0C35 hex dump verification** | All bytes correct, context payload not the issue |
| 17 | **int_q_state deep analysis** | Proves TXTIME reads TS descs, HEAD advances, but data DMA never triggered |
| 18 | **COMM_DBELL-only (no TXTIME)** | Packets TX — proves dual-doorbell is regular TX, not scheduled |
| 19 | **Kernel TXTIME context scan** | All kernel queues have txtime_ena_q=1; ctx[3] shows "active scheduling" bits |
| 20 | **MMIO writel verification** | rte_write32 = correct volatile store, not the issue |
| 21 | **TLAN context readback from HW** | DPDK q=1 and kernel q=2 TLAN contexts identical (gsc_ena=0, same fields) |
| 22 | **Program TXTIME ctx for all queues** | Programmed q=0 with txtime_ena_q=1; still opackets=0 |
| 23 | **Port stop→start cycle** | dev_stop→dev_start; still opackets=0 |
| 24 | **testpmd analysis** | testpmd uses same ice_xmit_pkts path, same TS descs, same TXTIME_DBELL — also opackets=0 |
| 25 | **Timestamp validation (correct PHC read)** | Timestamps ~1ms in future, TS desc bits perfect. NOT the problem |

## 8. Remaining Hypotheses

### Narrowed problem statement (session 3)

The TXTIME engine **IS processing TS descriptors** (int_q_state TAIL/HEAD advance). The failure is specifically: **the TXTIME engine does not trigger data descriptor DMA**. This means the link between the TS engine and the data queue's DMA engine is broken or never established.

Possible causes for the missing TS→data DMA link:
- FW-internal state that maps a TXTIME queue to its data queue is not set
- The data queue itself is not registered with the TXTIME engine (only the TXTIME context exists, but the data queue doesn't "know" it's TXTIME-managed)
- A FW init sequence establishes this link during VSI rebuild that our incremental setup skips

### High priority
1. **Kernel-first test (fastest to isolate setup vs HW issue)**: Bind ca:00.0 to kernel ice, enable TXTIME via ETF qdisc, verify it works, then unbind → vfio-pci → run DPDK with TS-only doorbell. If DPDK TS-only works after kernel setup → definitively proves it's a DPDK queue initialization issue. If it doesn't → HW state is reset on unbind, focus on replicating kernel's init sequence.

2. ~~**TLAN context readback from HW**~~: **RULED OUT (Finding 18)** — TLAN contexts identical, `gsc_ena=0` for both, no TXTIME association bit in TLAN.

3. ~~**Program TXTIME context for ALL VSI queues**~~: **RULED OUT (Finding 19)** — Programmed q=0 with txtime_ena_q=1, still opackets=0.

4. ~~**Full VSI stop→start cycle**~~: **PARTIALLY TESTED (Finding 20)** — dev_stop/dev_start doesn't match kernel's full VSI rebuild. A deeper approach (CORER reset or manual VSI teardown via AQ) may still be needed.

### Medium priority
5. **AQ 0x0C30 (add_lan_txq) comparison**: The TLAN context is sent via AQ 0x0C30. While we verified TXTIME context (0x0C35) is correct, we haven't verified the TLAN context bytes match the kernel's. There may be a TLAN field (e.g., a TXTIME-association bit not documented in SW) that the kernel sets and we don't.

6. **CORER reset before setup**: Call `ice_reset(hw, ICE_RESET_CORER)` before queue setup to get a clean HW state. CORER resets all per-queue state including TXTIME engine internals. Risk: breaks other HW state, needs re-init.

7. **Try single TX queue (reg_idx=0)**: Current setup uses queue 1 (reg_idx=1). Try with only 1 TX queue so reg_idx=0. Rules out any off-by-one or queue-0-special-casing in FW.

### Low priority — ruled out
8. ~~**Stale QTX_COMM_HEAD**~~: **RULED OUT** — cleared to 0, no effect.
9. ~~**MDD blocking TX**~~: **RULED OUT** — no MDD with HEAD=0.
10. ~~**FETCH_PROFILE=0**~~: **RULED OUT** — kernel also sees 0, works.
11. ~~**WRR credits/weights=0**~~: **RULED OUT** — FW-controlled, kernel also 0.
12. ~~**Missing txq_id in sched node**~~: **RULED OUT** — SW-only field.
13. ~~**desc_tx_id wrap bug**~~: **FIXED** — TX-path-only.
14. ~~**AQ 0x0C37 enable call**~~: **RULED OUT** — tested, err=0, no effect.
15. ~~**MMIO write mechanism**~~: **RULED OUT** — rte_write32 is correct.
16. ~~**TXTIME context payload**~~: **RULED OUT** — hex dump verified, all bytes correct.
17. ~~**VFIO vs kernel BAR mapping**~~: **UNLIKELY** — doorbell writes reach HW (int_q_state changes), DMA addresses are valid (dual-doorbell transmits).
18. ~~**TLAN context has TXTIME association**~~: **RULED OUT** — TLAN readback identical, gsc_ena=0 for both.
19. ~~**All queues need TXTIME context**~~: **RULED OUT** — programmed q=0, no effect.
20. ~~**Port stop/start cycle**~~: **RULED OUT** — dev_stop→dev_start doesn't fix it.
21. ~~**Timestamp values wrong or in the past**~~: **RULED OUT (Finding 22)** — timestamps ~1ms in future, TS descriptor bits verified correct.

### Finding 21: testpmd analysis — same failure, different timestamp source

Detailed comparison in `TESTPMD_VS_TXTIME_TEST_ANALYSIS.md`.

**Key findings**:
- testpmd with `--tx-offloads=0x200000 --force-max-simd-bitwidth=64` uses the **full TX path** (`ice_xmit_pkts`), NOT the simple path. The SEND_ON_TIMESTAMP offload prevents simple mode.
- testpmd builds TS descriptors and writes `ts_id` to `TXTIME_DBELL_LSB` — **identical to txtime_test**.
- testpmd uses `rte_eth_read_clock()` → `clock_gettime(CLOCK_MONOTONIC_RAW)` for timestamp base. txtime_test uses `rte_eth_timesync_read_time()` → `GLTSYN_TIME` (PHC).
- Both produce valid 19-bit sub-second timestamps via `(txtime % 1e9) >> 7`.
- **Both get `opackets=0`** — testpmd's "TX-dropped" = our opackets=0. The developer saying "everything is ok" reportedly means the TX function executes without error, not that packets egress.
- testpmd does NOT call `rte_eth_timesync_enable()` — PHC may not be initialized.

**Conclusion**: testpmd does NOT prove TXTIME works. The identical failure confirms the issue is in HW/FW queue initialization, not in timestamp values or TX function selection.

### Finding 22: Timestamp values CONFIRMED correct and in the future (session 4)
```
TXTIME TX[0]: txtime=1775631315878019695 txtime_nsec=878019695 tstamp_19b=43784
             desc_tx_id=1 ts_id=0 ts_desc=0x15610001
             phc=1775631315877033221 phc_nsec=877033221 phc_19b=36078
             delta_nsec=986474 delta_19b=7706
```
Fixed the PHC diagnostic to read full 64-bit PHC time via E830 alias registers (`E830_GLTSYN_TIME_L_0_AL` + `E830_GLTSYN_TIME_H_0_AL`), extract ns-within-second, and compare with TS stamp.

**Results** (10 samples, all consistent):
- **`delta_nsec ≈ +980,000 ns` (~1ms)** — timestamps are ~1ms in the future. Correct.
- **`tstamp_19b > phc_19b`** — TS stamp ahead of PHC in 19-bit space. Correct.
- TS descriptor bit layout verified: `0x15610001` → `tx_desc_idx=1, tstamp_19b=43784`. Matches `(878019695 >> 7) & 0x7FFFF = 43784`. Perfect.
- PHC timer confirmed: both app (`rte_eth_timesync_read_time`) and HW register read give consistent values.

**Previous diagnostic was WRONG**: Earlier session used `GLTSYN_TIME_L(0)` at `0x000888D0` — the legacy register. On E830, this is the lower 32 bits of a flat 64-bit ns counter, NOT ns-within-second. The comparison was apples-to-oranges, producing false negative `delta_19b` values.

**Conclusion**: Timestamps are NOT the problem. TS descriptors contain correct future timestamps ~1ms ahead of PHC. **RULED OUT.**

## 9. Next Steps (prioritized) — SESSION 4 PLAN

### Problem summary after 25 attempts

Everything we can verify is correct: TXTIME context, TLAN context, timestamps, doorbell addresses, PHC timer, AQ commands. The TXTIME engine processes TS descriptors (int_q_state advances) but never triggers data DMA. The root cause is in **FW-internal state** that we cannot read or set via any known register/AQ command.

### Tier 1: Compare kernel vs DPDK init at PCIe level (NEW APPROACH)

The only remaining path is to capture the **exact sequence of AQ commands and register writes** that the kernel sends vs DPDK, and find the difference.

1. **mmiotrace on kernel ice driver** — capture ALL MMIO writes during TXTIME queue setup
   - `CONFIG_MMIOTRACE=y` is confirmed available on this system
   - Bind `ca:00.0` to kernel ice driver
   - Enable mmiotrace → bring interface up → configure ETF qdisc with `offload on` → capture
   - Output: every `wr32` / `writel` with register address + value, in order
   ```bash
   echo mmiotrace > /sys/kernel/debug/tracing/current_tracer
   echo 1 > /sys/kernel/debug/tracing/tracing_on
   # ... bind ca:00.0 to ice, ifconfig up, tc qdisc add etf ...
   echo 0 > /sys/kernel/debug/tracing/tracing_on
   cat /sys/kernel/debug/tracing/trace > kernel_mmio.log
   echo nop > /sys/kernel/debug/tracing/current_tracer
   ```
   - **Limitation**: Kernel driver only. Cannot capture DPDK MMIO writes (userspace mmap).

2. **bpftrace on kernel ice AQ commands** — capture all AQ opcodes + params
   - `bpftrace` is installed, ice module is loaded
   - Trace `ice_sq_send_cmd` to capture every AQ command during TXTIME enable
   ```bash
   sudo bpftrace -e '
   kprobe:ice_sq_send_cmd {
     $desc = (struct ice_aq_desc *)arg2;
     printf("AQ op=0x%04x flags=0x%04x p0=0x%08x p1=0x%08x dlen=%d\n",
       $desc->opcode, $desc->flags,
       $desc->params.raw.param0, $desc->params.raw.param1,
       $desc->datalen);
   }'
   ```

3. **DPDK AQ debug logging** — enable `hw->debug_mask |= ICE_DBG_AQ_DESC | ICE_DBG_AQ_DESC_BUF`
   - DPDK's `ice_debug_cq()` already logs every AQ command with opcode, flags, params, and buffer hex dump
   - Set `hw->debug_mask` in `ice_dev_init()` or via `--log-level=pmd.net.ice:debug`
   - Compare output side-by-side with kernel bpftrace capture

4. **DPDK wr32 instrumentation** — add logging to `wr32()` macro in `ice_osdep.h`
   - Captures every register write with offset + value during queue setup
   - Compare register-by-register with kernel mmiotrace output
   - Filter to only TXTIME-related registers (0x002Dxxxx, 0x002Exxxx, 0x002Cxxxx range)

5. **Diff the two traces** — the AQ command or register write present in kernel but absent in DPDK is the root cause

### Tier 2: FW-level investigation

6. **ice driver tracepoints** — 30 tracepoints available including `ice_print_adminq_desc`
   ```bash
   sudo trace-cmd record -e ice:ice_print_adminq_desc -e ice:ice_print_adminq_msg
   ```

7. **FW debug logging** — ice-2.5.4 has `fwlog` debugfs interface
   - `ca:00.1` is kernel-bound; can capture FW logs from the device's perspective
   - Set `txq`, `scheduler`, `adminq` modules to verbose
   - Binary format (may need Intel tools to decode)

8. **Kernel-first test** — bind ca:00.0 to kernel, enable TXTIME, then switch to DPDK
   - Tests if residual FW state from kernel init enables DPDK TS-only doorbell
   - If yes → narrows root cause to FW init state. diff kernel vs DPDK AQ sequences.
   - If no → FW state resets on unbind. Need to replicate kernel init exactly.

### Tier 3: Nuclear options

9. **CORER reset** + full re-init before TXTIME setup
10. **PCIe hardware analyzer** (if available) — Teledyne LeCroy, Keysight

### Ruled out (do not retry)
- Timestamp values (Finding 22: confirmed correct, ~1ms in future)
- TLAN context fields (Finding 18: identical)
- TXTIME context fields (Finding 13: hex dump verified)
- All-queue TXTIME ctx (Finding 19: no effect)
- Port stop/start (Finding 20: no effect)
- AQ 0x0C37 enable (Finding 12: no effect)
- MDD / HEAD state (Findings 4, 7: clean)
- Doorbell register mapping (Finding 17: correct)
- FETCH_PROFILE / WRR (Findings 9, 10: kernel also 0)

## 10. ROOT CAUSE & FIX (Session 5)

### Finding 23: TXTIME Doorbell Init Write Causes MDD Event 29

**Root cause**: In `ice_tx_queue_start()`, DPDK wrote 0 to the TXTIME doorbell (`E830_GLQTX_TXTIME_DBELL_LSB`) immediately after `ice_aq_set_txtimeq()`. This rings the doorbell with ts_id=0 while the TS ring is empty/uninitialized, causing PQM to raise MDD event 29.

**Discovery method**: PCIe trace comparison (kernel mmiotrace vs DPDK wr32 instrumentation) revealed that:
- Kernel NEVER writes TXTIME doorbell during queue start — only assigns pointer (`tstamp_ring->tail`)
- DPDK was writing 0 to it after `aq_set_txtimeq`
- Removing this single write fixed everything

**Fix**: In `ice_rxtx.c:ice_tx_queue_start()`, TXTIME branch:
```c
// Before (broken):
txq->qtx_tail = hw->hw_addr + E830_GLQTX_TXTIME_DBELL_LSB(txq->reg_idx);
ICE_PCI_REG_WRITE(txq->qtx_tail, 0);  // <-- triggers MDD event 29

// After (fixed):
txq->qtx_tail = hw->hw_addr + E830_GLQTX_TXTIME_DBELL_LSB(txq->reg_idx);
// Do NOT write 0 — kernel never does this, it only assigns the pointer
```

**Test results** (verified twice):
- Test 0 (sanity): opackets=3 ✅
- Tests 1-10 (TXTIME offsets): opackets=10 each ✅
- Test 11 (MTL-style 100 packets): opackets=100, 100/100 sent ✅
- MDD events: ZERO ✅

### Why This Was Hard to Find

- The doorbell init write was modeled after the regular TX queue path (`QTX_COMM_DBELL` write 0), which is correct for data doorbells
- TXTIME doorbells have different semantics: they carry TS descriptor IDs, and writing 0 is a valid ts_id that triggers processing
- The MDD event was logged but its immediate connection to the doorbell write was obscured by 25+ other hypotheses being tested in parallel
- No documentation exists for PQM MAL_TYPE=29 meaning "invalid TS descriptor access on empty ring"

## 11. Diagnostic Cheat Sheet

### Key registers to read

| Register | Address | What it tells you |
|----------|---------|-------------------|
| `QTX_COMM_HEAD(q)` | `0x0E4000+q*4` | Data queue HEAD — advances when packets transmit |
| `QTX_COMM_DBELL(q)` | `0x2C0000+q*4` | Data doorbell (write-only, read=garbage) |
| `E830_GLQTX_TXTIME_DBELL_LSB(q)` | `0x2E0000+q*8` | TS doorbell (write-only, read=garbage) |
| `E830_GLTXTIME_QTX_CNTX_CTL` | `0x2D2630` | Write `(q\|0x80000)` to latch TXTIME context |
| `E830_GLTXTIME_QTX_CNTX_DATA(i)` | `0x2D2600+i*4` | 7 dwords of TXTIME context (read after CTL write) |
| `E830_GLTXTIME_TS_CFG` | `0x2D0000` | Global TXTIME enable (bit 0) |

### How to read TXTIME int_q_state
```
Write QTX_CNTX_CTL = (queue_id & 0x7FF) | (1 << 19)
Read data[0..6]
int_q_state = bits[126:195] = data[3] bits[30:31] + data[4] + data[5] + data[6] bits[0:5]
Low bits of int_q_state = TXTIME internal TAIL (should match last ts_id written to doorbell)
data[4] = TXTIME internal HEAD (should advance as engine processes TS descs)
```

## 12. Finding 24: DPDK Packets Transmit but NOT Scheduled (Session 6)

### Wire-level HW timestamp analysis

After the Finding 23 fix (removing doorbell init write), all 12 tests pass (opackets>0). However, **HW-timestamped pcap analysis proves the TXTIME HW scheduler is NOT delaying packets to their launch time.**

**Test setup**: E830 TX → 100G cable → E810 RX (eth0) with `tcpdump -j adapter_unsynced` for raw NIC HW timestamps.

**Test 11 (MTL-style stream, 100 pkts, 7936ns planned step)**:
```
R²            = 0.000000
Mean |error|  = 11,364 ns
Within 1µs    = 0/99 (0%)
Within 5µs    = 0/99 (0%)
Verdict       = NO SCHEDULING
```

Packets arrive in bursts of 4 at wire speed (~0ns gap within group, ~31µs between groups) — matching `g_stream_bulk=4`. The ~31µs inter-group gap is software TX ring refill time, NOT HW scheduling.

**Test 8 (+1s future offset)**: All 10 packets arrive in 1.9µs. Should have been held ~1 second by HW scheduler.

**Comparison**: Kernel ETF qdisc achieves **R²=0.999818** with sub-µs scheduling on the same NIC.

**Conclusion**: The TXTIME engine processes TS descriptors (int_q_state advances) and triggers data DMA (opackets>0), but does NOT apply hold-until-launch-time logic. The scheduling linkage between TS engine and data queue is missing in DPDK's incremental queue setup. The kernel's full VSI rebuild (`ice_vsi_close` → `ice_vsi_rebuild` → `ice_vsi_open`) establishes FW-internal state that DPDK does not replicate.

Full results: `DPDK_TXTIME_SCHEDULING_RESULTS.md`

## 13. Files

| File | Purpose |
|------|---------|
| `dpdk-25.11/drivers/net/intel/ice/ice_rxtx.c` | TXTIME queue start, TX path, diagnostics |
| `dpdk-25.11/drivers/net/intel/ice/ice_ethdev.c` | PHC init, MDD clearing, scheduler rebuild, fetch profile |
| `tools/txtime_test/txtime_test.c` | DPDK TXTIME test (12 tests + hold test + register dump) |
| `tools/txtime_test/parse_txtime_pcap.py` | Pcap parser with R² gap analysis |
| `tools/txtime_test/read_txtime_regs.c` | Standalone TXTIME register dump tool (no DPDK needed) |
| `tools/txtime_test/kernel_txtime_test.c` | Kernel SO_TXTIME reference test |
| `tools/txtime_test/RESULTS.md` | Detailed test results and doorbell matrix |
| `tools/txtime_test/DPDK_TXTIME_SCHEDULING_RESULTS.md` | Wire-level HW timestamp analysis results |
| `tools/txtime_test/dpdk_regdump_session6.txt` | DPDK register dump output from session 7 |
| `tools/dpdk-report/txtime_edge_test.c` | 32 edge-case tests |
| `tools/dpdk-report/parse_edge_pcap.py` | Edge test pcap parser |
| `ice-2.5.4/src/ice_base.c` | Kernel queue config (`ice_vsi_cfg_txq`, `ice_calc_ts_ring_count`) |
| `ice-2.5.4/src/ice_main.c` | Kernel TXTIME enable (`ice_vsi_cfg_txtime`) |
| `ice-2.5.4/src/ice_txrx.c` | Kernel TX path (TS doorbell write) |

## 14. Session 7 Findings (2026-04-13)

### Finding 25: Clean DPDK (9 patches) also gives R²=0

Rebuilt from `script/build_dpdk.sh` (clean 9-patch DPDK, no custom `ice_dev_start()` code).
This version has `ice_aq_ena_dis_txtimeq(enable=1)` after `ice_aq_set_txtimeq()`.
**Result**: All 12 tests pass but R²=0.000 — same as workspace DPDK.

**Conclusion**: The `ice_aq_ena_dis_txtimeq(enable=1)` call and the ~270 lines of custom init code are
NOT the difference. The issue is deeper — in the DPDK queue setup path itself.

### Finding 26: Kernel-first test — FW state doesn't carry over

1. Bound ca:00.0 to kernel ice
2. Enabled ETF qdisc with offload (`tc qdisc replace dev eth2 parent root handle 100 etf clockid CLOCK_TAI delta 500000 offload`)
3. dmesg confirmed `enable TxTime on queue: 0`
4. Unbound from kernel WITHOUT removing ETF qdisc (to preserve FW state)
5. Bound to vfio-pci, ran txtime_test
6. **Result**: R²=0.000

**Conclusion**: DPDK's `ice_init_hw()` during port init resets the PF state,
destroying whatever FW-internal TXTIME scheduling linkage the kernel established.
Residual FW state from kernel does NOT help DPDK.

### Finding 27: Hold test — DEFINITIVE proof of no scheduling

Added "Test 12: Hold test" to txtime_test:
- Sends 1 packet at PHC + 50ms (far enough in the future to measure)
- Polls `opackets` every 100µs
- Measures wall time until `opackets` increments

**Result**:
```
opackets=1 at 221 us — NO HOLD (immediate TX)
Expected: ~50000 us if HW scheduling active
Verdict: SCHEDULING NOT ACTIVE
```

The packet was transmitted **immediately** (221µs ≈ software overhead), not held until its
50ms-future launch time. If HW scheduling were active, opackets would remain 0 until ~50ms.

### Finding 28: EVERY readable TXTIME register is IDENTICAL between kernel and DPDK

Created `read_txtime_regs.c` — standalone tool that mmaps BAR0 via sysfs and reads all
TXTIME-related registers. Compared kernel (with ETF qdisc + offload active) vs DPDK
(after `ice_tx_queue_start` + `ice_aq_set_txtimeq`):

```
Register                 | DPDK (q=1)        | Kernel (q=0)      | Match
-------------------------|-------------------|-------------------|------
TS_CFG                   | 0x00000001 (EN=1) | 0x00000001 (EN=1) | ✅
FETCH_PROFILE[0,0]       | 0x00000000        | 0x00000000        | ✅
WRR_CREDITS              | 0x00000000        | 0x00000000        | ✅
WRR_WEIGHTS              | 0x00000000        | 0x00000000        | ✅
OUTST_REQ_CNTL           | 0x00000200 (T=512)| 0x00000200 (T=512)| ✅
GL_MDET_TX_PQM           | 0x00000000        | 0x00000000        | ✅
txtime_ena_q             | 1                 | 1                 | ✅
drbell_mode_32           | 1                 | 1                 | ✅
ts_res                   | 7 (128ns)         | 7 (128ns)          | ✅
ts_round_type            | 0                 | 0                 | ✅
ts_pacing_slot           | 0                 | 0                 | ✅
merging_ena              | 0                 | 0                 | ✅
ts_fetch_prof_id         | 0                 | 0                 | ✅
```

Non-TXTIME queues also match: kernel q=1..7 have `ena=0` with same field layout as DPDK q=0.

**Conclusion**: The TXTIME MMIO-readable context is identical. The difference is in
**FW-internal state that is NOT exposed via any MMIO register**. This state is set
via Admin Queue commands. The root cause must be a missing or different AQ command
in the DPDK queue setup sequence vs the kernel's full VSI rebuild.

### Finding 29: DPDK init sequence vs kernel — AQ command differences

**Kernel sequence** (on ETF qdisc `offload`):
```
1. ice_offload_txtime()
2. ice_vsi_close()         → disables ALL queues
3. ice_vsi_rebuild()       → tears down + recreates scheduler tree
   a. ice_rm_vsi_lan_cfg() → AQ: remove scheduler nodes
   b. ice_cfg_vsi_lan()    → AQ: add scheduler nodes (VSI → QG → leaf)
4. ice_vsi_open()          → for EACH queue:
   a. ice_vsi_cfg_txq()    → AQ 0x0C30 (add_lan_txq) with TLAN context
   b.                      → AQ 0x0404 (move_sched_elems) — attach to scheduler tree
   c.                      → AQ 0x0C35 (set_txtimeq) with TXTIME context
```

**DPDK sequence** (clean script build):
```
1. ice_dev_start()
2. ice_tx_queue_start() per queue:
   a. ice_ena_vsi_txq()    → AQ 0x0C30 (add_lan_txq) with TLAN context
   b.                      → AQ 0x0404 (move_sched_elems) — has this too
   c. ice_aq_set_txtimeq() → AQ 0x0C35 (set_txtimeq) with TXTIME context
   d. ice_aq_ena_dis_txtimeq(enable=1) → AQ 0x0C37 (script build only)
```

**Visible differences**:
1. Kernel does full VSI close→rebuild→open; DPDK does incremental per-queue add
2. Kernel calls `ice_rm_vsi_lan_cfg()` + `ice_cfg_vsi_lan()` to rebuild scheduler tree
3. DPDK may or may not do scheduler rebuild (workspace version does, script version doesn't)
4. Kernel never calls AQ 0x0C37; DPDK script version calls it

**KEY UNKNOWN**: The exact FW state that `ice_vsi_rebuild()` establishes internally
that links the TXTIME engine to the data queue's DMA controller. This state is NOT
visible via any MMIO register or AQ response.

### What We Tried (session 7)

| # | Attempt | Result |
|---|---------|--------|
| 26 | Clean 9-patch DPDK rebuild + install | R²=0, enable call doesn't help |
| 27 | Kernel-first test (ETF→unbind→DPDK) | R²=0, FW state reset on DPDK init |
| 28 | Hold test (1 pkt at PHC+50ms, poll opackets) | TX at 221µs, not 50ms — no hold |
| 29 | Side-by-side MMIO register comparison kernel vs DPDK | ALL registers identical |

### Remaining Approach: AQ Command Trace Diff

Since all MMIO-readable state matches but behavior differs, the root cause is in
**FW-internal state set via AQ commands**. The next step is:

1. **bpftrace kernel AQ commands** — capture every AQ opcode + buffer during ETF enable
2. **DPDK AQ debug log** — enable `ICE_DBG_AQ_DESC | ICE_DBG_AQ_DESC_BUF` via
   `hw_debug_mask` devarg or code change
3. **Diff the two traces** byte-by-byte — find the AQ command present in kernel
   but absent/different in DPDK

The AQ command buffer for `0x0C30` (add_lan_txq) is the most likely candidate —
it contains the **TLAN context** which may have AQ-only fields (not in the MMIO
readback) that link data queues to the TXTIME engine.

## 15. Debug Tools

### `read_txtime_regs.c` — Standalone TXTIME Register Dump

Works with either kernel or DPDK (reads BAR0 via sysfs resource0 mmap).

```bash
# Build
gcc -O2 -o read_txtime_regs read_txtime_regs.c

# Usage (requires root)
sudo ./read_txtime_regs 0000:ca:00.0 "label"
```

Reads: `TS_CFG`, `FETCH_PROFILE`, `WRR_CREDITS/WEIGHTS`, `OUTST_REQ_CNTL`,
`GL_MDET_TX_PQM/FIFO`, TXTIME contexts for q=0..7, `QTX_COMM_HEAD` for q=0..3.

### `txtime_test.c` — BAR0 Register Dump + Hold Test

The test tool now has:
- **BAR0 mapping** via sysfs `resource0` (works with vfio-pci)
- **`dump_txtime_regs(label)`** — reads all TXTIME registers, called before and after tests
- **`dump_txtime_context(queue)`** — reads per-queue TXTIME context via CNTX_CTL/DATA
- **Test 12: Hold test** — sends 1 packet at PHC+50ms, polls opackets every 100µs,
  measures wall clock until TX. Reports SCHEDULING ACTIVE/NOT ACTIVE.

### `setup_kernel_txtime.sh` — Kernel ETF Qdisc Setup

```bash
# Direct ETF root qdisc (simplest)
sudo tc qdisc replace dev eth2 parent root handle 100 etf clockid CLOCK_TAI delta 500000 offload
# Verify: dmesg | grep "enable TxTime"
```

## 16. SOLUTION FOUND — Session 8 (2026-04-13)

### Finding 30: Double-start (stop→start) enables TXTIME HW scheduling — R² = 1.000000

**The fix**: Perform `dev_stop → dev_start` before running TXTIME tests. The workspace DPDK (with scheduler rebuild in `ice_dev_start()`) achieves **R²=1.000000** with sub-µs per-packet scheduling accuracy.

**Results** (100-packet stream, 7936ns planned step):
```
Fitted slope     : 7935.8 ns/pkt (planned: 7936)
R²               : 1.000000
Mean |residual|  : 103.1 ns
Max |residual|   : 473.2 ns
Within 1 µs      : 99/99 (100%)
VERDICT          : EXCELLENT HW SCHEDULING
```

**Comparison with kernel**: Kernel ETF qdisc achieves R²=0.999818. **DPDK now matches or exceeds kernel accuracy.**

### Finding 31: FW version 7.9.5 api 1.7.11 nvm 1.20

E830 ca:00.0: `fw 7.9.5 api 1.7.11 nvm 1.20 0x80017ef4 1.3909.0`. Kernel ice: `Kahawai_2.2.8`.

### Finding 32: Scheduler rebuild needed for per-packet precision

| Configuration | R² | Mean residual | Scheduling |
|---|---|---|---|
| Workspace DPDK + double-start | **1.000000** | **103 ns** | Per-packet (sub-µs) |
| Clean DPDK + double-start | 0.998586 | 7,725 ns | Per-burst (groups of 4) |
| Any DPDK, no double-start | 0.000 | N/A | None |

**Without scheduler rebuild** (clean DPDK): 0x0C31 alone enables coarse scheduling (burst-level, R²≈0.999). Packets in each `tx_burst` group arrive at wire speed, with correct inter-group spacing.

**With scheduler rebuild** (workspace DPDK): 0x0C31 + 0x040F + 0x0401 enables per-packet scheduling (R²=1.000, 103ns mean error). Each individual packet arrives at its correct launch time.

### Root Cause

The E830 FW requires a **queue disable → scheduler rebuild → queue re-add** sequence to establish the internal linkage between the TXTIME scheduling engine and the data queue's DMA controller.

The critical AQ command sequence:
```
AQ 0x0C31 (dis_txqs)         — disable existing queues (provides FW state transition)
AQ 0x040F (delete_sched_elems) — remove scheduler tree nodes
AQ 0x0401 (add_sched_elems)   — rebuild scheduler tree with TXTIME-aware nodes
AQ 0x0C30 (add_txqs)          — re-add queues with TLAN context
AQ 0x0C35 (set_txtimeqs)      — set TXTIME context
```

On first `ice_dev_start()`, no queues exist to disable (0x0C31 has nothing to act on), so the FW never establishes the TXTIME→data DMA linkage. A `dev_stop → dev_start` cycle provides the missing 0x0C31 step.

### Fix for Upstream DPDK

The fix should be integrated into `ice_dev_start()` for TXTIME ports:
1. On first start with `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP`: create dummy queues → disable them (0x0C31) → rebuild scheduler → re-add real queues
2. Or simply require apps to call `rte_eth_dev_stop → rte_eth_dev_start` after initial `rte_eth_dev_start`

Detailed notes: `tools/txtime_test/SESSION8_NOTES.md`

### What We Tried (session 8)

| # | Attempt | Result |
|---|---------|--------|
| 30 | Workspace DPDK + double-start | **R²=1.000, 103ns mean error — SOLVED** |
| 31 | FW version check | 7.9.5, first time captured |
| 32 | Clean DPDK + double-start | R²=0.999, burst-level scheduling only |
