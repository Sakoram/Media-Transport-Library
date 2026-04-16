# E830 TXTIME Tracing Guide

How to capture and compare Admin Queue (AQ) command traces between kernel ice driver and DPDK ice PMD for E830 TXTIME debugging.

## Quick Reference — AQ Opcodes

| Opcode | Name | Direction | Purpose |
|--------|------|-----------|---------|
| `0x0C30` | `add_lan_txq` | → FW | Add TX LAN queue (includes TLAN context) |
| `0x0C31` | `dis_vsi_txq` | → FW | Disable/remove TX queue |
| `0x0C35` | `set_txtimeq` | → FW | Set TXTIME context for a queue |
| `0x0C37` | `ena_dis_txtimeq` | → FW | Enable or disable TXTIME on a queue |
| `0x0404` | `move_sched_elems` | → FW | Move scheduler tree node |
| `0x0600` | `get_phy_caps` | → FW | Get PHY capabilities |
| `0x02A0` | `add_txqs` (generic) | → FW | Generic TX queue add |

## 1. Tracing DPDK AQ Commands

DPDK's ice base driver has built-in AQ debug logging. Enable it via the `hw_debug_mask` devarg.

### Enable AQ Debug Logging

```bash
# ICE_DBG_AQ_DESC (0x04000000) | ICE_DBG_AQ_DESC_BUF (0x02000000) = 0x06000000
sudo ./builddir/txtime_test -l 0-1 \
    -a ca:00.0,hw_debug_mask=0x6000000 \
    --log-level=pmd.net.ice.driver:debug \
    2>&1 | tee /tmp/dpdk_aq_trace.log
```

**Debug mask bits** (from `ice_type.h`):

| Bit | Constant | What it logs |
|-----|----------|-------------|
| `0x04000000` | `ICE_DBG_AQ_DESC` | AQ descriptor (opcode, flags, params) |
| `0x02000000` | `ICE_DBG_AQ_DESC_BUF` | AQ buffer hex dump |
| `0x06000000` | Both | Full AQ trace with buffers |

### Output Format

```
ICE_DRIVER: ice 00.0 AQ Command: opcode 0x0C30, flags 0x3400, datalen 0x0038, retval 0x0000
ICE_DRIVER: ice 00.0    cookie (h,l) 0x00000000 0x00000000
ICE_DRIVER: ice 00.0    param (0,1)  0x00000001 0x00000000
ICE_DRIVER: ice 00.0    addr (h,l)   0x00000019 0xCCBB9BC0
ICE_DRIVER: ice 00.0 Buffer:
ICE_DRIVER: ice 00.0 0x0000  0x0000000100000257
ICE_DRIVER: ice 00.0 0x0008  0x0000000000000001
...
ICE_DRIVER: ice 00.0 AQ Response: opcode 0x0C30, flags 0x3403, datalen 0x0038, retval 0x0000
```

Each AQ command shows: the Command sent, then the Response from FW. `retval 0x0000` = success.

### Extracting TXTIME-Related Commands

```bash
# Find all TXTIME-related AQ commands
grep -n "0x0C30\|0x0C35\|0x0C37\|0x0C31" /tmp/dpdk_aq_trace.log

# Count commands by opcode
for op in 0x0C30 0x0C31 0x0C35 0x0C37 0x0404; do
    echo -n "$op: "; grep -c "AQ Command: opcode $op" /tmp/dpdk_aq_trace.log
done

# Extract full buffer for a specific command at known line number
sed -n '91840,91870p' /tmp/dpdk_aq_trace.log
```

### What You Learn from DPDK Traces

- **Complete AQ command sequence** during port init, queue setup, start/stop
- **TLAN context bytes** in `add_lan_txq` buffers (DPDK compares 1:1 with kernel)
- **TXTIME context bytes** in `set_txtimeq` buffers
- **Scheduler tree operations** via `move_sched_elems`
- **Return values** — whether FW accepted or rejected each command
- **Ordering** — which commands come before/after others

## 2. Tracing Kernel Ice AQ Commands

### Using bpftrace

The kernel ice driver's `ice_aq_send_cmd` is the single bottleneck for all AQ commands. Trace it with bpftrace.

> **Important**: `ice_sq_send_cmd` is **inlined** in ice-2.5.4 and NOT kprobeable. Use `ice_aq_send_cmd` instead (same args).

#### Verify the function is kprobeable

```bash
sudo cat /proc/kallsyms | grep -w ice_aq_send_cmd
# Should show at least one address. If empty, try ice_sq_send_cmd.
```

#### The Bpftrace Script

Save as `/tmp/trace_ice_aq.bt`:

