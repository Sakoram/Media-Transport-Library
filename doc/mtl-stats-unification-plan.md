# Plan: Unify MTL Session Statistics via C Struct Inheritance

## TL;DR
MTL session stats have significant inconsistencies across media types: type mismatches (`int` vs `uint64_t`), duplicated fields, missing counters (video per-port OOO), and ST41 TX containing RX-domain fields. Fix by deepening the existing C struct embedding ("inheritance") pattern with intermediate layers and enforcing consistent types.

## Catalogued Inconsistencies

### Type Mismatches
| Field | Where | Type Used | Expected |
|-------|-------|-----------|----------|
| `stat_pkts_wrong_interlace_dropped` | ST40 RX `st40_rx_user_stats` | `int` | `uint64_t` |
| `stat_interlace_first_field` | ST41 RX `st41_rx_user_stats` | `uint32_t` | `uint64_t` |
| `stat_interlace_second_field` | ST41 RX `st41_rx_user_stats` | `uint32_t` | `uint64_t` |
| `stat_pkts_wrong_interlace_dropped` | ST41 RX `st41_rx_user_stats` | `int` | `uint64_t` |

### Missing Counters
| Counter | ST20 | ST30 | ST40 | ST41 |
|---------|------|------|------|------|
| RX `port[].out_of_order_packets` | **NEVER SET** | ✓ | ✓ | ✓ |

### Duplicated / Wrong-Domain Fields
- ST41 **TX** stats contain RX-domain fields: `stat_pkts_redundant`, `stat_pkts_out_of_order`, `stat_pkts_wrong_ssrc_dropped`, `stat_pkts_wrong_pt_dropped`, `stat_pkts_received`, `stat_pkts_enqueue_fail` — these belong in RX, not TX

### Inconsistent Naming
| Concept | ST20 | ST40/ST41 |
|---------|------|-----------|
| Notification latency | `stat_max_notify_frame_us` | `stat_max_notify_rtp_us` |

### Inconsistent Exposure
- `stat_last_time`: exposed in ST41 public API, internal-only in ST20/ST30/ST40
- `stat_max_notify_rtp_us`: exposed in ST41, internal-only elsewhere

### Shared Concepts Not in Common Base
- Interlace tracking (ST20, ST40, ST41 — not ST30)
- Redundancy tracking (`stat_pkts_redundant`: ST30, ST40, ST41 RX — not ST20 RX base, though ST20 has `stat_pkts_redundant_dropped`)

## Steps

### Phase 1: Fix Type Mismatches (no API behavior change, ABI break)
1. In `include/st40_api.h`: change `st40_rx_user_stats.stat_pkts_wrong_interlace_dropped` from `int` → `uint64_t`
2. In `include/st41_api.h`: change `st41_rx_user_stats.stat_interlace_first_field` and `stat_interlace_second_field` from `uint32_t` → `uint64_t`
3. In `include/st41_api.h`: change `st41_rx_user_stats.stat_pkts_wrong_interlace_dropped` from `int` → `uint64_t`
4. Verify internal impl structs in `lib/src/st2110/st_header.h` match (internal may stay `uint32_t` since `ST_SESSION_STAT_INC` duals-increments — but public API types must be `uint64_t`)

### Phase 2: Add Missing Per-Port OOO Counter for Video
5. In `lib/src/st2110/st_rx_video_session.c`: wherever `ST_SESSION_STAT_INC(s, port_user_stats.common, stat_pkts_out_of_order)` is called, also increment `s->port_user_stats.common.port[s_port].out_of_order_packets` — *depends on step 1 being done first to avoid confusion*
6. Verify the `s_port` variable is available in all 4 OOO detection sites in st_rx_video_session.c (lines ~1663, ~1852, ~2045, ~2209)

### Phase 3: Introduce Interlace Stats Mixin (C struct inheritance layer)
7. Create a new struct in `include/st_api.h`:
   ```c
   struct st_rx_interlace_stats {
     uint64_t stat_interlace_first_field;
     uint64_t stat_interlace_second_field;
     uint64_t stat_pkts_wrong_interlace_dropped;
   };
   ```
8. Similarly for TX:
   ```c
   struct st_tx_interlace_stats {
     uint64_t stat_interlace_first_field;
     uint64_t stat_interlace_second_field;
   };
   ```
9. Replace the individual interlace fields in `st20_rx_user_stats`, `st40_rx_user_stats`, `st41_rx_user_stats` with `struct st_rx_interlace_stats interlace` — *parallel with step 7*
10. Same for TX: `st20_tx_user_stats`, `st40_tx_user_stats`, `st41_tx_user_stats` → embed `struct st_tx_interlace_stats interlace`
11. Update all `ST_SESSION_STAT_INC` call sites referencing interlace fields to use the new `.interlace.` path
12. Update all stat logging functions to reference `.interlace.` path

