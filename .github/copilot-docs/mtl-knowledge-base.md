# MTL Knowledge Base — AI Agent Context

> **Purpose**: Single consolidated reference for AI agents working on MTL.
> This file replaces all prior per-topic copilot-docs files.
> Last verified against codebase: 2026-03.

---

## §1 Architecture & Design Philosophy

### What MTL Is
SMPTE ST 2110 media transport over IP. DPDK-based with HW pacing (Intel E810). Supports:
- ST2110-20 (uncompressed video)
- ST2110-22 (compressed video / JPEG-XS)
- ST2110-30 (audio)
- ST2110-40 (ancillary data)
- ST2110-41 (fast metadata)

### Two-World Pattern (Data Plane / Control Plane Split)
- **Data plane**: DPDK hugepage memory, spinlocks, lock-free rings, zero-copy, polling tasklets. Hot path — never blocks.
- **Control plane**: Heap memory, mutexes, POSIX threads. Session create/destroy, config, stats — allowed to block.

Rule: A data-plane code path must NEVER call a control-plane function (no malloc, no mutex, no log at INFO level).

### Three API Layers

| Layer | API Pattern | Who Uses It | What It Handles |
|-------|-------------|-------------|-----------------|
| **Pipeline** | `st20p_tx_get_frame()` / `put_frame()` | Most apps | Format conversion, frame lifecycle, blocking/polling, codec |
| **Session** | `st20_tx_create()` + callbacks | Advanced users | Direct control of frames/slices/RTP |
| **Transport** | Internal only | MTL core | Packet construction, pacing, NIC interaction |

Pipeline wraps session: `st20p_tx_create()` calls `st20_tx_create()` internally.

### Naming Conventions

| Prefix | Component |
|--------|-----------|
| `mt_` | Core library (non-media) |
| `st_` / `st20_` / `st22_` / `st30_` / `st40_` / `st41_` | Media session APIs |
| `st20p_` / `st22p_` / `st30p_` | Pipeline APIs |
| `tv_` | TX video session internals |
| `rv_` | RX video session internals |
| `tx_audio_session_` | TX audio internals |
| `rx_audio_session_` | RX audio internals |
| `tx_ancillary_session_` | TX ancillary internals |
| `rx_ancillary_session_` | RX ancillary internals |
| `tx_fastmetadata_` / `rx_fastmetadata_` | ST2110-41 internals |

### Video Complexity vs Audio/Ancillary/Fast-Metadata

| Aspect | Video (ST2110-20/22) | Audio/Ancillary/FMD |
|--------|---------------------|---------------------|
| Packets/frame | ~4,500 (1080p60) | 1–8 |
| Pacing | Per-packet (µs precision) | Per-frame (ms) |
| Assembly | Slot-based, bitmap, DMA | Sequential, trivial |
| Separate builder+transmitter? | Yes (ring-coupled) | No (single tasklet) |
| Shared queue friendly? | Only with flow director | Yes (low bandwidth) |

**Uniform design**: Adding a new ST2110-xx type = copy the video session pattern and simplify. Don't invent new architecture.

### End-to-End TX Data Journey
```text
App fills frame buffer (hugepage)
→ Pipeline: format convert (optional, via plugin)
→ Session: builder tasklet constructs RTP packets (header mbuf + extbuf chain to frame)
→ Builder enqueues to rte_ring
→ Transmitter tasklet dequeues, applies pacing (RL or TSC)
→ mt_txq_burst() → NIC DMA reads payload from frame buffer
→ NIC transmits on wire
→ extbuf refcnt hits 0 → frame buffer freed for reuse
```

### End-to-End RX Data Journey
```text
NIC receives multicast packet (flow rule steers to session's RX queue)
→ rte_eth_rx_burst() returns mbufs
→ RX tasklet: parse RTP header → extract tmstamp, seq_id, SRD fields
→ Find/create slot by tmstamp (rv_slot_by_tmstamp)
→ Calculate frame offset from SRD row/offset
→ Bitmap test-and-set (mt_bitmap_test_and_set) → skip if duplicate
→ memcpy (or DMA) payload into pre-allocated frame buffer
→ When recv_size >= frame_size AND dma_nb == 0 → frame complete
→ Pipeline: optional format convert → deliver to app
→ App returns frame → buffer freed for reuse
```

### Resilience Philosophy
- **Redundancy, not recovery**: ST2110-7 sends same stream on two paths; first-arriving packet wins
- **No retransmission by default**: RTCP-based retransmit is opt-in, custom format
- **Cooperative yield**: Never block, never spin-wait on NIC — save state, return, retry next iteration

### Design Decisions — Alternatives Considered

| Decision | Why This Way | Alternative Rejected |
|----------|-------------|---------------------|
| DPDK not kernel stack | Need µs-level pacing at 100Gbps | Kernel can't poll fast enough |
| Tasklets not threads | Cache locality, cooperative scheduling | Thread-per-session wastes cores |
| extbuf not memcpy TX | Zero-copy: NIC DMAs from app buffer | memcpy at 12Gbps burns CPU |
| rte_ring between builder/transmitter | Decouples timing from packet construction | Single tasklet can't meet both |
| Per-session mempool (TX) | Isolation: one session's leak can't starve others | Shared pool = cascading failures |
| Per-queue mempool (RX) | Multiple sessions share queue; pool sized to queue | Per-session = waste |
| Bitmap not seq tracking (RX) | O(1) dedup with out-of-order delivery | Last-seq useless with reordering |
| Slots not single buffer (RX) | Concurrent assembly of interleaved frames | Single buffer drops early arrivals |
| Round-robin slot eviction | Fair eviction, simple | LRU penalizes longest-waiting slot |
| Hugepages not regular pages | Fewer TLB misses at 5Gbps+ data rates | Regular pages = TLB thrashing |
| DMA threshold 1024 bytes | CPU memcpy faster below threshold | DMA setup cost dominates small copies |
| Stack mempool ops (LIFO) | Better cache reuse than FIFO ring ops | FIFO = cold cache lines |
| Spin polling not epoll/sleep | Only way to achieve <1µs pacing jitter | Any sleep mechanism adds 5-100µs |
| Hardware RL not software pacing | Sub-µs accuracy, zero CPU overhead | TSC pacing: 10-100µs jitter |

### Coding Rules
- Error paths must free in reverse allocation order
- Every `rte_malloc` must have a matching `rte_free` on the same NUMA socket
- `dbg()` / `info()` / `warn()` / `err()` for logging — never `printf`
- No C++ in library core (C99 only); tests use C++ for gtest
- All public API in `include/` — internal headers stay in `lib/src/`

---

## §2 Threading & Scheduler

### Cooperative Tasklet Model
- Tasklets are function pointers called repeatedly by a scheduler thread in a tight loop
- Tasklets **must never block** — one blocked tasklet starves all others
- Signal "I have work" (positive return) or "I'm idle" (return 0)
- Scheduler-to-tasklet: 1:many

### Two Scheduler Modes

| Mode | How | When |
|------|-----|------|
| **Lcore** (default) | DPDK lcores, CPU-pinned via `rte_eal_remote_launch()` | Deterministic latency |
| **Pthread** (`MTL_FLAG_TASKLET_THREAD`) | Regular pthreads, not pinned | Containers, non-dedicated cores |

Max schedulers: `MT_MAX_SCH_NUM = 18` (in `mt_main.h`)

### Quota System
Sessions assigned to schedulers by weight in "1080p-equivalents":
1. Search existing schedulers for capacity on right NUMA node
2. If none found, allocate new scheduler (up to 18)
3. As sessions freed, quota returned

**If adding a new session type**: define its quota correctly or schedulers get overloaded.

### Sleep/Wake Design
- Each tasklet declares `advice_sleep_us`
- Scheduler takes minimum across all tasklets
- Below 200µs (`sch_zero_sleep_threshold_us`): just yields
- Above 200µs: `pthread_cond_timedwait()` (works in both lcore and pthread modes)
- `MTL_FLAG_TASKLET_SLEEP` works in both modes

**Gotcha**: `advice_sleep_us = 0` → scheduler never sleeps → 100% CPU.

### Why Spin Polling Is Necessary

1080p60 `trs` ≈ 3.7µs between packets. Any sleep mechanism adds too much jitter:

| Mechanism | Latency | Viable? |
|-----------|---------|---------|
| `usleep()` | 50–100µs | No |
| `nanosleep()` | 50+µs | No |
| `epoll_wait()` | 5–15µs | No |
| **Spin on TSC** | **<0.1µs** | **Yes** |

Cost: 100% CPU per scheduler core. Acceptable because media machines dedicate cores.

### Builder vs Transmitter Split (TX Video Only)
Two tasklets per video session connected by `rte_ring` (`packet_ring`):
- **Builder** (`tvs_tasklet_handler`): reads frames, constructs RTP packets, enqueues to ring
- **Transmitter** (`video_trs_tasklet_handler`): dequeues, applies pacing, bursts to NIC

Ring = shock absorber. Builder can be slow; transmitter still has queued packets.