```bpf
#!/usr/bin/env bpftrace

kprobe:ice_aq_send_cmd {
    /* ice_aq_send_cmd(hw, desc, buf, buf_size, cd)
     * arg1 = desc (struct ice_aq_desc *, 32 bytes)
     * arg2 = buf
     * arg3 = buf_size
     *
     * ice_aq_desc layout (little-endian on x86):
     *   [0:2]   opcode (u16)
     *   [2:4]   flags (u16)
     *   [4:6]   datalen (u16)
     *   [6:8]   retval (u16)
     *   [8:16]  cookie (u64)
     *   [16:32] params (16 bytes, shown as two u64)
     */
    $desc = arg1;
    $buf = arg2;
    $buflen = arg3;

    $opcode = *(uint16 *)$desc;
    $flags  = *(uint16 *)($desc + 2);
    $dlen   = *(uint16 *)($desc + 4);

    printf("AQ op=0x%04x fl=0x%04x dl=%d", $opcode, $flags, $dlen);
    printf(" p=%016lx %016lx\n",
           *(uint64 *)($desc + 16), *(uint64 *)($desc + 24));

    if ($buf != 0 && $buflen > 0) {
        printf("  B[%d]:", $buflen);
        if ($buflen >= 8)  { printf(" %016lx", *(uint64 *)$buf); }
        if ($buflen >= 16) { printf(" %016lx", *(uint64 *)($buf + 8)); }
        if ($buflen >= 24) { printf(" %016lx", *(uint64 *)($buf + 16)); }
        if ($buflen >= 32) { printf(" %016lx", *(uint64 *)($buf + 24)); }
        if ($buflen >= 40) { printf(" %016lx", *(uint64 *)($buf + 32)); }
        if ($buflen >= 48) { printf(" %016lx", *(uint64 *)($buf + 40)); }
        if ($buflen >= 56) { printf(" %016lx", *(uint64 *)($buf + 48)); }
        printf("\n");
    }
}
```

> **Gotcha**: bpftrace uses `uint16`, `uint64` etc. — NOT `u16`. Standard C types like `u16` from kernel headers are NOT available.

#### Running the Trace

```bash
# Start bpftrace BEFORE triggering the action you want to capture.
# -B line = line-buffered output (important for tee).
sudo bpftrace -B line /tmp/trace_ice_aq.bt 2>&1 | tee /tmp/kernel_aq_trace.log &
BPFTRACE_PID=$!

# Now trigger the TXTIME enable on the kernel interface:
sudo ip link set eth3 down
sudo ip link set eth3 up
# Wait for link to come up
sleep 5
sudo tc qdisc add dev eth3 root etf clockid CLOCK_TAI offload delta 500000

# Verify TXTIME was enabled:
sudo dmesg | tail -5
# Should show: "ice 0000:ca:00.0 eth3: enable TxTime on queue: 0"

# Stop tracing:
sudo kill -INT $BPFTRACE_PID
wait $BPFTRACE_PID
```

> **ETF qdisc syntax**: The keyword is `offload` (NOT `offload on`). The command `offload on` gives "unknown parameter".

#### Output Format

```
AQ op=0x2400 fl=0x0c30 dl=0 p=0000000000000001 0000000000000000
  B[56]: 0000000100000177 0000000000000000 0000000041778d80 0000000004088000 ...
```

- `op=0x2400`: Outer opcode (0x2400 = `send_to_pf` for VF→PF forwarding, common wrapper)
- `fl=0x0c30`: The **actual** AQ opcode lives in the flags/sub-opcode field
- `B[56]`: Buffer contents as 8-byte LE words (7 × 8 = 56 bytes)

### Extracting TXTIME-Specific Commands

```bash
# Find all TXTIME-related opcodes (in the fl= field)
grep -n "fl=0x0c3[0-9a-f]" /tmp/kernel_aq_trace.log

# Count by opcode
for op in 0x0c30 0x0c31 0x0c35 0x0c37 0x0404; do
    echo -n "fl=$op: "; grep -c "fl=$op" /tmp/kernel_aq_trace.log
done

# Find the boundary between link-up and ETF-triggered commands:
# 0x0c31 (dis_vsi_txq) = teardown during link-down
# 0x0c30 (add_lan_txq) = queue add during link-up
# 0x0c35 (set_txtimeq) = TXTIME context (only after ETF qdisc)
grep -n "fl=0x0c35" /tmp/kernel_aq_trace.log | head -3
# The first 0x0c35 marks the start of ETF-triggered commands.
```

## 3. Decoding AQ Buffers

### TLAN Context (in `add_lan_txq` / 0x0C30 buffer)

The 0x0C30 buffer is `struct ice_aqc_add_tx_qgrp` (56 bytes for 1 queue):

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0x00 | 4 | `parent_teid` | Scheduler parent node (LE u32) |
| 0x04 | 1 | `num_txqs` | Always 1 in these traces |
| 0x05 | 3 | reserved | |
| 0x08 | 2 | `txq_id` | Queue index (LE u16) |
| 0x0A | 2 | reserved | |
| 0x0C | 4 | `q_teid` | 0 on send, filled by FW in response |
| 0x10 | 22 | `txq_ctx[]` | Packed TLAN context (ice_tlan_ctx_info) |
| 0x26 | 2 | reserved | |

#### TLAN Context Bit Fields (ice_tlan_ctx_info)

| Field | Width | LSB | Description |
|-------|-------|-----|-------------|
| base | 57 | 0 | TX descriptor ring DMA base (128B units) |
| port_num | 3 | 57 | Port number |
| cgd_num | 4 | 60 | |
| pf_num | 3 | 64 | PF number |
| vmvf_num | 10 | 67 | VM/VF number |
| vmvf_type | 2 | 77 | 0=VF, 1=VMQ, 2=PF |
| src_vsi | 10 | 79 | Source VSI index |
| tsyn_ena | 1 | 89 | Timestamp enable |
| internal_usage_flag | 1 | 90 | |
| alt_vlan | 2 | 91 | |
| cpuid | 8 | 93 | |
| wb_mode | 1 | 101 | |
| tphrd_desc | 1 | 102 | |
| tphrd | 1 | 103 | |
| tphwr_desc | 1 | 104 | |
| cmpq_id | 9 | 105 | |
| qnum_in_func | 14 | 114 | |
| itr_notification_mode | 1 | 128 | |
| adjust_prof_id | 6 | 129 | |
| **qlen** | **13** | **135** | **TX descriptor ring length** |
| quanta_prof_idx | 4 | 148 | |
| tso_ena | 1 | 152 | TSO enable |
| tso_qnum | 11 | 153 | TSO queue number |
| legacy_int | 1 | 164 | Legacy interrupt mode |
| drop_ena | 1 | 165 | |
| cache_prof_idx | 2 | 166 | |
| pkt_shaper_prof_idx | 3 | 168 | |

