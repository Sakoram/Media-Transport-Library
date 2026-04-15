# testpmd vs txtime_test: E830 TXTIME Analysis

## Executive Summary

**testpmd does NOT actually prove TXTIME works.** All packets are TX-dropped because they
never leave the wire — but both testpmd and txtime_test share the same `opackets=0` behavior.
The developer saying "everything is ok" likely refers to testpmd's TX function executing
without errors, NOT that packets actually egress with launch-time scheduling.

The key difference is the **timestamp source**: testpmd uses `CLOCK_MONOTONIC_RAW` (system time)
via `rte_eth_read_clock()`, while txtime_test uses PHC time via `rte_eth_timesync_read_time()`.
Both ultimately produce a 19-bit sub-second timestamp shifted to 128ns resolution —
the difference matters only if HW compares the timestamp against a specific time base.

---

## 1. TX Path Selection

### testpmd command
```bash
sudo ./build/app/dpdk-testpmd -c f -n 4 -a 0000:ca:00.0 \
  --force-max-simd-bitwidth=64 -- -i --tx-offloads=0x200000
```

### Which TX function is selected?

In `ice_set_tx_function()` (`ice_rxtx.c:4125`):

1. **Vector path check**: `rte_vect_get_max_simd_bitwidth()` returns 64 (from `--force-max-simd-bitwidth=64`).
   The condition `>= RTE_VECT_SIMD_128` is **FALSE** → `tx_vec_allowed = false`.

2. **Simple path check** (`ice_set_tx_function_flag`, line 3923):
   ```c
   ad->tx_simple_allowed =
       (txq->offloads == (txq->offloads & RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE) &&
        txq->tx_rs_thresh >= ICE_TX_MAX_BURST);
   ```
   With `--tx-offloads=0x200000` (`RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP`):
   - `txq->offloads & MBUF_FAST_FREE` = `0x200000 & 0x10000` = 0
   - `txq->offloads == 0` → **FALSE** → `tx_simple_allowed = false`

3. **Result**: Falls through to the **full scalar path**:
   ```c
   dev->tx_pkt_burst = ice_xmit_pkts;  // The FULL path with TS descriptor handling
   ```

**Conclusion**: `--force-max-simd-bitwidth=64` does NOT force the simple path.
The SEND_ON_TIMESTAMP offload already prevents simple mode. testpmd uses the same
`ice_xmit_pkts` full path that txtime_test uses.

---

## 2. Timestamp Source Comparison

### testpmd: `rte_eth_read_clock()` → `CLOCK_MONOTONIC_RAW`

In `txonly.c:441`:
```c
timestamp_enable = tx_pkt_times_inter &&
                   timestamp_mask &&
                   timestamp_off >= 0 &&
                   !rte_eth_read_clock(pi, &timestamp_initial[pi]);
```

The ice driver implementation (`ice_ethdev.c:7694`):
```c
ice_read_clock(__rte_unused struct rte_eth_dev *dev, uint64_t *clock)
{
    struct timespec system_time;
    clock_gettime(CLOCK_MONOTONIC_RAW, &system_time);
    *clock = system_time.tv_sec * NSEC_PER_SEC + system_time.tv_nsec;
    return 0;
}
```

**`rte_eth_read_clock()` returns `CLOCK_MONOTONIC_RAW` system nanoseconds, NOT PHC time!**

### txtime_test: `rte_eth_timesync_read_time()` → GLTSYN_TIME (PHC)

In `txtime_test.c:195`:
```c
static uint64_t read_phc(void) {
    struct timespec ts;
    rte_eth_timesync_read_time(g_port, &ts);
    return timespec_to_ns(&ts);
}
```

The ice driver implementation (`ice_ethdev.c:7642`):
```c
ice_timesync_read_time(struct rte_eth_dev *dev, struct timespec *ts)
{
    uint32_t hi, lo;
    lo = ICE_READ_REG(hw, GLTSYN_TIME_L(tmr_idx));
    hi = ICE_READ_REG(hw, GLTSYN_TIME_H(tmr_idx));
    time = ((uint64_t)hi << 32) | lo;
    *ts = rte_ns_to_timespec(time);
}
```

### Impact on timestamp extraction

The TX datapath (`ice_rxtx.c:3393`) extracts only sub-second nanoseconds:
```c
uint32_t tstamp = (uint32_t)(txtime % NS_PER_S) >> ICE_TXTIME_CTX_RESOLUTION_128NS;
// = (txtime % 1_000_000_000) >> 7
```