### Phase 4: Fix ST41 TX Wrong-Domain Fields
13. Remove RX-domain fields from `st41_tx_user_stats`: `stat_pkts_redundant`, `stat_pkts_out_of_order`, `stat_pkts_enqueue_fail`, `stat_pkts_wrong_pt_dropped`, `stat_pkts_wrong_ssrc_dropped`, `stat_pkts_received`, `stat_last_time`, `stat_max_notify_rtp_us`
14. If any of these are genuinely used in the TX path (check `st_tx_fastmetadata_session.c`), move them to a properly named field or the correct RX struct instead
15. Move `stat_pkts_wrong_interlace_dropped` to the `struct st_tx_interlace_stats` mixin if truly used in TX

### Phase 5: Add Redundancy Stats Mixin (optional, lower priority)
16. Create `struct st_rx_redundancy_stats { uint64_t stat_pkts_redundant; }` in `st_api.h`
17. Embed in ST30, ST40, ST41 RX stats — *parallel with step 16*
18. For ST20, keep `stat_pkts_redundant_dropped` as video-specific (different semantics)

### Phase 6: Standardize Naming
19. Rename `stat_max_notify_rtp_us` → `stat_max_notify_frame_us` in ST41 for consistency with ST20 — or choose one name and update all

## Relevant Files

**Public API headers (struct definitions):**
- `include/st_api.h` — `st_rx_port_stats`, `st_rx_user_stats`, `st_tx_port_stats`, `st_tx_user_stats` (add mixin structs here)
- `include/st20_api.h` — `st20_rx_user_stats`, `st20_tx_user_stats`
- `include/st30_api.h` — `st30_rx_user_stats`, `st30_tx_user_stats`
- `include/st40_api.h` — `st40_rx_user_stats`, `st40_tx_user_stats` (type fixes)
- `include/st41_api.h` — `st41_rx_user_stats`, `st41_tx_user_stats` (type fixes, remove wrong-domain fields)

**Internal implementation (counter increments):**
- `lib/src/st2110/st_header.h` — `ST_SESSION_STAT_INC` macro, internal session impl structs
- `lib/src/st2110/st_rx_video_session.c` — add per-port OOO counter, update interlace refs
- `lib/src/st2110/st_rx_audio_session.c` — update interlace refs
- `lib/src/st2110/st_rx_ancillary_session.c` — update interlace refs
- `lib/src/st2110/st_rx_fastmetadata_session.c` — update interlace refs
- `lib/src/st2110/st_tx_video_session.c` — update interlace refs
- `lib/src/st2110/st_tx_audio_session.c` — no interlace
- `lib/src/st2110/st_tx_ancillary_session.c` — update interlace refs
- `lib/src/st2110/st_tx_fastmetadata_session.c` — update interlace refs, remove wrong-domain counters

**Tests:**
- `tests/` — any tests using stats structs need field path updates

## Verification
1. `./build.sh` — must compile cleanly (all struct field references updated)
2. `./format-coding.sh` — formatting compliance
3. `grep -rn 'stat_interlace_first_field\|stat_interlace_second_field\|stat_pkts_wrong_interlace' lib/ include/` — verify no stale references remain
4. `grep -rn 'out_of_order_packets' lib/src/st2110/st_rx_video_session.c` — verify per-port counter is now incremented
5. Run existing gtests: `cd build && meson test` — no regressions
6. Manual: check periodic stat dumps still print correctly for all session types

## Decisions
- **ABI break is accepted** — type changes (`int` → `uint64_t`) and struct layout changes (adding mixin) break ABI. This should be done in a major version bump or behind a version guard.
- **Struct embedding is the C "inheritance" mechanism** — MTL already uses this pattern (`struct st_rx_user_stats common`). We extend it with interlace/redundancy mixins.
- **ST41 TX wrong-domain fields**: need to verify if any TX code path actually uses them before removing. If so, they may need to stay but be renamed/documented.
- **Phase 3-5 are breaking API changes** — Phase 1-2 are bug fixes with minimal disruption.

## Further Considerations
1. **Versioning**: Phase 1-2 are safe bug fixes. Phases 3-5 break API/ABI — consider shipping them together in a single API bump. Recommendation: Phase 1-2 first as a standalone PR, then Phase 3-5 as a second PR.
2. **`ST_SESSION_STAT_INC` macro vs direct access**: The dual-increment pattern (internal + API struct) adds complexity. Consider removing the internal duplicate counters and having stat logging read directly from `port_user_stats` — this would halve the stat-related fields in impl structs. But this is a separate refactor.
3. **`stat_snapshot` for delta logging**: ST30 TX has `stat_snapshot` for computing deltas between stat dumps. ST20 TX also has it. Consider standardizing this pattern or removing it where unused.