### TXTIME Context (in `set_txtimeq` / 0x0C35 buffer)

The 0x0C35 buffer is `struct ice_aqc_set_txtime_qgrp` (40 bytes for 1 queue):

| Offset | Size | Field |
|--------|------|-------|
| 0x00 | 8 | reserved |
| 0x08 | 4 | reserved (per-queue) |
| 0x0C | 25 | `txtime_ctx[]` packed context |
| 0x25 | 3 | reserved |

The descriptor params contain `q_id` (LE u16) and `q_amount` (LE u16).

#### TXTIME Context Bit Fields (ice_txtime_ctx_info)

| Field | Width | LSB | Description |
|-------|-------|-----|-------------|
| base | 57 | 0 | TS descriptor ring DMA base (128B units) |
| pf_num | 3 | 57 | PF number |
| vmvf_num | 10 | 60 | |
| vmvf_type | 2 | 70 | 0=VF, 1=VMQ, 2=PF |
| src_vsi | 10 | 72 | Source VSI |
| cpuid | 8 | 82 | |
| tphrd_desc | 1 | 90 | |
| **qlen** | **13** | **91** | **TS ring length** |
| timer_num | 1 | 104 | PHC timer index |
| **txtime_ena_q** | **1** | **105** | **Must be 1 to enable TXTIME** |
| **drbell_mode_32** | **1** | **106** | **Must be 1 for 32-bit doorbell** |
| **ts_res** | **4** | **107** | **7 = 128ns resolution** |
| ts_round_type | 2 | 111 | |
| ts_pacing_slot | 3 | 113 | |
| merging_ena | 1 | 116 | |
| ts_fetch_prof_id | 4 | 117 | |
| ts_fetch_cache_line_aln_thld | 4 | 121 | |
| tx_pipe_delay_mode | 1 | 125 | |
| int_q_state | 70 | 126 | Internal — DO NOT WRITE |

### Python Decoder Script

```python
import struct

def decode_ctx(qwords, fields, ctx_offset=0):
    """Decode packed context from 64-bit words.
    qwords: list of uint64 values from trace
    fields: list of (name, width, lsb)
    ctx_offset: byte offset where context starts within the buffer
    """
    raw = b''.join(struct.pack('<Q', q) for q in qwords)
    ctx_bytes = raw[ctx_offset:]
    ctx_int = int.from_bytes(ctx_bytes[:32], 'little')
    
    for name, width, lsb in fields:
        val = (ctx_int >> lsb) & ((1 << width) - 1)
        print(f"  {name:35s} = {val}")

# Example: decode TXTIME context from 0x0C35 buffer
# Buffer qwords from trace (5 × 8 bytes):
txtime_qwords = [0x0000000000000000, 0x749fa97700000000,
                 0x0000088000000000, 0x0000000000003e11,
                 0x0000000000000000]

txtime_fields = [
    ("base", 57, 0), ("pf_num", 3, 57), ("vmvf_num", 10, 60),
    ("vmvf_type", 2, 70), ("src_vsi", 10, 72), ("cpuid", 8, 82),
    ("tphrd_desc", 1, 90), ("qlen", 13, 91), ("timer_num", 1, 104),
    ("txtime_ena_q", 1, 105), ("drbell_mode_32", 1, 106),
    ("ts_res", 4, 107), ("ts_round_type", 2, 111),
    ("ts_pacing_slot", 3, 113), ("merging_ena", 1, 116),
    ("ts_fetch_prof_id", 4, 117),
    ("ts_fetch_cache_line_aln_thld", 4, 121),
    ("tx_pipe_delay_mode", 1, 125), ("int_q_state", 70, 126),
]

# Context starts at byte 12 in the 0x0C35 buffer
decode_ctx(txtime_qwords, txtime_fields, ctx_offset=12)
```

## 4. Comparing Kernel vs DPDK Traces

### Step-by-step Diff Procedure

```bash
# 1. Extract TXTIME opcodes from both traces

# DPDK (opcodes are in "AQ Command: opcode 0xNNNN" format):
grep "AQ Command: opcode 0x0C3" /tmp/dpdk_aq_trace.log

# Kernel (opcodes are in the fl= field):
grep "fl=0x0c3" /tmp/kernel_aq_trace.log

# 2. Find ETF-triggered commands in kernel trace
# The first 0x0c35 marks the start of TXTIME-related setup
FIRST_TXTIME=$(grep -n "fl=0x0c35" /tmp/kernel_aq_trace.log | head -1 | cut -d: -f1)
echo "ETF commands start at line $FIRST_TXTIME"

# 3. Extract just the ETF-triggered sequence
sed -n "${FIRST_TXTIME},\$p" /tmp/kernel_aq_trace.log > /tmp/kernel_etf_only.log

# 4. Count commands in each
for op in 0x0c30 0x0c31 0x0c35 0x0c37 0x0404; do
    K=$(grep -c "fl=$op" /tmp/kernel_etf_only.log 2>/dev/null || echo 0)
    D=$(grep -c "opcode $(echo $op | tr a-f A-F)" /tmp/dpdk_aq_trace.log 2>/dev/null || echo 0)
    echo "$op: kernel=$K dpdk=$D"
done
```