This produces a 19-bit value (0–7,812,499) representing the sub-second time in 128ns ticks.

| Property | testpmd (MONOTONIC_RAW) | txtime_test (PHC) |
|----------|------------------------|-------------------|
| Time source | `clock_gettime(CLOCK_MONOTONIC_RAW)` | `GLTSYN_TIME_L/H` registers |
| Epoch | System boot | PTP epoch (or free-run from NIC init) |
| Example value | ~93,000,000,000,000 ns | ~1,750,000,000,000,000,000 ns |
| After `% NS_PER_S` | sub-second fraction of system time | sub-second fraction of PHC time |
| After `>> 7` | valid 19-bit TS value | valid 19-bit TS value |

**Both produce valid 19-bit timestamp values**, but they reference different time bases.
If the HW TXTIME engine compares the TS descriptor timestamp against the PHC's sub-second
counter, then MONOTONIC_RAW timestamps are effectively random relative to PHC time.

---

## 3. Timestamp Computation Detail

### testpmd timestamp formula

```
timestamp_initial[port] = rte_eth_read_clock() = CLOCK_MONOTONIC_RAW in ns

For first burst (skew init):
  skew = timestamp_initial + tx_pkt_times_inter + phase
  phase = tx_pkt_times_inter * queue_idx / nb_queues

For each subsequent burst:
  pkt[0]: skew += tx_pkt_times_inter (30,000,000 ns = 30 ms)
  pkt[i]: skew += tx_pkt_times_intra (1,000,000 ns = 1 ms)

Packet dynfield = skew (absolute MONOTONIC_RAW ns)
```

### txtime_test timestamp formula

```
phc_ns = rte_eth_timesync_read_time() = GLTSYN_TIME (PHC ns)

launch = phc_ns + offset_ns
  offset_ns varies by test (e.g., +5ms, +50ms, etc.)

Packet dynfield = launch (absolute PHC ns)
```

### Key difference

| Aspect | testpmd | txtime_test |
|--------|---------|-------------|
| Base time | MONOTONIC_RAW at `start` cmd | PHC at burst build time |
| Offset from "now" | `tx_pkt_times_inter` (30ms) | `g_stream_offset_ms` (5ms) |
| Increments | Inter-burst: 30ms, Intra-burst: 1ms | Per-pkt: `g_stream_step_ns` (7936ns) |
| Time base drift | Monotonic drifts vs PHC over time | Always fresh PHC read |

---

## 4. TX Doorbell and TS Descriptor

Both testpmd and txtime_test use the same code path in `ice_xmit_pkts` (line 3391):

```c
if (txq->tsq != NULL && txq->tsq->ts_flag > 0) {
    // Read timestamp from mbuf dynfield
    uint64_t txtime = *RTE_MBUF_DYNFIELD(tx_pkt, txq->tsq->ts_offset, uint64_t *);
    // Extract sub-second 128ns ticks
    uint32_t tstamp = (uint32_t)(txtime % NS_PER_S) >> ICE_TXTIME_CTX_RESOLUTION_128NS;
    // desc_tx_id: maps TS desc to data desc (wraps 0 → nb_tx_desc)
    const uint32_t desc_tx_id = (tx_id == 0) ? txq->nb_tx_desc : tx_id;
    // Build TS descriptor: [desc_tx_id | tstamp]
    __le32 ts_desc = rte_cpu_to_le_32(
        FIELD_PREP(ICE_TXTIME_TX_DESC_IDX_M, desc_tx_id) |
        FIELD_PREP(ICE_TXTIME_STAMP_M, tstamp));
    txq->tsq->ice_ts_ring[ts_id].tx_desc_idx_tstamp = ts_desc;
    ts_id++;
    // TS ring wrap-around with fetch padding...
}

// Doorbell: write ts_id to TXTIME_DBELL_LSB
ICE_PCI_REG_WRITE(txq->qtx_tail, ts_id);
```

**Both apps write identical register/descriptor sequences.** The only variable is the
timestamp value embedded in the TS descriptor.

---

## 5. What "TX-dropped" Means

testpmd reports all packets as TX-dropped. This is consistent with our txtime_test
showing `opackets=0`. The packets are:

1. ✅ Accepted by `rte_eth_tx_burst()` (return value = count)
2. ✅ Written to data descriptor ring
3. ✅ Written to TS descriptor ring
4. ✅ TXTIME_DBELL doorbell rung (with ts_id)
5. ❌ Never DMA'd by HW → `opackets` stays 0