**Ring sizing**: Default 512 entries (`ST_TX_VIDEO_SESSIONS_RING_SIZE`, ≈ 1/9 of a 1080p frame's ~4500 packets). Enlarged to 2048 for TSN pacing (NIC holds packets → backpressure fills ring). Halved if exceeds `st20_total_pkts`. Uses SP/SC (single-producer/single-consumer) mode — both tasklets run on the same lcore, so no atomics needed (~2-3ns vs ~20+ns for multi-producer).

**Bulk enqueue semantics**: `rte_ring_sp_enqueue_bulk` (all-or-nothing) not `_burst` (best-effort). The 4 packets have sequential RTP sequence numbers and pacing timestamps — they must be sent as a unit. Partial enqueue would break batch integrity.

### Cooperative tx_burst: No Retry Loop
If `rte_eth_tx_burst()` returns fewer than requested:
1. Save unsent packets to `inflight` array
2. Return to scheduler immediately (don't starve other sessions)
3. Next tasklet invocation (~0.1–1µs later): retry inflight first

### Session Migration
`MTL_FLAG_TX_VIDEO_MIGRATE`: admin thread monitors scheduler CPU. If >95% busy, moves last session to less-loaded scheduler.
- Lock ordering: **target** manager mutex first, then **source**
- Move session pointer atomically (NULL old slot, set new)

### Thread Inventory

| Thread | Purpose | Detail |
|--------|---------|--------|
| Scheduler (×18 max) | Tasklet polling | Lcore-pinned or pthread |
| TSC Calibration (×1) | Measures TSC frequency | Joins before sessions start transmitting (called lazily by `_mt_start()` and pacing init) |
| Admin (×1) | Watchdog, migration | Periodic alarm-based |
| Statistics (×1) | Stat dump | Configurable `dump_period_us` |
| CNI (×1) | ARP/PTP/DHCP/IGMP | Tasklet or thread mode (`MTL_FLAG_CNI_THREAD`) |
| Socket TX/RX (×4 each) | Kernel socket backend | Only with `kernel:` backend |
| SRSS (×1) | Shared RSS polling | Only for NICs without flow director |

---

## §3 Memory Management

### Two-World Memory Model
- **Hugepage** (DPDK): frames, mbufs, mempools, rings — via `rte_malloc` / `rte_zmalloc`
- **Heap** (libc): control structures, configs, strings — via `mt_rte_zmalloc` wrapper for tracked allocation

### NUMA Matters
All DPDK allocations take `socket_id` from `mt_socket_id(impl, port)`. Socket mismatch → 2× DMA latency. Fallback: if preferred socket has no hugepages, allocate from any socket (logs warning).

### Frame Ownership Chains

**TX**: App fills buffer → MTL attaches as extbuf → NIC DMAs → extbuf refcnt drops to 0 → buffer freed for reuse

**RX**: NIC receives → memcpy into pre-allocated frame buffer → app reads → app returns → buffer freed for reuse

### Mempool Architecture

| Scope | Pool Per | Sizing Logic | Why |
|-------|----------|-------------|-----|
| TX video | Session | `2^q-1` ≥ `nb_tx_desc + bulk` | Isolation: one session's leak can't starve others |
| TX audio/anc | Session | Same formula, smaller numbers | Same isolation |
| RX | Queue | `2^q-1` ≥ `nb_rx_desc × 1.5` | Multiple sessions share queue; pool sized to queue |

Pool sizing formula: find smallest `2^q - 1` ≥ desired count. Implemented in `mt_util.c`.

Default mempool ops: `"stack"` (LIFO) — better cache reuse than FIFO ring ops. LIFO returns the most-recently-freed mbuf (L1/L2 hot, ~3-5ns). FIFO returns the oldest freed mbuf (L3/DRAM cold, ~15-40ns). Over 260K alloc/free cycles/sec per 1080p60 stream, this saves ~2.6-9.1ms/sec. The spinlock in stack ops is uncontended (single-threaded access) — costs ~1ns.

### Zero-Copy TX Design
- Header mbufs: allocated from per-session pool, contain RTP/UDP/IP/Eth headers (~62 bytes)
- Chain mbufs: `rte_pktmbuf_attach_extbuf()` points to app's frame buffer (no copy)
  - In **IOVA VA mode** (default with `--in-memory`): chain pool `data_room = 0` — chain mbufs are pure pointer containers
  - In **IOVA PA mode** (legacy): chain pool `data_room = s->st20_pkt_len` (~1200-1260B, session-calculated) — cross-page payloads copied into mbuf
- Cross-page fallback: if payload spans hugepage boundary, memcpy into chain mbuf's data room instead of extbuf
- `rte_mbuf_ext_shared_info` callback (`tv_frame_free_cb`) decrements frame refcnt
- **Cost without zero-copy**: memcpy at 312 MB/s per 1080p60 stream, 1.2 GB/s at 4K60. A 16-stream appliance would need ~19 GB/s just for copying — most of a NUMA node's memory bandwidth
- NIC scatter-gather DMA: segment 1 = header mbuf (~62B), segment 2 = chain extbuf (~1260B). Requires `RTE_ETH_TX_OFFLOAD_MULTI_SEGS`

### RX Frame Buffers
- Allocated via `mt_rte_zmalloc_socket()` (`rte_zmalloc_socket` wrapper) — zero-initialized
- `framebuff_cnt` typically 3: one assembling, one for app, one spare
- Zero-init rationale: partial frames (packet loss) have zeroed gaps instead of garbage

### DMA Copy Engine (RX)

Decision tree for each received payload:
```text
payload_size >= 1024 (ST_RX_VIDEO_DMA_MIN_SIZE)?
  ├─ YES → DMA copy (borrow mbuf until DMA completes, inc dma_nb)
  └─ NO  → CPU memcpy (immediate, no tracking needed)
```
- DMA must not cross hugepage boundaries
- Borrowed mbufs tracked via `lender` field in `st_rx_muf_priv_data`; returned after DMA completion
- Frame completion waits for `dma_nb == 0`

### mbuf Private Data (`mt_muf_priv_data`)
**Union** (32 bytes) of TX and RX variants:

| TX fields (`st_tx_muf_priv_data`) | RX fields (`st_rx_muf_priv_data`) |
|---|---|
| `tsc_time_stamp` (8B) — pacing timestamp | `offset` — destination offset in frame |
| `ptp_time_stamp` (8B) — RTP timestamp source | `len` — payload length |
| `priv` (8B) — pointer to owning session/frame | `lender` — borrowed mbuf pointer (DMA) |
| `idx` (4B) — packet index within frame | `padding` |

Chain mbufs have `priv_size = 0` — metadata in header mbuf only.

### Why Not a Mempool for Frame Buffers?
Frame buffers are large (5.2 MB), few (3 per session), long-lived (entire session), and DMA-referenced. A mempool would waste hugepage memory on rounding/alignment/metadata. The alloc/free pattern (refcount/state machine) doesn't match pool semantics (alloc/free). Use `rte_zmalloc_socket` instead.

### RX Session-Level Frame State (Separate from Pipeline States)
Simple refcount-based lifecycle at the session layer:
- `refcnt = 0` → FREE (available for `rv_get_frame()` — linear scan for refcnt==0, atomic increment)
- `refcnt = 1` → IN USE (being filled by RX assembly, or held by app)
- `rte_atomic32_dec` → FREE (returned by app via `rv_put_frame()`)

The pipeline layer (`st20p_rx_frame_status`) adds richer states on top of this.

### Key Constants & Gotchas
- Pool names include `recovery_idx` suffix: `_HDR_0`, `_HDR_1`... (unique across recovery cycles)
- `rte_mempool_create` fails on duplicate names — recovery_idx prevents this
- ASAN: Meson option `enable_asan`, poisons freed hugepage memory
- EAL flags:
  - `--match-allocations`: Forces 1:1 allocation→hugepage mapping. Without it, DPDK may merge adjacent allocations and freeing one pool won't reclaim hugepages because they're merged with a live allocation
  - `--in-memory`: Disables shared memory files in `/var/run/dpdk/`. Avoids filesystem ops, permission issues, stale lockfiles after crash
- x86 cache line: 64 bytes (`RTE_CACHE_LINE_SIZE`)
- Default descriptors: `MT_DEV_RX_DESC = 4096/2 = 2048`, `MT_DEV_TX_DESC = 4096/8 = 512` (in `mt_dev.h`)
- RX mempool element count: `nb_rx_desc + 1024` (headroom), rounded to `2^q - 1 = 4095`
- Mempool element size cache-aligned: e.g., 1494 → 1536 (rounded to `MT_MBUF_CACHE_SIZE = 128`)

---

## §4 Concurrency & Locking

### Two-Tier Locking
- **Data plane**: `rte_spinlock_t` — never sleeps, used in tasklets
- **Control plane**: `pthread_mutex_t` — allowed to sleep, used in create/destroy

### Session Access Pattern
Three variants:
- `rx_video_session_get()` — blocking `rte_spinlock_lock`
- `rx_video_session_try_get()` — non-blocking `rte_spinlock_trylock`
- `rx_video_session_get_timeout()` — `mt_spinlock_lock_timeout`

Paired with `rx_video_session_put()` (unlock). Always get→work→put.

### Lock Ordering
- Manager mutex → session spinlock (never reverse)
- Migration: target manager mutex → source manager mutex

### Atomics Usage
- Frame refcnt (`rte_atomic32_t`) — TX extbuf lifecycle
- Session count in managers — manager needs to know when all sessions gone
- Stats counters — relaxed ordering sufficient
- RX bitmap `mt_bitmap_test_and_set()` — atomic test-and-set per packet
- RX DMA mbuf borrowing — `lender` field tracks borrowed mbufs

### Shared Queue Contention
- TX Shared Queue (TSQ): spinlock-protected, multiple sessions enqueue
- RX Shared Queue (RSQ): dispatcher thread distributes mbufs to per-session rings

### Common Race Conditions
- Create vs destroy: manager mutex prevents
- Port reset during active sessions: drain queues first
- Stats read during session teardown: get/put pattern protects

---

## §5 Pacing, Timing & Performance

### Why Pacing Exists
ST2110-21 mandates packets spread evenly across frame period. 1080p60: ~4,500 packets in ~16.7ms, ideal spacing ~3.7µs (`trs`).

### Hardware Rate Limiter (RL) — Preferred
Intel E810 Traffic Manager enforces per-flow rate limits:
- Configured with exact wire rate adjusted for blanking (`reactive` factor: active_lines/total_lines, e.g., 1080/1125)
- NIC hardware spaces packets — CPU just enqueues
- Sub-microsecond accuracy, zero CPU overhead for timing
- Max shapers: `MT_MAX_RL_ITEMS = 128` per port

### Software TSC Pacing — Fallback
When no hardware RL available:
- Read TSC → compare against `pacing.tsc_time_cursor`
- Too early → return, let scheduler poll again
- On time → transmit

### Bulk Size: Why 4
`ST_SESSION_MAX_BULK = 4` (compile-time constant in `st_header.h`). Sizes `trs_inflight[port][4]` arrays.

**RL mode**: NIC paces regardless of CPU burst size. RL transmitter sends up to 8 packets per iteration (double-call). VRX compensation is for NIC burst behavior (`max_burst_size=2048`):
```c
pacing->vrx -= 2; /* VRX compensate to rl burst */
pacing->vrx -= 2; /* leave VRX space for deviation */
```

**TSC mode**: CPU controls timing. Bulk=4 → 3 packets of VRX budget consumed:
```c
pacing->vrx -= (s->bulk - 1); /* compensate for bulk */
```

**TSC Narrow mode**: Forces `s->bulk = 1` for maximum accuracy.

| Constraint | Impact |
|-----------|--------|
| `ST_SESSION_MAX_BULK = 4` | Array sizing, compile-time |
| Ring efficiency | Amortizes ring atomics over 4 entries |
| PCIe doorbell | ~200-500ns per `tx_burst` MMIO. Bulk=4 → ~65K doorbells/sec vs 260K at bulk=1 |
| TSC VRX headroom | 3 of ~8 packets Narrow budget consumed |
| `ST20_TX_FLAG_DISABLE_BULK` | App can override to bulk=1 |

### Warm-Up Padding
Hardware RL has ramp-up delay. MTL sends padding packets (RTP padding bit set) before first frame. Default: 80% of `pkts_in_tr_offset`, capped at 128.

### PTP: The Time Reference
- MTL implements PTP slave in software (`mt_ptp.c`)
- NIC hardware timestamps PTP packets
- Every RTP packet carries PTP-derived timestamp
- Without PTP: local TSC-based pacing works but clocks drift

### TSN / LaunchTime Pacing (E830)
- E830 supports per-packet TX scheduling via LaunchTime descriptors
- Requires `RTE_ETH_TX_OFFLOAD_SEND_ON_TIMESTAMP` in port txmode offloads (`mt_dev.c`)
- ICE driver stores `(txtime_ns % 1e9) >> 7` = 128ns-resolution sub-second timestamp
- Descriptor format: 32-bit, bits 0-12 = TX desc index, bits 13-31 = 19-bit tstamp
- 19-bit tstamp wraps every 2^19 × 128ns = 67.1ms (fine for frames < 33ms)
- `ts_round_type` field (2-bit) exists in txtime queue context but is left at 0 (truncate/floor) — never explicitly set by driver
- `ptp_time_cursor` (uint64_t, st_header.h) stored in mbuf private area → transmitter copies to dynfield → ICE driver writes to HW tsq ring
- **Critical**: NIC truncates `>> 7` (floor to 128ns). For trs=7776ns (1080p30): 7776%128=96ns residual. Without alignment, the 96ns lost per packet compresses frame span by ~400µs (31.6ms vs 32.0ms), causing Cinst/VRX violations.
- **Alignment fix**: `pacing->ptp_cursor_128ns_align = true` rounds ptp_time_cursor UP to 128ns after each trs step in `pacing_forward_cursor()`
- Flag set in `tv_init_pacing()` when `pacing_way == ST21_TX_PACING_WAY_TSN`
- Also applied at frame start in `tv_sync_pacing()` for the initial cursor

### TSN vs RL/TSC Transmitter Differences
- **TSN transmitter** (`video_trs_launch_time_tasklet`): uses per-bulk software TSC gating together with NIC LaunchTime timestamps. TSN still stamps each packet with a PTP LaunchTime, but the transmitter waits until the first packet's target TSC before handing the bulk to the NIC.
- **RL/TSC transmitter**: software-gated — waits for `cur_tsc >= target_tsc` before bursting
- TSN **build timeout is skipped**: NIC backpressure naturally rate-limits the builder to ~frame_active_time
- Current rollback baseline keeps the older TSN SW ring policy: `2 * total_pkts`, rounded to power-of-two.
- TSN **inflight counts appear high** (normal): ~total_pkts × bulk in NIC TX ring at any time
- Current tree keeps only TSN observability additions on top of that baseline. Recent floor / catch-up / admission-window experiments should be treated as rejected history, not current behavior.

### TSN LaunchTime Forward Bias (czwartek17-18 discovery)
- **Problem**: ~109µs/frame processing overhead causes accumulated drift: LTs transition from future→past over ~303 frames. Mode transition creates 6.5ms wire gap → VRX=-829.
- **Historical fix**: decouple PTP cursor (LaunchTime) from TSC cursor (builder rate). Original TSN lead was `lt_bias_ns = frame_time` ONLY on `ptp_time_cursor`.
- **Current code**: the rollback baseline no longer applies `lt_bias_ns`; TSN LaunchTime again seeds directly from the epoch-derived `start_time_tai`.
- **Does NOT require `--nb_tx_desc 8096`**: Default 512 TX desc ring is sufficient. The NIC holds only the next ~512 packets, not the entire bias window.
- Do **not** assume analyzer TRO is simply `tr_offset + lt_bias_ns`. Even historical one-frame bias runs coexisted with very different analyzer phase outcomes depending on RTP anchoring.
- **E830 dual-mode**: Future LTs → batch per doorbell (CINST narrow-compliant). Past LTs → per-descriptor (higher CINST). With bias, we FORCE future-LT mode permanently.

### TSN Drift Root Cause (czwartek20 analysis)
- **Measured drift**: +109µs/frame (PTP and TSC agree). Epoch drop every ~306 frames (~10s).
- **Breakdown via overhead instrumentation**:
  - Inter-frame overhead: avg 34µs, max 118µs. Dominant: `tv_notify_frame_done` pipeline callback (mutex + condvar + app callback, max 114µs). `get_next_frame`: 5µs max. `tv_sync_pacing`: 14µs max.
  - Intra-frame overhead: ~75µs. Scheduler loop overhead (79ns avg) × 1029 bulks = ~81µs. Builder is ring-backpressure-limited (inflight_cnt ≈ every bulk → SW ring always full).
- **Builder rate limiting**: Builder → SW ring (full, ~8192 entries) → Transmitter → NIC ring → NIC wire rate. Transmitter has per-frame TSC gate on first packet (time_to_tx wait). Effective builder frame time = NIC drain (32ms) + transmitter TSC gate + inter-frame overhead → frame_time + 109µs.

### TSN Epoch Drop Suppression — FAILED (czwartek21)
- **Approach**: Suppress epoch drops for TSN (use `next_free_frame_slot` sequential instead of snap forward).
- **Result**: Builder lag grows unboundedly (457ms/10s). Wire = 100% frame_time utilized, ZERO slack for recovery. TSC gate bypass doesn't help because NIC is the bottleneck.
- **Lesson**: Epoch drops ARE the only recovery mechanism. Cannot be suppressed. Must decouple PTP cursor from epoch instead.

### TSN Monotonic PTP Cursor (czwartek21 fix, replaces failed suppression)
- **Problem**: Epoch drops are required for builder recovery, but cause `ptp_time_cursor` to jump → wire gap → VRX spike → NIC mode transition.
- **Fix**: Track `ptp_time_cursor` as monotonic counter independent of epoch. Uses Bresenham integer frame_time accumulation.
- **New fields in `st_tx_video_pacing`**: `tsn_ptp_cursor_base`, `tsn_ptp_frame_time_int/extra/denom`, `tsn_ptp_frame_accum`, `tsn_ptp_cursor_init`
- **Behavior**: Epoch drops snap `cur_epochs` and `tsc_time_cursor` forward (builder recovery). But `ptp_time_cursor = tsn_ptp_cursor_base` advances smoothly by frame_time — no wire gaps.
- **RTP**: Non-epoch path derives from `ptp_time_cursor` (monotonic) → automatically sequential.
- **Bresenham precision**: For 30fps: int=33333333, extra=10, denom=30. After 30 frames = exactly 1e9ns.
- **128ns alignment**: Applied to `ptp_time_cursor` copy only, NOT to `tsn_ptp_cursor_base` (avoids error accumulation).

### TSN Loop Diagnostics
- `tsn_loops ...` stats remain available on `st_tx_video_session_impl` even after reverting the hot path closer to the historical per-bulk TSN pacing model.
- Current expected steady-state after the revert is near `build_avg=1.00` and `tx_avg=1.00`; these counters are kept for regression comparison, not as proof of work-conserving operation.
- `tsn-256-revert` showed an important hybrid lesson: restoring the historical per-bulk TSN transmitter without also reducing TSN SW-ring depth brought back `ring_peak=16380/16380`, `lt_pkts future=0%`, and packet_ts_vs_rtp_ts around `95-101ms`. On the current RxTxApp workload, `2 * total_pkts` is too deep for TSN even with the old gating model; the next validated direction is the hybrid of historical gating plus a smaller TSN SW ring.
- `tsn-256-revert2` validated that hybrid direction: shrinking the TSN SW ring to `8192` cut drift from about `109us/frame` to about `87us/frame`, reduced `T` from `5` to `4`, dropped `avg_tro_ns` from about `26.8ms` to about `8.95ms`, and reduced packet_ts_vs_rtp_ts from about `95-101ms` to about `40-46ms`. But `lt_pkts future` remained only `0.0-0.2%`, so queue residency improved materially without yet restoring future-LT submission.
- `tsn-256-revert5` with the `4096` TSN ring was the first strong proof that queue depth is no longer the dominant blocker: logs showed `epoch drop 0`, `ttx_bands future=298 past=0`, `lt_pkts future=99.3%`, and queue depth fell to `C:6 T:2`, while local pcap timing stayed clean. The remaining analyzer failure became a stable RTP phase offset: `avg_tro_ns ~1.239ms` (near `tro_default_ns ~1.274ms`) but `packet_ts_vs_rtp_ts ~134.572ms = 4 * frame_time + TRO`.
- That `revert5` result means TSN still needs an exact RTP clock, but its seed must match the transmit-phase model. A pure build-count seed from `tai_from_frame_count(cur_epochs)` can preserve a startup / epoch-drop frame slip forever.
- `tsn-256-revert6` rejected that LT-derived TSN RTP re-anchor. Even with `epoch drop 0`, `ttx_bands future=298`, `lt_pkts future=99.3%`, and the same shallow queue (`C:6 T:2`), local pcap regressed to a classic single `+6.632ms` phase step followed by a shifted plateau, plus a later burst. Analyzer output also flipped sign on `packet_ts_vs_rtp_ts` (about `-15.8ms .. -9.2ms`) and drove `avg_tro_ns` up to about `22.5ms`. Conclusion: do not derive TSN RTP from `ptp_time_cursor`/LaunchTime offset on this path; keep the `revert5` exact RTP accumulator seeded from `tai_from_frame_count(cur_epochs)` until a safer phase model is proven.
- `tsn-256-revert7` mostly restored the `revert5` transport behavior after backing out the `revert6` RTP patch: logs again showed the `4096` TSN ring, `epoch drop 0`, queue depth near `C:6 T:2`, and steady-state `lt_pkts future` climbing back to `99.3%`. But the run was not fully clean: local pcap showed one isolated late event around frames `15-16` with a `6.887ms` intra-frame gap followed by an immediate burst (`max_burst` about `250` / `236`), which was enough to flip both `2110_21_cinst` and `2110_21_vrx` back to `not_compliant` while leaving the same stable `packet_ts_vs_rtp_ts ~134.7ms` offset in place. That made burst-path instrumentation the right next debugging step.
- `tsn-256-revert8` validated temporary dequeue/retry instrumentation and changed the diagnosis again. Local pcap returned to a clean narrow wire shape after the truncated first frame, `2110_21_cinst` became compliant again, and the analyzer failure collapsed to a stable phase error: `packet_ts_vs_rtp_ts ~124.722ms`, `avg_tro_ns ~24.722ms`, `vrx histogram [-3015, 100]`. The key conclusion survived after removing that temporary instrumentation: the active blocker in the healthy `4096`-ring regime is absolute RTP/VRX phase alignment, not recurring deep-residency collapse.
- `tsn-256-revert10` rejected another TSN RTP re-anchor idea. Keeping the exact TSN RTP stepper but seeding it from `transmission_start_time(cur_epochs)` reduced the analyzer average offset from about `14.0ms` to about `7.0ms`, but local pcap showed a clean `+6.633ms` phase step around frame 5 followed by a shifted plateau. `2110_21_cinst` still passed and transport stayed healthy, so this should be treated as another timestamp-anchoring regression, not a transport fix.
- Current rollback baseline no longer carries the exact TSN RTP stepper. TSN RTP handling is back to the older epoch/PTP-cursor path; keep the startup/stable diagnostics, but treat the exact-stepper notes in this section as historical experiment context.
- `tsn-256-revert11` then backed out the `revert10` RTP-seed experiment and restored a clean local wire shape: no burst, no phase step, no inter-frame gap anomaly after startup. But the analyzer did not return to the `revert9` plateau; instead it landed on a new constant offset with `packet_ts_vs_rtp_ts ~ avg_tro_ns ~ 21.729ms` and a single-bin VRX histogram near `-2630`. This means the remaining failure is still a clean absolute TSN phase-selection problem that can move between startup plateaus while transport stays healthy.
- `tsn-256-revert12` added startup-only TSN trace logs and changed the diagnosis again: frames 0-5 showed consecutive epochs, no late-advance, a single TSN RTP resync followed by exact `+3000` steps, and packet-0 LaunchTime still about one frame in the future while software submission was only microseconds late. Local pcap stayed clean through frames 1-16 (`TRO ~0us`) and then hit a later one-off discontinuity: frame 17 max internal gap `~35.49ms`, frame 18 `TRO ~42.09ms`, frame 19 `dRTP=6000` with `~1us` inter-frame gap. Analyzer improved sharply (`avg_tro_ns ~2.048ms`, `packet_ts_vs_rtp_ts avg ~3.715ms`) but remained non-compliant due to that outlier. Conclusion: do not assume every bad run is startup plateau selection; this path can also fail later via a discrete frame / RTP discontinuity.
- `tsn-256-revert13` widened the TSN trace window to 40 frames and added builder frame-done, last-packet, and frame-boundary timing logs. Analyzer still had exact `inter_frame_rtp_ts_delta=3000` and compliant `cinst`, but local pcap showed repeated `+6.633ms` wire steps from `~7.974ms` frame-boundary gaps. In the traced 40-frame window, software frame targets stayed exact (`gap_rtp=3000`, target boundary `~1.341ms`, no extra RTP resync), so the suspected fault moved downstream of the planned builder/epoch/RTP schedule.
- `tsn-256-revert14` split that downstream hypothesis further. Local pcap stayed clean through frames 1-28, then stepped by `+6.633ms` at frame 29 and again at frame 59 from the same `~7.974ms` boundary gap. The important software trace change happened earlier: packet-0 submit headroom collapsed from about `9.11ms` at frame 25 to about `2.4-2.8ms` from frame 26 onward, while `tv_sync_pacing()` still showed the frame itself being scheduled about `9ms` ahead. That means the headroom is being burned between sync and packet-0 submit. Also, the new planned-gap vs submit-gap `TSN BOUNDARY ANOMALY[...]` comparison is a false-positive diagnostic: even healthy frames show only `~10-20us` between previous last-packet submit and next packet-0 submit because packet-0 is normally submitted much earlier relative to LaunchTime than the previous frame's last packet. Use continuous packet submit headroom (`delta_ptp_ns` / `delta_tsc_ns`) as the real signal, not raw submit-gap mismatch.
- Current follow-up trace after `revert14` stores per-frame TSN timestamps at sync completion, first-bulk enqueue, first-bulk dequeue, and packet-0 submit. The useful post-startup trigger is `TSN HEADROOM ANOMALY[...]` when packet-0 LaunchTime headroom drops below `5ms`; that log includes sync→enqueue, enqueue→dequeue, and dequeue→submit spans so the next run can localize where the missing `~6ms` is spent.
- `tsn-256-revert15` added one practical correction: the user's packet capture begins only after about `20s`, once PTP is stable. Startup frame-0..39 traces are therefore not the right correlation window for the pcap/json artifacts. The trace build now also emits a delayed `TSN STABLE ...` window starting `20s` after the first TSN sync, and first-bulk enqueue timing is recorded even when the first bulk briefly sat in builder inflight before reaching the ring.
- `tsn-256-revert16` finally tied the late wire failure to both sides at once. In the delayed `TSN STABLE ...` window, frames 0-32 were healthy (`time_to_tx ~33.2ms`, `enqueue_to_deq ~31.83ms`, packet-0 submit gap `~1.35ms`), then frame 33 switched modes: previous-frame `TX_LAST` was about `4.9ms` late on the TSC schedule, next-frame `DEQ` was delayed by the same `~4.9ms`, packet-0 submit gap collapsed to only `~15us`, and `time_to_tx` fell to `~29.6ms` while enqueue stayed cheap. A few seconds later, the actual pcap failures with `dRTP=6000` matched real epoch skips logged by `tv_update_tsn_rtp_time_stamp()` (`...1300 -> ...1302`, `...1310 -> ...1312`). Treat this as confirmation that the active root cause is still transmitter-side dequeue / backpressure / TSC-gate lag that eventually forces epoch-drop recovery, not a standalone RTP clock bug.
- Follow-up diagnostics after `revert16` now emit `TSN MODE[...]` transition logs and per-stat-period `tsn_mode[port] ...` summaries. The mode classifier is intentionally tied to the three signals that changed at the healthy→lagging flip: first-dequeue headroom collapse, previous-frame `TX_LAST` lateness, and packet-0 submit-gap collapse. Use those logs to decide whether a transmit-side gating/backpressure fix is actually causal before changing RTP math again.
- `tsn-piatek` (2026-03-27, plain `--pacing_way tsn`) added an important correlation split: the run log still had real TSN headroom collapse and non-consecutive epoch recovery **before** the user capture window, but the captured `pcap/json` then showed exact `inter_frame_rtp_ts_delta=3000`, compliant `2110_21_cinst`, and a pure stepwise absolute phase error instead. Local pcap plateaus landed near `4.577ms`, `11.210ms`, `17.843ms`, `24.476ms`, and `31.109ms`, with each new plateau created by one `~8.013ms` inter-frame gap. Treat this as proof that a later capture can look like a clean fixed-latency / VRX problem even when earlier epoch-resync events seeded the bad phase outside the capture window.
- `tsn-piatek2` (after the frame-boundary split) changed the dominant evidence again: delayed `TSN STABLE ...` traces showed the TSN ring pinned at `1016/1020`, packet-0 first-dequeue happening about `48-49ms` after sync, and `TSN MODE` spending most samples in `lagging`. At the same time, packet-0 was already late in PTP (`delta_ptp_ns` about `-14ms .. -33ms`) while the TSC floor still forced only about one bulk per tasklet invocation (`TSN HEADROOM ANOMALY`, `tsn_loops ... tx_avg=1.00`, `tsc_floor~51k`). Treat the current dominant blocker as transmit-side recovery throttling for already-late LaunchTime bulks, not another RTP-clock problem.
- `tsn-piatek3` tested that late-bulk floor relaxation and rejected it as a complete fix. The change restored strongly positive packet-0 submit headroom in the delayed stable window (`TSN STABLE TX delta_ptp_ns ~ +21ms .. +28ms`, versus `-20ms .. +33ms` before), but it did **not** reduce first-dequeue residency (`sync_to_deq_ptp_ns` still about `49-55ms`) or the full-ring regime (`ring_peak=1020/1020`, `tsn_loops ... tx_avg=1.00`, `TSN MODE` mostly `lagging`). The pcap kept the same `+6.633ms` staircase plateaus and later regressed into a real discontinuity (`dRTP=6000`, frame-start burst `111`). Conclusion: do not keep loosening the TSC floor in isolation; the dominant blocker is still the persistent dequeue / TXQ / backpressure lag, not software submit headroom alone.
- Follow-up instrumentation after `tsn-piatek3` added three TSN transmit diagnostics aimed at that lagging regime: `tsn_txdiag[port] ...` per-stat-period counters, sampled `TSN TX STALL[...]` logs on partial/zero-accept retries, and sampled `TSN TX PASS_BUDGET[...]` logs for the later bounded catch-up experiment. In the rollback baseline, `tsn_txdiag` and `TSN TX STALL[...]` remain useful current diagnostics; the pass-budget path is historical.
- `tsn-piatek4.log` finally exposed a concrete TSN library-side failure mode: a partial accept (`1/4`) followed by repeated zero-progress retries of the remaining `3` packets while those packets still had about `+33ms` LaunchTime headroom and the SW ring stayed pinned full. The first fix direction is inside MTL, not RTP math: before retrying TSN inflight remnants, call `mt_txq_done_cleanup()` to reclaim completed descriptors, and if retry still makes zero progress, treat it as recoverable backpressure (`yield and retry later`) rather than a hard inflight burst failure. Watch the new `tsn_txdiag ... cleanup=` and `zero=` counters on the next rerun.
- `tsn-piatek5` tested that cleanup-before-retry fix and rejected it as sufficient. The new `cleanup=` counter became large immediately, proving the cleanup hook runs, but `zero=` stayed almost equal to `cleanup=` and the ring remained pinned full. Stable trace still hit a real `gap_rtp=6000`, and local pcap still showed the same `+6.633ms` staircase (`~4.823ms`, `11.455ms`, `18.089ms`, `24.722ms`). Conclusion: reclaiming completed descriptors is necessary but not enough; the TSN path still saturates downstream admission with future LaunchTime packets, so small inflight tails are starved even after cleanup. The next MTL fix must preserve explicit TXQ/NIC admission headroom under TSN backpressure instead of continuing to dequeue fresh future packets into a saturated path.
- Current TSN transmit policy after `tsn-piatek5`: gate NIC admission by LaunchTime headroom, not just by first-packet TSC. `video_trs_launch_time_tasklet()` now keeps packets in software until they are within a bounded admission window, sized from about one TXQ worth of packet time and capped at `frame_time / 8` (with a small minimum floor). Apply this gate to both fresh dequeues and inflight retries. Goal: keep the NIC queue from filling a full frame early with future LaunchTime packets while leaving recovery slack for small remainders. Read `tsn_txdiag` using the new `admit_wait=` and `admit_save=` counters.
- `tsn-piatek6` rejected that admission-window candidate hard enough that it had to be removed. Startup immediately regressed into huge dequeue residence (`TSN STARTUP DEQ ~78-112ms`), repeated epoch skips (`drop 28/29`), `dev_pkt_valid(... invalid nb_segs 3)` errors, and then an ICE Malicious Driver Detection event on the TX queue. The new gate did not preserve headroom; it stalled packet-0 long enough to fill the SW ring and destabilize the downstream TX path. Do not reuse that bounded-admission-window design on this path.

### ⚠️ Double Precision Gotcha: `uint64_t += double`
- `pacing_forward_cursor()` does `ptp_time_cursor += trs` where cursor is `uint64_t` and trs is `double`
- C promotes the `uint64_t` to `double` for the addition. At PTP magnitude ~1.77×10¹⁸ (2026), double ULP = 256ns
- Each step loses ~96ns on average. Over 4115 packets: **~395µs cumulative error per frame**
- This produces the SAME 31.6ms compressed frame span as the NIC's >>7 truncation — the 128ns alignment cannot help because the input values are already corrupted by double conversion
- **Fix required**: integer-only cursor arithmetic (Bresenham-style fractional accumulator or pre-quantized integer trs)
- Same concern applies to `tai_from_frame_count()` (`frame_count × frame_time` as double at 10¹⁸ scale) — mitigated by `nextafter(..., INFINITY)` but not perfect
- **Do not over-attribute epoch drops to this class**: for `cur_tai / frame_time` or `frame_count * frame_time`, double resolution at current PTP magnitude is still only 256ns. That is enough to create **boundary ambiguity** around epoch/frame rounding, but not enough to explain the observed 109µs/frame drift or the discrete ~6.63ms phase steps. Treat epoch/frame-count float math as a secondary correctness risk, not the primary throughput/root-cause signal.

### Epoch Timing
Frame transmission aligned to PTP epoch boundaries. Epoch = frame count since TAI time zero (`cur_epochs = ptp_time / frame_time`).

**Epoch drop condition** (in `calc_frame_count_since_epoch`):
- `frame_count_tai = cur_ptp / frame_time` (where are we now)
- `next_free_slot = cur_epochs + 1` (next slot we can use)
- If `frame_count_tai > next_free_slot` → **epoch drop** (skipped frames), snap forward to `frame_count_tai`
- **TSN mode**: epoch drops happen normally (builder recovery), but `ptp_time_cursor` is monotonic (no wire gap)
- Causes: app slow to provide frames, builder CPU-bound, ring full (transmitter backpressure), scheduler latency
- `stat_epoch_drop` counts skipped slots
- Opposite: `stat_epoch_onward` if building ahead of real time (> `max_onward_epochs` = 1 second's worth)

**Transmission start time per frame**:
```
start_time = tai_from_frame_count(epoch) + tr_offset - vrx × trs
```
where `tai_from_frame_count` uses `nextafter(epoch × frame_time, INFINITY)` to handle double precision at large epoch counts.

### VRX (Virtual Receiver Buffer) Conformance
RX diagnostic stats:
- `vrx_min` / `vrx_max` — buffer excursion bounds
- High vrx_max → sender too bursty
- Negative vrx_min → packets arriving late

### Performance Numbers (Intuition)
- 1080p60 uncompressed ≈ 5 Gbps
- 4K60 uncompressed ≈ 12 Gbps
- One E810 100G port ≈ 8× 4K60 or 20× 1080p60 (theoretical)
- RL: 128 shapers per port
- TSC resolution ≈ 1ns, scheduler poll adds 10-100µs jitter

### Performance Debugging Mental Model

1. **NIC bottleneck?** Check `stat_tx_burst` vs `stat_tx_bytes`
2. **Scheduler overloaded?** 100% CPU, never sleeping → too many sessions
3. **Pacing broken?** RX: VRX stats. TX: `stat_epoch_mismatch`, `stat_frame_late`
4. **NUMA problem?** Socket mismatch → 2× DMA latency
5. **Ring underflow?** Transmitter sends fewer packets than expected → increase ring or reduce builder load
6. **RX losing packets?** NIC `imissed` counter → increase `nb_rx_desc`, mempool, or drain faster (continuous burst: if `rte_eth_rx_burst` returns ≥ `rx_burst_size/2`, loop immediately via `MTL_TASKLET_HAS_PENDING`)
7. **RX memcpy bottleneck?** 4K60 = 1.2GB/s memcpy per stream. Use DMA engine for payloads ≥1024 bytes

### USDT Tracepoints
Probes in `lib/src/mt_usdt_provider.d`, compiled when `MTL_HAS_USDT`. Attach with bpftrace/SystemTap.

| Provider | Key Probes | Traces |
|----------|-----------|--------|
| `sys` | `log_msg`, `tasklet_time_measure` | Logging, tasklet timing |
| `ptp` | `ptp_msg`, `ptp_result` | PTP timestamps, sync delta |
| `st20`/`st22`/`st30` | `tx_frame_next/done`, `rx_frame_available/put` | Session frame lifecycle |
| `st20p`/`st22p`/`st30p`/`st40p` | `tx_frame_get/put/drop`, `rx_frame_get/put` | Pipeline frame transitions |

Several probes are **attach-to-enable**: zero cost until tracing tool attaches.

---

## §6 Session Lifecycle & Data Flow

- `st_tx_video_session.c`: TSN-specific sizing decisions that depend on `s->pacing_way[]`
  must happen **after** `s->pacing_way[i] = st_tx_pacing_way(...)` initialization. A real bug
  was observed where TSN ring sizing ran too early, silently leaving TSN on the default ~512
  entry SW ring; symptoms included `ring_peak=480/480`, high `tx_partial`, all LaunchTimes in
  the past, and misleading conclusions about builder/transmitter throughput.
- TX pipeline callback coupling matters under deep buffering: `tv_frame_free_cb()` calls
  `tv_notify_frame_done()`, which reaches `tx_st20p_frame_done()`. That function marks the frame
  `FREE` under `ctx->lock`, but `notify_frame_available` is only called **after** the app's
  `notify_frame_done` callback returns. If the app callback blocks, the next blocked
  `st20p_tx_get_frame()` wakeup is delayed even though the frame is already logically free.
  In TSN large-ring experiments this showed up as `frame_overhead avg ~33ms`,
  `notify_max ~40ms`, `busy as no ready frame from user` every frame, while `getframe` and
  `sync` remained only a few microseconds.
- Mitigation applied on 2026-03-25 across ST20/ST22/ST30/ST40 TX pipeline paths: once a frame
  transitions to `FREE` in `frame_done` / late-drop, `block_get` waiters are signaled
  immediately via the internal `*_block_wake()` helper **before** app callbacks run. This keeps
  public callback behavior mostly unchanged while decoupling blocking `get_frame()` reuse from
  slow app callbacks executed on the tasklet thread.
- Important validation caveat: RxTxApp TX (`tests/tools/RxTxApp/src/tx_st20p_app.c`) uses a
  dedicated producer thread with `ST20P_TX_FLAG_BLOCK_GET` and does **not** register
  `notify_frame_done` / `notify_frame_available`. Therefore callback-decoupling patches do not
  materially affect that workload; a `frame_overhead ~ frame_time` there mainly reflects waiting
  for the next reusable framebuffer, not callback execution time.
- TSN lead requirement depends on downstream queue depth, not just one frame. Historical smaller-ring
  and LT-bias experiments remain relevant for debugging, but the current rollback baseline has those
  behavior changes removed.
- RxTxApp ST20 pipeline TX is also back on the historical `ops.framebuff_cnt = 2` baseline in the
  current rollback tree.
- Monotonic TSN `ptp_time_cursor` had a second-order failure mode: if it advanced by exact
  `frame_time` regardless of epoch drops, every dropped epoch left the PTP/RTP cursor behind wall
  clock by one additional frame forever. This was proven directly from logs where
  `start_tai - ptp_cursor = 1.366666624s`, exactly 41 frame times. Symptom pattern: long runs get
  worse even after local improvements; pcap shows many perfect early frames, then a single
  `+6.63ms` phase step and a permanent shifted plateau.
- A bounded positive catch-up servo was tested on 2026-03-25 and then REMOVED after validation.
  It did not reduce `ptp_lag`; instead it broke RTP cadence (`inter_frame_rtp_ts_delta = 3045`
  instead of 3000), increased epoch drops to 6/10s, and dropped fps to ~29.3. Conclusion:
  do NOT modify the monotonic TSN cursor step away from exact frame_time if RTP timestamps are
  derived from that cursor. Keep `ptp_lag` as a diagnostic, not as an active correction path.
- Post-gpt6 TXQ-horizon hypothesis was tested and then REMOVED after gpt7. Validation showed
  `tx_wait_lt=0`, `wait_max=0us`, so the horizon never engaged on the real workload; do not retry
  this blindly.
- Current TSN direction: separate RTP progression from LaunchTime progression. TSN non-epoch RTP
  now advances with its own exact per-frame media-clock accumulator (integer+Bresenham), while LT
  may use bounded positive frame-boundary catch-up. Rule: LT-only correction is acceptable only
  after RTP has been decoupled from the corrected cursor.
- gpt8 refined that rule further: even after RTP/LT decoupling, changing the LT frame step away
  from exact `frame_time` still slowed wire cadence to ~`33.833ms/frame` (~29.4fps) while RTP
  stayed exact at `3000` ticks/frame. Conclusion: keep the dedicated TSN RTP clock, but do NOT
  add positive LT catch-up on the frame step.
- RxTxApp TS20 pipeline depth of `4` also became insufficient once TSN downstream residency grew:
  logs showed `4 frames are in trans, total 4` together with `ring_peak=16120` at `4115`
  packets/frame (~3.9 frames in the SW ring alone). Practical rule: if TSN logs show all current
  framebuffers in transmit, increase `framebuff_cnt` beyond that occupancy (next test moved to 8).
- After moving RxTxApp to `framebuff_cnt=8`, the producer-starvation symptom disappeared (`C:3 T:5`
  with no user-busy line), but epoch drops and zero future-LT remained. Next distinct experiment:
  keep exact LT frame_time stepping between drops, but re-anchor LT to the epoch-derived target
  only when an epoch-drop recovery happens. This targets the observed `~33333us` LT debt added per
  drop without reintroducing the failed per-frame LT servo.
- After adding epoch-drop-only LT re-anchor, `ptp_lag` reset to `0us` and TSN returned to the
  older baseline (`~29.9fps`, `~109us/frame` drift), proving that accumulated LT debt was real but
  not the last blocker.
- RL comparison now isolates the strongest remaining TSN-specific issue: **downstream residency**.
  In TSN, frame-start `time_to_tx` can be positive while packet-level `lt_pkts future` is still
  `0%`, which means future margin is being consumed between `tv_sync_pacing()` and actual NIC
  submission. Supporting log pattern: full SW ring (`16352/16352`), huge `ring_stop`, high
  `tx_partial`, and deeper in-transmit queue (`C:3 T:5`) versus RL's shallow `C:6 T:2` with all
  frames future and near-zero drift.
- Best next validation is a **queue-depth sweep** for TSN (`nb_tx_desc`, and if needed smaller TSN
  SW-ring target) while keeping pacing logic fixed. Do not mix this sweep with new timestamp/math
  changes.

### Session Creation Order (TX Video)
1. Validate ops (`*_ops_check`) — reject invalid configs before allocating
2. Acquire scheduler quota — find/create scheduler with capacity
3. Attach to manager — claim slot (protected by mutex)
4. Allocate resources — frames, mempools, rings (NUMA-pinned)
5. Acquire NIC TX queue
6. Initialize pacing (RL or TSC)
7. Register tasklets (builder + transmitter)

Cheap checks first, then scarce resources (scheduler, queue), then expensive (memory).

### Session Creation Order (RX Video)
Actual sequence in `rv_attach()`:
1. `rv_init_hw()` — acquire RX queue + install flow rule + drain queue
2. `rv_init_sw()` — internally calls `rv_alloc_frames()` then `rv_init_slot()`, allocates bitmap, creates rings
3. `rv_init_mcast()` — register multicast MAC + IGMP join (2 duplicate reports)
4. `rv_init_rtcp()` — conditional, if RTCP enabled
5. `rv_init_pkt_handler()` — select packet handler based on flags/features

Tasklet is per-manager (not per-session), registered in `st_rx_video_sessions_sch_init`.

Alternative path: `rv_detector_init()` instead of `rv_init_sw()` when auto-detect mode enabled.

### RX Packet Handlers (set by `rv_init_pkt_handler`)
- `rv_handle_rtp_pkt` — standard frame mode
- `rv_handle_frame_pkt` — frame mode variant
- `rv_handle_hdr_split_pkt` — header-split mode
- `rv_handle_st22_pkt` — compressed video
- `rv_handle_detect_pkt` — auto-detection mode
- `rv_handle_detect_err` — detection error handler

### TX Destroy Order (Load-Bearing)
Dependency chain: `NIC TX descriptors → hold mbufs → hold extbuf refs → point to frame buffers`

`tv_uinit()` comment: *"must uinit hw firstly as frame use shared external buffer"*:
1. Drain ring — `mt_ring_dequeue_clean()`
2. Free ring — `rte_ring_free()`
3. Flush TX queue — `mt_txq_flush()` sends pad packets to push all session mbufs out of NIC
4. Release TX queue — `mt_txq_put()`
5. Free pad packets
6. Free inflight — `rte_pktmbuf_free_bulk(inflight)`
7. Free chain pool — `tv_mempool_free()`
8. Free header pool — `tv_mempool_free()`
9. Free frame buffers — `rte_free(frame->addr)`

**Why HW before SW (steps 1-5 before 6-9)**: After step 3, NIC holds NO references. Only then safe to free pools/frames.

### RX Destroy Order
Actual sequence in `rv_uinit()`:
1. `rv_stop_pcap_dump()`
2. `rv_uinit_mcast()` — remove multicast MAC + IGMP leave
3. `rv_uinit_rtcp()`
4. `rv_uinit_sw()` — internally calls `rv_free_frames()`, frees bitmap, destroys rings
5. `rv_uinit_hw()` — destroy flow rule + release RX queue

**Key asymmetry with TX**: NO queue drain/flush on RX destroy. RX mbufs hold copies (memcpy'd) — not references to frame buffers via extbuf.

### Frame State Machines

**TX Pipeline** (`st20p_tx_frame_status`):
```text
FREE → IN_USER (app fills) → READY → IN_CONVERTING (optional) → CONVERTED → IN_TRANSMITTING → FREE
```
If no conversion needed: READY → IN_TRANSMITTING directly.
Late frame handling: if IN_USER when epoch arrives → skip. If older READY exists alongside newer READY → drop older (newest-first).

**RX Pipeline** (`st20p_rx_frame_status`):
```text
FREE → READY (transport delivers) → IN_CONVERTING (optional) → CONVERTED → IN_USER (app reads) → FREE
```

**Session-Level TX** (`st21_tx_frame_status`):
```text
WAIT_FRAME ──(get_next_frame)──► SENDING_PKTS ──(all pkts sent)──► WAIT_FRAME
```
Note: `ST21_TX_STAT_WAIT_PKTS` exists in enum but is dead code (never assigned/checked).

### RX Packet Assembly: Slot Design

**Why slots**: Packets from different frames (different RTP timestamps) can arrive interleaved. Slots allow concurrent assembly.

- `slot_max = 1` for single-port
- `slot_max = 2` for redundant (ST2110-7) or RTCP-enabled sessions

### Slot Assignment (`rv_slot_by_tmstamp`)
```text
1. SCAN: Check active slots — if tmstamp matches, return it
2. STALE: If T < active slot's tmstamp → drop packet (old frame)
3. DMA GUARD: If slot has pending DMA (dma_nb > 0) → don't evict
4. EVICT (round-robin): Pick next free/oldest slot. If occupied → notify partial frame (CORRUPTED status)
5. CLAIM: Acquire frame buffer, set tmstamp, clear bitmap, reset recv_size
6. RETURN: Caller writes payload into slot's frame
```

### RTP Header Layout (ST2110-20)
```text
Ethernet (14B) + IPv4 (20B) + UDP (8B) + RTP (12B) + SRD (8B) = 62 bytes
```
- `seq_number` (16-bit) + `seq_number_ext` (16-bit) → 32-bit sequence: `(ext << 16) | seq`
- `tmstamp` (32-bit) — same for all packets in one frame (frame ID)
- SRD: `srd_length`, `srd_row_number`, `srd_offset`, continuation bit for multi-line packets

### Frame Offset Calculation
```text
offset = srd_row_number × bytes_per_line + (srd_offset / pixel_coverage) × pg_size
```
Example (1080p YCbCr-422 10-bit): `bytes_per_line = 1920/2 × 5 = 4800`

### Bitmap Duplicate Detection
```text
bitmap_size = frame_size / 800 / 8    (normal path: st_rx_video_session.c:3243)
           = frame_size / 1000 / 8   (detect path: st_rx_video_session.c:2697)
Floor: max(bitmap_size, height * 2 / 8)
→ 1080p: 5,184,000 / 800 / 8 = 810 bytes

pkt_idx = (seq_id - base_seq_id) & 0xFFFFFFFF  // handles 32-bit wraparound
base_seq_id estimated from first packet: seq_id - (offset / payload_length)
mt_bitmap_test_and_set(bitmap, pkt_idx) → atomic, skip if already set
```

### Frame Completion
1. `recv_size >= frame_size`
2. `dma_nb == 0` (all DMA copies finished)
3. No pending mbuf borrows

Complete → `rv_slot_full_frame()` → `ST_FRAME_STATUS_COMPLETE`. Incomplete/evicted → `ST_FRAME_STATUS_CORRUPTED`.

### Inflight Pattern (Cooperative Non-Blocking Retry)
- **Builder inflight**: ring full → save packets to `s->inflight[port][]` → return → retry next iteration
- **Transmitter inflight**: `tx_burst` sends fewer → save unsent to `s->trs_inflight[]` → retry next iteration
- Never discard (data loss + refcount leak), never block (starves other sessions)

### Recovery via `recovery_idx`
When TX queue hangs (`st20_tx_queue_fatal_error`):
1. Drain rings → release old queue → `recovery_idx++`
2. Acquire new queue → `tv_mempool_free()` → `tv_mempool_init()` with new name suffix
3. Reset state → notify app (`ST_EVENT_RECOVERY_ERROR`)
4. If recovery fails → `s->active = false` → `ST_EVENT_FATAL_ERROR`

### RTCP Retransmission
Custom format (RTCP type 204, name "IMTL"):
- RX detects bitmap gaps → NACK with (start_seq, follow_count) ranges
- TX maintains circular buffer of recent packets → deep-copies for retransmit (original mbufs may be freed)
- Standard RTCP NACKs (RFC 4585) too inefficient for thousands of packets per frame

### Redundancy: Active-Active Merging

**Video RX** — Packet-level bitmap merge:
- Two ports with independent queues/flow rules
- Same bitmap for both paths — first write wins (atomic test-and-set)
- Even 50% loss on one path recoverable if other path has missing packets
- Extra slot: `slot_max = 2`

**Audio/Ancillary RX** — Timestamp-based dedup:
- Reject packets where RTP timestamp ≤ current (`mt_seq32_greater`)
- Ancillary also checks `seq_id` via `mt_seq16_greater`
- Safety valve: after `ST_SESSION_REDUNDANT_ERROR_THRESHOLD = 20` consecutive rejects, bypass filter
- "redundant error threshold reached" in logs → investigate source

**TX Redundancy**: Clone packet with different headers for second port. Clone shares same chain payload mbuf (refcnt incremented). Frame not free until BOTH ports complete DMA.

### IGMP Multicast Group Management
- Join: 2 duplicate IGMPv3 Membership Reports (reliability — IGMP has no ACK)
- Keepalive: 10-second periodic re-join (`IGMP_JOIN_GROUP_PERIOD_S = 10`)
- Leave: IGMPv3 Leave on session destroy
- Well-known group `224.0.0.1` always joined (accept IGMP Query from routers)
- Switch without IGMP snooping → multicast floods all ports (doesn't break MTL, wastes bandwidth)

### Audio/Ancillary/Fast-Metadata Sessions
Same lifecycle pattern as video, simplified:
- No per-packet pacing (frame-level)
- No assembly complexity (1-8 packets per frame)
- Low scheduler quota
- Shared queue friendly
- ST2110-41: payload type 115, header 58 bytes, API in `st41_api.h`

### Manager Pattern
Sessions organized into managers (`st_tx_video_sessions_mgr`), one per session type per scheduler:
- Fixed-size session array (indexed by `idx`)
- Manager mutex protects attach/detach
- Tasklet handler iterates all sessions in local array

Per-scheduler (not global) → no filtering needed in tasklet hot path.

### Global Init Ordering
`mtl_init()`: DMA → queues → ARP/mcast → CNI → admin → plugins → DHCP → PTP → TSC calibration thread

`mtl_start()` blocks on TSC calibration via `mt_wait_tsc_stable()` before starting ports.

Sessions can be created between init and start (allocate queues/mempools/flow rules against stopped port).

`MTL_FLAG_DEV_AUTO_START_STOP`: init calls start internally, stop becomes no-op.

### Plugin Contract
3-phase: Registration → Session binding → Frame exchange

Registration: `dlopen` → resolve 3 symbols (`st_plugin_get_meta`, `st_plugin_create`, `st_plugin_free`) → version/magic check

Binding: search by capability bitmask (`input_fmt_caps`, `output_fmt_caps`). First match wins.

- Plugins own their threads (not called from tasklets)
- Max 8 plugins per MTL instance (`ST_MAX_DL_PLUGINS = 8`)
- Loaded during `mtl_init()` from JSON config

### Validation Checklist (`*_ops_check()`)
- `num_port`: 1-2, `payload_type`: 0-127, `framebuff_cnt`: 2-8 (video)
- IP: not all zeros, multicast = 224.x-239.x, redundant ports must differ
- Required callbacks checked based on `type` field

---

## §7 DPDK Usage Patterns

### Abstraction Layer
Queue management abstracted via `mt_queue.c` (in `lib/src/datapath/`). Functions: `mt_txq_burst()`, `mt_txq_flush()`, etc.

### Port Initialization
Static functions in `mt_dev.c`:
- `dev_config_port()` — configure device (RSS, promiscuous, multi-seg TX, checksum offload)
- `dev_start_port()` — start device, then `rte_eth_stats_reset()` (clean baseline, prevents stale counters from probe/startup contaminating session stats)
- TX offloads: `MULTI_SEGS` (scatter-gather for header+chain) + `IPV4_CKSUM` (HW checksum). RX offloads: `0` (no scatter-gather, `rx_nseg=0`)
- `rte_eth_dev_set_ptypes()`: tells NIC to only classify TIMESYNC/ARP/VLAN/QINQ/ICMP/IPv4/UDP/FRAG — reduces per-packet classification overhead. Only applied when driver reports ≥5 supported ptypes
- No promiscuous mode by default — hardware flow rules steer traffic. `MTL_FLAG_NIC_RX_PROMISCUOUS` enables it for debugging

### TX Callback Safety Net
`dev_tx_pkt_check()` registered via `rte_eth_add_tx_callback()` on every TX queue. Validates each packet:
- `pkt_len > 16` (catches zero-length mbufs)
- `pkt_len ≤ MTL_MTU_MAX_BYTES` (catches oversized)
- `nb_segs ≤ 2` (catches corrupted scatter-gather chains)

Failing packets are replaced with pad mbuf (not dropped — preserves burst count). Disabled with `MTL_FLAG_TX_NO_BURST_CHK`. Cost: ~1ns per packet (two integer comparisons).

### Flow Rules
Created in `mt_flow.c`. Two patterns:
- **Multicast**: match destination IP only (`mt_is_multicast_ip()` check)
- **Unicast**: match both source and destination IP

No explicit ASM/SSM distinction in flow code — just multicast vs unicast.

### Multicast MAC Registration
IP-to-MAC formula in `mt_mcast_ip_to_mac()` (`mt_mcast.h`): `01:00:5E` + `ip[1]&0x7f` + `ip[2]` + `ip[3]`

Two driver paths:
- `rte_eth_dev_set_mc_addr_list()` (preferred, when using kernel control)
- `rte_eth_dev_mac_addr_add()` (fallback)

Well-known group `224.0.0.1` always joined for IGMP query reception (unless kernel-ctl or user-no-multicast flags).

### Queue Architecture

| Strategy | When Used | How |
|----------|----------|-----|
| **Dedicated queue** | Default for video | One TX/RX queue per session, best isolation |
| **Flow director** | NIC supports flow rules | Multiple sessions share port, flow rules steer to per-session queues |
| **Shared RX Queue (RSQ)** | Flow director unavailable | Dispatcher distributes mbufs to per-session rings |
| **Shared TX Queue (TSQ)** | Low-bandwidth sessions | Spinlock-protected, multiple sessions enqueue |
| **RSS** | Fallback | Hash-based distribution, no per-flow control |

### TX Pad Flush
`mt_txq_flush()` → `mt_dpdk_flush_tx_queue()`: sends `mt_if_nb_tx_burst(impl, port) × 2` pad packets (for DPDK ports, `nb_tx_burst = nb_tx_desc`).

Pad destination MAC: `01:80:C2:00:00:01` (IEEE slow-protocol, switches won't forward).

Why pads instead of `tx_done_cleanup`: E810 ice driver does NOT implement `rte_eth_tx_done_cleanup` (returns `-ENOTSUP`).
Even on NICs that support it, the API only frees already-completed descriptors — if the NIC hasn't finished DMA-reading an mbuf, cleanup won't touch it.
Pads actively force the ring to cycle, guaranteeing completion regardless of NIC state.
(Note: MTL does call `rte_eth_tx_done_cleanup` in shared TX queue path as best-effort, but pad flush is the authoritative cleanup.)

### RX Burst Pattern
Default burst: 128 (runtime assignment: `s->rx_burst_size = 128`). RX packets fit in a single mbuf because `data_room = 1664` > max packet size (1494 bytes) — no multi-segment reassembly needed.

Continuous burst: if `rte_eth_rx_burst` returns ≥ `rx_burst_size / 2` (≥64), track as continuous burst. Tasklet returns `MTL_TASKLET_HAS_PENDING` when any packets received → scheduler re-polls immediately.

### Header-Split RX
Intel E810 with `ST20_RX_FLAG_HDR_SPLIT`: NIC writes payload directly into frame buffer, bypassing CPU memcpy. Requires DPDK ice driver patches.

### mbuf Lifecycle

**TX**: alloc header → fill headers → chain extbuf → enqueue ring → tx_burst → NIC DMA → mbuf freed back to pool (extbuf refcnt dec)

**RX**: NIC writes to mbuf data room → rx_burst returns mbuf → parse + memcpy/DMA payload to frame → `rte_pktmbuf_free()` returns to pool

### Traffic Manager (Rate Limiter)
Hierarchy: port → shapers (max 128, `MT_MAX_RL_ITEMS`). Each shaper = one session's rate limit.

### Multi-Process
MTL supports `--proc-type=secondary` for read-only stats access. Full multi-process TX/RX not supported.

### Backend Support

| Backend | Prefix | When To Use |
|---------|--------|-------------|
| DPDK PMD | (PCI BDF) | Production — full speed |
| AF_XDP | `native_af_xdp:` or `dpdk_af_xdp:` | When kernel driver can't unbind |
| AF_PACKET | `dpdk_af_packet:` | Testing only — very slow |
| Kernel socket | `kernel:` | Development/testing — no DPDK needed |

### Debugging
- `rte_eth_stats_get()` → check `imissed`, `ierrors`, `oerrors`
- `rte_eth_xstats_get()` → detailed per-queue stats
- `RTE_LOG_LEVEL` controls DPDK log verbosity

---

## §8 Testing

### Test Layers

| Layer | Framework | Language | What | Speed |
|-------|-----------|----------|------|-------|
| Integration (gtest) | Google Test | C++ | Internal APIs via loopback | Minutes |
| Fuzz | LLVM libFuzzer | C | RX packet parsers | Hours |
| Validation (pytest) | pytest | Python | E2E via RxTxApp/FFmpeg/GStreamer | 10s of minutes |
| Shell scripts | bash | Shell | JSON-driven loopback scenarios | Minutes each |

### Integration Tests (`tests/integration_tests/`)

Three binaries:
- `KahawaiTest` — main ST2110 tests (links MTL)
- `KahawaiUfdTest` — UDP file descriptor tests (links MTL)
- `KahawaiUplTest` — LD_PRELOAD tests (**no** MTL link, standard POSIX sockets)

Architecture: single global `mtl_init()` for entire run (`st_test_ctx()`). Sessions ephemeral per test.

Key CLI flags: `--p_port`, `--r_port`, `--log_level`, `--level` (all/mandatory), `--pacing_way`, `--rss_mode`, `--tsc`

#### Test Suites

| Suite | What |
|-------|------|
| `Main` | Init/start/stop, bandwidth calc, format utils |
| `Misc` | Version, memcpy, PTP, NUMA |
| `St20_tx`/`St20_rx` | Session-level video |
| `St20p` | Pipeline video, plugins, digest, RTCP |
| `St22_tx`/`St22_rx` | Compressed video session |
| `St22p` | Compressed pipeline, encode/decode |
| `St30_tx`/`St30_rx` | Audio session |
| `St30p` | Audio pipeline |
| `St40_tx`/`St40_rx` | Ancillary |
| `Sch` | Scheduler create/tasklet registration |
| `Dma` | DMA copy/fill sweeps |
| `Cvt` | Pixel format conversion (4349 lines!) |

#### Verification Patterns
- **Data integrity (digest)**: TX SHA256 → RX SHA256 → compare
- **FPS accuracy**: Count frames over timed window
- **Create/free stress**: `create_free_test(base, step, repeat)`
- **Expected failure**: Invalid params → expect NULL
- **Level filtering**: `MANDATORY` always runs; `ALL` needs `--level all`

#### Noctx Tests (`tests/integration_tests/noctx/`)
Tests needing isolated `mtl_init`/`mtl_uninit` per test. Run via `noctx/run.sh` (serial, 10s cooldown). Requires 4 ports.

### Fuzz Tests (`tests/fuzz/`)

| Target | File | What |
|--------|------|------|
| ST2110-20 video RX | `st20/st20_rx_frame_fuzz.c` | RFC4175 video parsing |
| ST2110-22 compressed RX | `st22/st22_rx_frame_fuzz.c` | RFC9134 compressed parsing |
| ST2110-30 audio RX | `st30/st30_rx_frame_fuzz.c` | Audio parsing |
| ST2110-40 ancillary RX | `st40/st40_rx_rtp_fuzz.c` | Ancillary parsing |
| ST40 helpers | `st40/st40_ancillary_helpers_fuzz.c` | UDW, checksum, parity |

Architecture: minimal DPDK EAL (no hugepages, no PCI) → full session context reset per input → call internal handler.

**Limitations**: single-packet fuzzing only (no multi-packet sequence bugs), no TX/control/PTP coverage.

```bash
meson setup build_fuzz -Denable_fuzzing=true -Denable_asan=true
ninja -C build_fuzz
./build_fuzz/tests/fuzz/st20_rx_frame_fuzz corpus/st20
```

### Validation Tests (`tests/validation/`)
E2E framework launching real MTL apps over SSH. Tests do NOT call MTL C API.

Config: `topology_config.yaml` (hosts, PCI BDFs) + `test_config.yaml` (session_id, paths)

Validation methods: log parsing, FPS check, MD5 per-frame integrity, EBU LIST compliance (optional)

- EBU LIST helper scripts live in `tests/validation/compliance/`: `upload_pcap.py` uploads only, while `upload_and_download_report.py` uploads then polls `/api/pcap/<uuid>/report?type=json` until `analyzed=true` and writes `<pcap>.json` by default.
- LIST API flow used by validation: `POST /auth/login` → bearer token, `PUT /api/pcap` → upload returns `uuid`, `GET /api/pcap/<uuid>/report?type=json` → report, optional `DELETE /api/pcap/<uuid>` cleanup.

Markers: `@pytest.mark.smoke`, `@pytest.mark.nightly`, `@pytest.mark.dual`, `@pytest.mark.ptp`

Root required. Depends on `mfd-*` Intel-internal packages for SSH/NIC automation.

### RxTxApp (`tests/tools/RxTxApp/`)
Universal JSON-driven test vehicle. Max sessions: 180 video, 1024 audio, 180 ancillary, 180 fast-metadata (each direction).

JSON config directories: `loop_json/`, `audio_json/`, `native_af_xdp_json/`, `kernel_socket_json/`, `rss_json/`, `redundant_json/`

### Key Testing Gotchas
- DPDK can't reinit within a process → noctx pattern for isolated tests
- `cvt_test.cpp` is 4349 lines — add round-trip tests when adding new pixel formats
- Fuzz harnesses are single-packet — multi-packet protocol bugs uncovered
- `KahawaiUplTest` uses standard POSIX sockets (validates LD_PRELOAD interception)
- Test level: CI runs `mandatory`; `all` for thorough local validation