### Key Findings from AQ Comparison (Session 4)

#### Command Ordering — Both Follow the Same Pattern

For each TX queue:
1. `0x0C30` (add_lan_txq) — add the queue with TLAN context
2. `0x0404` (move_sched_elems) — attach to scheduler tree
3. `0x0C35` (set_txtimeq) — set TXTIME context

#### TLAN Context (0x0C30) — Functionally Identical

| Field | Kernel | DPDK | Match? |
|-------|--------|------|--------|
| src_vsi | 17 | 17 | ✅ |
| vmvf_type | 0 (PF) | 0 (PF) | ✅ |
| tso_ena | 1 | 1 | ✅ |
| legacy_int | 1 | 1 | ✅ |
| qlen | 256 | 512 | ✅ (different ring sizes) |
| tso_qnum | 0 | 1 | ✅ (different queue indices) |
| base | (differs) | (differs) | ✅ (different DMA addrs) |

#### TXTIME Context (0x0C35) — Functionally Identical

| Field | Kernel | DPDK | Match? |
|-------|--------|------|--------|
| txtime_ena_q | 1 | 1 | ✅ |
| drbell_mode_32 | 1 | 1 | ✅ |
| ts_res | 7 (128ns) | 7 (128ns) | ✅ |
| vmvf_type | 2 (PF) | 2 (PF) | ✅ |
| src_vsi | 8 | 8 | ✅ |
| qlen | 288 | 544 | ✅ (256+32 vs 512+32) |
| base | (differs) | (differs) | ✅ (different DMA addrs) |

#### Differences Found

| Difference | Kernel | DPDK | Impact |
|-----------|--------|------|--------|
| `0x0C37` (ena_dis_txtimeq) | **NOT sent** | 1 call (before stop/start) | Already tested — no effect |
| Number of queues | 55 queues rebuilt | 1 TXTIME queue + 1 FDIR | Expected |
| VSI rebuild | Full `ice_vsi_rebuild()` | Incremental queue add | **Possible root cause** |

#### Conclusion

**At the AQ command level, the contexts are identical.** The root cause is NOT in the AQ command payloads. The difference is in the **lifecycle**: the kernel does a full VSI teardown + rebuild (`ice_vsi_decfg` + `ice_vsi_cfg_tc_lan`) which reorganizes the entire scheduler tree, while DPDK does incremental queue adds.

## 5. MMIO Tracing

### 5.1 Kernel: mmiotrace

For capturing ALL register writes (not just AQ commands), use mmiotrace.

**Critical gotcha**: mmiotrace must be enabled BEFORE the driver maps the BAR. If the BAR is already mapped, mmiotrace captures nothing. You must unbind → enable mmiotrace → rebind.

#### Procedure (tested, works)

```bash
# 1. Unbind the NIC first
sudo bash -c 'echo 0000:ca:00.0 > /sys/bus/pci/devices/0000:ca:00.0/driver/unbind'

# 2. Enable mmiotrace
sudo bash -c 'echo mmiotrace > /sys/kernel/debug/tracing/current_tracer'
sudo bash -c 'echo 1 > /sys/kernel/debug/tracing/tracing_on'

# 3. Rebind to ice driver (this makes mmiotrace see the BAR mapping)
sudo bash -c 'echo 0000:ca:00.0 > /sys/bus/pci/drivers/ice/bind'

# 4. Wait for link, then do your ETF setup
sleep 10
sudo tc qdisc add dev eth3 root etf clockid CLOCK_TAI offload delta 500000

# 5. Stop tracing
sudo bash -c 'echo 0 > /sys/kernel/debug/tracing/tracing_on'
sudo cat /sys/kernel/debug/tracing/trace > /tmp/kernel_mmio_raw.log
sudo bash -c 'echo nop > /sys/kernel/debug/tracing/current_tracer'
```

**Warning**: mmiotrace is system-wide and captures ALL MMIO for ALL devices. Filter by BAR address.

#### mmiotrace Line Format

```
W <size> <timestamp> <pid> <phys_addr> <value> <pc> <rest>
R <size> <timestamp> <pid> <phys_addr> <value> <pc> <rest>
```

Fields: `$1`=R/W, `$2`=size, `$3`=timestamp, `$4`=pid, `$5`=addr, `$6`=value.

#### Filter to NIC BAR Address

```bash
# Find BAR0 from lspci
lspci -v -s ca:00.0 | grep "Memory at"
# Our NIC: BAR0 = 0x28ffe8000000 (128M)

# Extract NIC-only writes, compute register offset
grep "^W " /tmp/kernel_mmio_raw.log | awk 'index($5,"28ffe8") > 0'

# Get register offset from full physical address:
# addr=$5; sub(".*28ffe8","0x",addr)  → gives e.g. 0x080400
```

### 5.2 DPDK: Instrumented wr32

DPDK's `wr32()` macro in `ice_osdep.h` can be instrumented to log all register writes:

```c
// In ice_osdep.h, replace wr32 macro with:
#define wr32(a, reg, value) do { \
    writel((value), (a)->hw_addr + (reg)); \
    { static FILE *_f = NULL; \
      if (!_f) _f = fopen("/tmp/dpdk_wr32.log", "a"); \
      if (_f) { fprintf(_f, "W 0x%06x 0x%08x\n", (unsigned)(reg), (unsigned)(value)); fflush(_f); } \
    } \
} while(0)
```