testpmd counts packets as "dropped" when `rte_eth_tx_burst()` returns fewer than
requested, OR when freed during cleanup without having been transmitted. The latter
is what happens — HW never processes the descriptors.

---

## 6. Why `--force-max-simd-bitwidth=64`?

The developer likely uses this flag to:
1. Force deterministic behavior (no AVX2/AVX512 vector paths)
2. Ensure the scalar `ice_xmit_pkts` path is selected, which is the only path
   that handles TS descriptors

Without this flag, the vector paths (AVX2/AVX512) would be selected on x86, and
these **do NOT support TS descriptors** — they'd skip TXTIME entirely and write to
`QTX_COMM_DBELL` instead of `TXTIME_DBELL`.

Actually, the offload flag alone (`0x200000`) already prevents vector paths via
`ice_tx_vec_dev_check()`, but the `--force-max-simd-bitwidth=64` is belt-and-suspenders.

---

## 7. Differences Summary

| Aspect | testpmd | txtime_test | Impact |
|--------|---------|-------------|--------|
| TX function | `ice_xmit_pkts` (full) | `ice_xmit_pkts` (full) | **Same** |
| TS descriptors | Yes | Yes | **Same** |
| Doorbell | `TXTIME_DBELL_LSB` | `TXTIME_DBELL_LSB` | **Same** |
| Doorbell value | `ts_id` | `ts_id` | **Same** |
| Timestamp source | `CLOCK_MONOTONIC_RAW` | PHC (`GLTSYN_TIME`) | **DIFFERENT** |
| Timestamp unit | nanoseconds | nanoseconds | **Same** |
| TS desc value | `(monotonic_ns % 1e9) >> 7` | `(phc_ns % 1e9) >> 7` | **DIFFERENT** |
| Result: opackets | 0 (TX-dropped) | 0 | **Same failure** |
| Port stop/start | No | Yes (attempted) | Not relevant |
| PHC init | No `timesync_enable()` | Yes, with retry | **DIFFERENT** |
| Burst pattern | Continuous at 30ms inter-burst | Single burst, wait 3s | **DIFFERENT** |

---

## 8. Conclusions

### 8.1 testpmd does NOT prove TXTIME works
Both testpmd and txtime_test exhibit identical failure: `opackets=0`. The developer
saying "everything is ok" may mean:
- The TX function path is correctly selected (vector disabled, TS desc built)
- No crashes, no MDD events, no errors
- The TX-dropped count rises as expected when TXTIME holds packets

### 8.2 Timestamp time-base is different but irrelevant to the core failure
The MONOTONIC_RAW vs PHC difference would matter if HW actually processed the
TS descriptors — it would determine *when* packets egress. But since HW isn't
processing them at all (opackets=0), this difference is moot for debugging the
core issue.

### 8.3 testpmd skips PHC initialization
testpmd does NOT call `rte_eth_timesync_enable()`. This means:
- GLTSYN_ENA may not be set
- GLTSYN_INCVAL may not be programmed
- The PHC counter may not be running

However, the TXTIME engine might use a different timer than the PHC. The E830
TS descriptor embeds only the sub-second nanosecond field — HW needs a running
counter to compare against. Whether that counter is the PHC or a separate
free-running timer is not fully documented.

### 8.4 The real problem is upstream of timestamp values
Since both apps write TS descriptors and ring TXTIME_DBELL with the same code
path, and both get `opackets=0`, the issue is NOT in the timestamp values or the
TX function selection. Possible root causes remain:
- FW TXTIME engine not activated / missing queue association
- Missing scheduler node configuration for TXTIME queues
- AQ sequence difference from kernel (ice_vsi_cfg_txtime does more than just set context)
- TXTIME_DBELL not being read by HW (wrong queue state)

### 8.5 Next steps
1. **Try testpmd without `--force-max-simd-bitwidth=64`** — verify it still uses full path
   (it should, due to SEND_ON_TIMESTAMP offload)
2. **Add MONOTONIC_RAW timestamps to txtime_test** — eliminate timestamp source as a variable
3. **Focus on FW/HW queue state** — the identical failure in both apps points to a HW/FW
   configuration issue, not a software timestamp bug
4. **Compare full AQ command sequences** between kernel ice driver and DPDK during
   queue setup — the kernel does `ice_vsi_cfg_txtime()` which may include additional steps
   beyond what DPDK's `ice_aq_set_txtimeq()` does