**Important limitation**: `wr32()` only captures writes via `wr32(hw, offset, value)`. Doorbell writes via `ICE_PCI_REG_WRITE(reg, value)` (which calls `writel(value, absolute_addr)`) are **NOT captured**. This includes:
- `QTX_COMM_DBELL` doorbell (data queue tail)
- `E830_GLQTX_TXTIME_DBELL_LSB` (TXTIME doorbell)

To capture doorbells, you'd also need to instrument `writel()` or `ICE_PCI_REG_WRITE`.

### 5.3 MMIO vs AQ

- **AQ commands** configure queue contexts, scheduler, and TXTIME — firmware-mediated
- **MMIO writes** directly poke hardware registers — doorbells, interrupt config
- Key MMIO-only registers: `QINT_TQCTL` (interrupt binding), `QTX_COMM_DBELL` (data doorbell), `E830_GLQTX_TXTIME_DBELL_LSB` (TS doorbell)

## 6. Reading TXTIME Context Back from Hardware

DPDK can read the E830 TXTIME context registers directly:

```bash
# In DPDK code, read E830_GLQTX_TXTIME(q, word) for word=0..6
# Each is a 32-bit register giving the packed TXTIME context for queue q
```

The `TXTIME SCAN` output in DPDK traces shows this readback:
```
TXTIME SCAN: q=1 ENABLED data={0x749fa977 0x0 0x880 0x3e11 0x0 0x3800 0x0}
```

These 7 × 32-bit values are the raw TXTIME context as stored in hardware. Decode with the TXTIME context bit fields table above.

## 7. Useful Grep Patterns

```bash
# All AQ errors (non-zero retval) in DPDK trace
grep "retval 0x" /tmp/dpdk_aq_trace.log | grep -v "retval 0x0000"

# DPDK TXTIME-specific log lines (custom printf added during investigation)
grep "TXTIME\|TLAN\[" /tmp/dpdk_aq_trace.log

# Kernel dmesg for TXTIME events
sudo dmesg | grep -i "txtime\|etf\|launch"

# Check if ETF qdisc is active
tc qdisc show dev eth3
```

## 8. Prerequisites

| Tool | Check | Install |
|------|-------|---------|
| bpftrace | `which bpftrace` | `apt install bpftrace` |
| mmiotrace | `grep MMIOTRACE /boot/config-$(uname -r)` | Needs `CONFIG_MMIOTRACE=y` |
| tc (iproute2) | `which tc` | `apt install iproute2` |
| DPDK | Built with ice PMD | See DPDK build docs |
| Kernel ice | ice-2.5.4 loaded | `modprobe ice` |

## 9. NIC Binding Cheat Sheet

```bash
# Bind to kernel ice driver
sudo python3 /home/gta/mkasiew/repos/dpdk-25.11/usertools/dpdk-devbind.py -b ice 0000:ca:00.0

# Bind to DPDK (vfio-pci)
sudo python3 /home/gta/mkasiew/repos/dpdk-25.11/usertools/dpdk-devbind.py -b vfio-pci 0000:ca:00.0

# Check current binding
sudo python3 /home/gta/mkasiew/repos/dpdk-25.11/usertools/dpdk-devbind.py -s | grep ca:00
```

---

## 10. Trace Findings & Conclusions

All findings below were captured on:
- **NIC**: Intel E830 at PCI `ca:00.0`, BAR0 = `0x28ffe8000000` (128M)
- **Kernel driver**: ice-2.5.4 (out-of-tree)
- **DPDK**: 25.11 with custom TXTIME patches
- **Test**: `txtime_test` tool with TS-only doorbell (no QTX_COMM_DBELL during TX)

### 10.1 Kernel mmiotrace Capture

**Trace file**: `/tmp/kernel_mmio_raw.log` — 34,496 lines total

**Procedure**: unbind ice → enable mmiotrace → rebind ice → wait for link → apply ETF qdisc → capture

**Time phases**:
- **Probe phase** (ts < 757759.658): Driver initialization after rebind
  - 3,761 NIC BAR0 writes
  - Only 1 unique register: `0x080400` (AQ tail pointer) — all probe-phase config is AQ-mediated
- **ETF phase** (ts >= 757759.658): VSI teardown + rebuild triggered by ETF qdisc
  - 1,310 NIC BAR0 writes
  - 141 unique register offsets

**Kernel ETF-phase register breakdown (141 unique registers)**:

| Register Block | Count | Description |
|----------------|-------|-------------|
| `QRX_CTX` (0x280000-0x28e01c) | 64 | RX queue context (8 queues × 8 dwords) |
| `GLINT_ITR` (0x15a004-0x15a020) | 8 | Interrupt throttle rate |
| `GLINT_RATE` (0x156004-0x156020) | 8 | Interrupt rate config |
| `GLINT_DYN_CTL` (0x160000-0x160020) | 9 | Dynamic interrupt control |
| `QINT_TQCTL` (0x140000-0x14001c) | 8 | TX queue interrupt control |
| `QINT_RQCTL` (0x150000-0x15001c + 0x154004-0x154020) | 15 | RX queue interrupt control |
| `QRX_CTRL` (0x120000-0x12001c) | 8 | RX queue control enable |
| `QRX_TAIL` (0x290000-0x29001c) | 8 | RX queue tail pointer |
| `QRXFLXP_CNTXT` (0x480000-0x48001c) | 8 | RX flex parser context |
| `QTX_COMM_DBELL` (0x2c0000) | 1 | TX queue 0 data doorbell |
| `GL_RDPU_CNTRL` (0x052054) | 1 | Checksum feature bit |
| AQ regs (0x080400-0x080480) | 2 | Admin queue pointers |

#### Critical Finding: ZERO TXTIME MMIO During ETF Setup

| Register Range | Name | Writes | Reads | Significance |
|----------------|------|--------|-------|-------------|
| `0x2d3xxx` | TXTIME global (WRR, FETCH_PROFILE, CNTX_CTL) | **0** | 128 | Kernel only READS fetch profile, never writes |
| `0x2e0xxx` | TXTIME doorbell (E830_GLQTX_TXTIME_DBELL_LSB) | **0** | 0 | No doorbell during setup (expected) |
| `0x2c0000` | QTX_COMM_DBELL(0) | **2** | 0 | Values 0x2, 0x3 — regular TX after ETF (control traffic) |

**Conclusion**: The kernel's TXTIME setup is **entirely AQ-mediated**. No TXTIME-specific MMIO register writes occur during ETF qdisc application. The kernel:
1. Reads `E830_GLTXTIME_FETCH_PROFILE` 128 times (16 entries × 8 reads) to compute TS ring size
2. Sends AQ commands (`add_lan_txq`, `move_sched_elems`, `set_txtimeq`) to FW
3. Maps `tstamp_ring->tail` to the TXTIME doorbell address for later TX-time writes
4. Never writes WRR, fetch profile, or context control registers via MMIO

### 10.2 DPDK wr32 Capture

**Trace file**: `/tmp/dpdk_wr32.log` — 7,988 entries (init + queue start, NOT including doorbell writes)

**DPDK register breakdown (101 unique registers)**:

| Register Block | Count | Description |
|----------------|-------|-------------|
| AQ regs (0x080000-0x080480) | 10 | Admin queue (PF + MBX) |
| PTP regs (0x088808-0x088920) | 8 | PTP/PHC time registers |
| PF_MBX (0x22e100-0x22e580) | 10 | Mailbox queue init |
| GLQF_HMASK (0x40fc00-0x40fc3c) | 16 | Flow director hash masks |
| GLQF_FDMASK (0x410800-0x41083c) | 16 | Flow director field masks |
| QRX_CTX (0x280000-0x28e004) | 16 | RX queue context (2 queues × 8 dwords) |
| TXTIME regs (0x2d3204-0x2d3210) | 3 | TXTIME WRR + context control |
| MDET regs (0x2d2c80-0x2d2e00 + 0x0fc000-0x0fc068) | 5 | MDD event clear registers |
| GL_PREEXT (0x20f0fc-0x20f108) | 2 | Parser extension |
| PFINT_OICR (0x16c900) | 1 | Interrupt cause register |
| PFGEN_CTRL (0x091000) | 1 | PF general control |
| PFQF_FD_ENA (0x43a000) | 1 | Flow director enable |
| Interrupt regs | 5 | QINT_TQCTL, QINT_RQCTL, GLINT_DYN_CTL |
| QRX_CTRL (0x120000-0x120004) | 2 | RX queue control |
| QRXFLXP (0x480000-0x480004) | 2 | RX flex parser |
| QRX_ITR (0x292000) | 1 | RX interrupt throttle |
| PF_MDET (0x040800) | 1 | MDD detection for PF |

**Limitation**: `wr32()` instrumentation does NOT capture doorbell writes (`ICE_PCI_REG_WRITE` → `writel()` with absolute address). QTX_COMM_DBELL and TXTIME doorbell writes are invisible in this trace.

### 10.3 Register Comparison: Kernel ETF vs DPDK

**Method**: `comm -23 /tmp/kernel_etf_regs.txt /tmp/dpdk_regs.txt`

| Set | Count | Description |
|-----|-------|-------------|
| Kernel ETF only | 116 | Registers kernel writes but DPDK doesn't |
| DPDK only | 76 | Registers DPDK writes but kernel doesn't |
| Both | 25 | Common registers |

#### Kernel-Only Registers (116) — ALL are interrupt/RX config for extra queues

| Block | Count | Why kernel has them but DPDK doesn't |
|-------|-------|--------------------------------------|
| QRX_CTX (queues 2-7) | 48 | Kernel has 8 RX queues; DPDK has 2 |
| QINT_TQCTL (queues 1-7) | 7 | Interrupt binding for extra TX queues |
| QINT_RQCTL (queues 1-7) | 15 | Interrupt binding for extra RX queues |
| GLINT_ITR (vectors 1-8) | 8 | Interrupt throttle for extra vectors |
| GLINT_RATE (vectors 1-8) | 8 | Interrupt rate for extra vectors |
| GLINT_DYN_CTL (vectors 1-8) | 8 | Dynamic interrupt for extra vectors |
| QRX_CTRL (queues 2-7) | 6 | RX enable for extra queues |
| QRX_TAIL (queues 0-7) | 8 | RX tail for all queues |
| QRXFLXP_CNTXT (queues 2-7) | 6 | Flex parser for extra queues |
| GL_RDPU_CNTRL | 1 | Checksum feature bit (GCS) — not TXTIME related |
| QTX_COMM_DBELL(0) | 1 | Regular TX after ETF (control traffic) |

**Conclusion**: The 116 kernel-only registers are ALL scaling/interrupt config for queues 2-7. **ZERO are TXTIME-specific.** The kernel does not write any TXTIME MMIO registers that DPDK misses.

#### DPDK-Only Registers (76) — DPDK writes MORE TXTIME registers than kernel

| Block | Count | Why DPDK has them but kernel doesn't |
|-------|-------|--------------------------------------|
| PTP regs | 8 | DPDK explicitly enables PTP/PHC |
| PF_MBX | 10 | DPDK inits mailbox queues |
| GLQF_HMASK + GLQF_FDMASK | 32 | DPDK inits flow director |
| **TXTIME regs** | **3** | **DPDK writes WRR + CNTX_CTL — kernel doesn't!** |
| MDET regs | 5 | DPDK clears stale MDD events |
| GL_PREEXT | 2 | Parser extension config |
| Other (PFINT_OICR, PFGEN_CTRL, etc.) | 16 | Various DPDK-specific init |

### 10.4 Key DPDK TXTIME Register Writes (from wr32 log)

```
W 0x2d320c 0x00004040    # E830_GLTXTIME_DBL_COMP_WRR_MAX_CREDITS = {doorbell=64, completion=64}
W 0x2d3210 0x00000820    # E830_GLTXTIME_DBL_COMP_WRR_WEIGHTS = {doorbell=32, completion=32}
W 0x2d3204 0x00080000    # E830_GLTXTIME_QTX_CNTX_CTL — context read for q=0
W 0x2d3204 0x00080001    #   ... q=1
... (continues for q=0 through q=1023 for TXTIME SCAN readback)
```

**WRR Issue**: DPDK writes `0x4040` to WRR_MAX_CREDITS and `0x0820` to WRR_WEIGHTS, but **readback returns 0x0 for both**. These registers may be:
- Read-only (FW-managed)
- Write-once (require specific enable sequence)
- Protected by a lock bit
- Irrelevant (kernel never writes them and TXTIME works)

**The kernel never writes WRR registers and TXTIME works fine.** This strongly suggests WRR is FW-managed and our writes are ignored/unnecessary.

### 10.5 MDD Event 29 — Persistent Problem

Every DPDK test run triggers:
```
OICR: MDD event
Malicious Driver Detection event 29 by PQM on TX queue 1 PF# 0
GL_PQM=0xf4001000 GL_TCLAN=0x0 PF_PQM=0x1 PF_TCLAN=0x0 PF_TDPU=0x0
```

Decoded `GL_PQM=0xf4001000`:
- VALID=1, MAL_TYPE=29, QNUM=1, VF_NUM=0, PF_NUM=0

**MAL_TYPE 29** = PQM (Packet Queue Manager) malicious event on TX queue 1. This fires during queue start — BEFORE any TX attempts. It happens consistently:
- On fresh DPDK start
- After stop→start cycle
- Even after kernel-first test (kernel ETF → unbind → vfio-pci → DPDK)
- Clearing MDD registers doesn't prevent re-occurrence

**Queue 1 is the DPDK TX queue** (`reg_idx=1`, because `base_queue=1` for the DPDK VSI).

**Hypothesis**: The MDD event 29 may indicate PQM detects a problem with the queue configuration or TXTIME context BEFORE any packets are sent. This could be blocking TXTIME.

### 10.6 Kernel-First Test with TS-Only Doorbell

**Procedure**: kernel ice bind → ETF qdisc enable → unbind → vfio-pci bind → run txtime_test with TS-only doorbell

**Result**: `opackets=0` across ALL 12 tests.

**What residual kernel state is visible to DPDK**:
- TXTIME contexts from queues 2-9 still active (kernel's other queues)
- `E830_GLTXTIME_TS_CFG=0x00000001` (TXTIME globally enabled by kernel)
- TLAN contexts show `tsyn_ena=1` and various kernel queues

**Conclusion**: Residual kernel FW state does NOT help when using TS-only doorbell. The problem is in the DPDK TX path, not in the FW initialization.

### 10.7 Kernel TX Path — How TXTIME Doorbell is Written

From `ice_txrx.c` in kernel ice-2.5.4:

```c
// Kernel writes EITHER the TXTIME doorbell OR the regular doorbell, never both:
if (tx_ring->flags & ICE_TX_FLAGS_TXTIME) {
    // Build TS descriptor: bits[12:0]=tx_desc_idx, bits[31:13]=timestamp_128ns
    ts_desc->tx_desc_idx_tstamp = ice_build_tstamp_desc(i, tstamp);
    j++;
    // Handle TS ring wrap with extra fetch descriptors to prevent MDD
    if (j == tstamp_ring->count) {
        int fetch = tstamp_ring->count - tx_ring->count;
        j = 0;
        for (; j < fetch; j++)
            ts_desc->tx_desc_idx_tstamp = ice_build_tstamp_desc(i, tstamp);
    }
    tstamp_ring->next_to_use = j;
    writel_relaxed(tstamp_ring->next_to_use, tstamp_ring->tail);  // TXTIME doorbell
} else {
    writel_relaxed(i, tx_ring->tail);  // Regular QTX_COMM_DBELL
}
```

Key observations:
- Kernel uses `writel_relaxed` for doorbell (no memory barrier after)
- TS descriptor format: `FIELD_PREP(GENMASK(12,0), tx_desc_idx) | FIELD_PREP(GENMASK(31,13), tstamp_128ns)`
- On TS ring wrap, kernel fills extra `fetch` descriptors to prevent MDD
- `kick` flag from `__netdev_tx_sent_queue` gates the doorbell — may be batched

DPDK does the same but:
- Uses `ICE_PCI_REG_WRITE` (= `writel` with full barrier) instead of `writel_relaxed`
- Same TS descriptor format, same wrap handling
- `desc_tx_id` quirk: `(tx_id == 0) ? txq->nb_tx_desc : tx_id` — matches upstream DPDK 26.03

### 10.8 Summary of What Traces Tell Us

| Aspect | Kernel mmiotrace | DPDK wr32 | Conclusion |
|--------|-----------------|-----------|------------|
| TXTIME MMIO writes | 0 | 3 (WRR + CNTX_CTL) | DPDK does MORE, kernel does none |
| AQ commands | Identical contexts | Identical contexts | Not the root cause |
| QTX_COMM_DBELL | 2 (regular TX) | N/A (not captured) | — |
| TXTIME doorbell | 0 (setup only) | N/A (not captured) | Expected: doorbell only at TX time |
| Interrupt config | 8 queues | 2 queues | Just scaling difference |
| MDD event 29 | Not observed | Every run | **Unique DPDK problem** |
| WRR readback | N/A | Write 0x4040/0x820 → reads back 0x0 | FW-managed registers |

**Bottom line from PCIe tracing**:
1. The register write gap is NOT the root cause — DPDK writes a superset of what kernel writes
2. All TXTIME configuration is AQ-mediated (confirmed by kernel mmiotrace: zero TXTIME MMIO writes)
3. MDD event 29 on queue 1 at startup is a DPDK-unique problem that doesn't occur in kernel
4. WRR registers are likely FW-managed — kernel never writes them and TXTIME works

### 10.9 ROOT CAUSE FOUND: TXTIME Doorbell Init Write

**The fix**: Remove `ICE_PCI_REG_WRITE(txq->qtx_tail, 0)` for the TXTIME case in `ice_tx_queue_start()`.

**Root cause**: During `ice_tx_queue_start()`, DPDK wrote 0 to the TXTIME doorbell (`E830_GLQTX_TXTIME_DBELL_LSB`) immediately after calling `ice_aq_set_txtimeq()`. This "rings" the TXTIME doorbell with ts_id=0, which the PQM interprets as a request to process TS descriptor 0 — but the TS ring is empty/uninitialized at that point. The hardware raises MDD event 29 (PQM malicious driver detection) because it detects an invalid TS descriptor access.

**Why kernel works**: The kernel ice driver (`ice_base.c:ice_vsi_cfg_txq`) assigns `tstamp_ring->tail` pointer but **never writes to it** during queue start. The TXTIME doorbell is only written during actual packet TX in `ice_txrx.c:ice_tstamp_tx_hwtstamp()`. This was confirmed by mmiotrace: zero TXTIME MMIO writes during ETF setup.

**Evidence chain**:
1. PCIe mmiotrace showed kernel never writes TXTIME doorbell during setup (§10.3)
2. DPDK wr32 trace showed DPDK writes MORE registers than kernel (§10.4)
3. The key difference: DPDK called `ICE_PCI_REG_WRITE(txq->qtx_tail, 0)` after `aq_set_txtimeq`
4. Removing this single write eliminated all MDD events and enabled full TXTIME operation

**Fix location**: `dpdk-25.11/drivers/net/intel/ice/ice_rxtx.c`, `ice_tx_queue_start()` — TXTIME branch now assigns `qtx_tail` pointer but does NOT write to it:
```c
txq->qtx_tail = hw->hw_addr + E830_GLQTX_TXTIME_DBELL_LSB(txq->reg_idx);
/* Do NOT write 0 to the TXTIME doorbell during init.
 * The kernel ice driver never does this — it only assigns
 * the pointer. Writing the doorbell here triggers PQM MDD
 * event 29 on queue start.
 */
```

**Test results** (two independent runs, identical results):

| Test | Description | Result |
|------|-------------|--------|
| Test 0 | Sanity (no TXTIME) | opackets=3 ✅ |
| Tests 1-10 | Various TXTIME offsets | opackets=10 each ✅ |
| Test 11 | MTL-style stream (100 pkts, TS-only doorbell) | opackets=100, 100/100 sent, 0 stalls ✅ |
| MDD events | — | **ZERO** ✅ |

### 10.10 Resolved Unknowns

The following items from the original investigation are now resolved:

1. **MDD event 29 root cause** — RESOLVED: Writing 0 to the TXTIME doorbell during queue init triggers PQM MAL_TYPE=29. The TS ring is empty at that point, so the PQM detects an invalid descriptor access.

2. **Doorbell capture gap** — RESOLVED: The gap didn't matter — the fix was in the init path, not the TX path. The TX-path doorbell writes (ts_id values) work correctly once the init write is removed.

3. **`ice_vsi_rebuild` vs incremental** — NOT the root cause. The incremental approach works fine once the spurious doorbell write is removed.

4. **Timer association** — NOT the root cause. The timer_num is correct.

### 10.11 Remaining Work

1. **Upstream patch**: The fix should be submitted as a DPDK patch for E830 TXTIME support
2. **Additional TXTIME init code**: The current DPDK code includes several workarounds added during investigation (pre-disable stale context, WRR writes, fetch profile programming, all-queue TXTIME context, TXTIME scan, TLAN readback). Some may be unnecessary now that the real fix is known — these should be evaluated for removal
3. **Clean up debug instrumentation**: wr32 logging macro and TX-path printf should be reverted before final patch
