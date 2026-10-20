# Run status

| | |
|---|---|
| Status | The ledger of the autonomous implementation run ([autonomous-run.md](autonomous-run.md)). The orchestrator keeps it current and commits it by path while no lead runs (autonomous-run.md §5); a lead commits its own §3 and §4 rows, and a pending §2 row, with its first commit |
| Branch | `unified-api-impl` on `sakoram` |
| Base commit | `a6fc9093cb161769d3c506beb956f18321413f0b` (R0 started 2026-10-20), on `main` at `2fe5599f`; `lib/` and `include/` are unchanged since `770294a1` |

On every resume: read [autonomous-run.md](autonomous-run.md), this file, the current milestone's
ledger and `git log --oneline -20`, then continue (autonomous-run.md §7).

## 1. The host

The guard check and the `status_report` of `mtl-system-admin` before R0, every later change to the
host, and the known-red list of L0 (autonomous-run.md §1): the legacy cases red or skipped at the
base commit.

| Date | Check | Result |
|---|---|---|
| 2026-10-16 | preparation: tests at `770294a1` (the library is unchanged since) | `./build.sh` green after `build/` was moved back to the `/usr/local` prefix; `./build.sh unit` 959 passed, 1 skipped; KahawaiTest `St20p*` 44/44 on the E830 (`0000:15:01.0`/`.1`) and on the E810 (`0000:c9:01.0`/`.1`) |
| 2026-10-16 | preparation: host | patched ICE `Kahawai_2.6.7`; MtlManager of this tree running; the stale `st41_api.h` removed from `/usr/local/include/mtl` and `.local_install/mtl/include/mtl`; both install trees built from `770294a1` (`build-both.sh`) |
| 2026-10-20 | guard check at `b38e258c` (autonomous-run.md §9) | **failed; the run stopped before R0.** `env \| wc -l` ran instead of being blocked: `.github/claude/settings.local.json` is missing, so step 1 of §9 was not done (see below) |
| 2026-10-20 | guard check repeated, after the maintainer asked for a restart | **passed.** The candidate settings are installed and loaded live: `env \| wc -l`, a subagent's `git push origin x` and a Write into the hooks directory are blocked; `git status`, a `for` loop, `sudo -n true` and `hugepages_get` run |
| 2026-10-20 | `test-permissions.sh` (its log is in the run's `logs/`) | 10–16 denied as expected. Of 1–9, four are denied under `dontAsk`: an `&&` compound, a `for` loop, a `$(…)` substitution, a Write under `build/`. Such calls are rephrased (autonomous-run.md §5 "A denied call") |
| 2026-10-20 | session mode | the orchestrator's session kept bypass permissions mode (it was not relaunched): the deny rules and the guard hook hold, the allow list does not restrict it |
| 2026-10-20 | host before R0 (`mtl-system-admin`, read-only) | ICE `Kahawai_2.6.7`; 1024 × 2 MB hugepages on each node; 12 VFs per NIC on vfio-pci; MtlManager running; `ptp4l`/`phc2sys` not running. The installed DPDK is `26.03.91`, `versions.env` pins `26.07`: the preparation baseline was green on it, kept as is |
| 2026-10-20 | L0: the queue-rate check of D-253 (autonomous-run.md §8), E830 `0000:15:01.0`/`.1`, binaries of `770294a1` | **a VF re-create lifts a kept rate; the host keeps DPDK `26.03.91`.** TSC red at 27.7 fps after the RL kill, green at 50.06 and 60.04 fps after `nic_create_vf` on `0000:15:00.0` (see below) |

The guard check of 2026-10-20: without `settings.local.json` the session ran in bypass
permissions mode, not `dontAsk`, and the `PreToolUse` guard (`.git/autonomous-run/bash-guard.py`,
present) was not enabled. The git hooks `commit-msg`, `pre-commit`, `pre-merge-commit` and
`pre-push` are installed. The probes of a subagent's `git push origin x` and a Write to
`.git/hooks/x` were not run, because with the guards off they could take effect. To restart, copy
`/home/labrat/wip/unified-api-impl/settings/settings.local.candidate.json` to
`.github/claude/settings.local.json`, run `test-permissions.sh`, and start `claude` again with the
orchestrator prompt of autonomous-run.md §9.

The queue-rate check of L0: kills of the RL run at 15 s and at 9 s left only the rates its own
sessions had trained (q1 2.04, q2 2.45 Gbit/s), which the TSC run of the same case fits under, so
that run stayed green. Killed right after both ports logged `tx pacing ratelimit`, with every TX
queue still on the init shaper (134217728 B/s, by
`/home/labrat/wip/unified-api-impl/l0/rl-kill-after-init.sh`), the TSC run was red at 27.7 fps of
50 and 59.94 (1958 Mb/s). After `nic_create_vf` on `0000:15:00.0` (6 VFs) it was green at 50.06 and
60.04 fps (3912 Mb/s). The logs are `/home/labrat/wip/unified-api-impl/logs/l0-qr-*.log`.

## 2. Milestones

States: todo, in progress, exit review, done, done with exceptions (autonomous-run.md §4). The
agent column holds the ID of the running lead, for SendMessage on resume. A milestone starts when
the one in Needs is done or done with exceptions.

| Milestone | Ledger | Needs | State | Lead agent | Exit commit | Exit evidence |
|---|---|---|---|---|---|---|
| R0 (re-pin to the new base, autonomous-run.md §1) | this file | — | done | `a382f8ddd6f259ae0` | `e3bb41c2` | 10 docs commits `1f600403..e3bb41c2`; `sketch/check.sh` OK; `mtl-reviewer` (Opus) over the range: 3 MAJOR and 12 MINOR fixed, then APPROVE (`wip/unified-api-impl/r0/review-r0.md`); D-246–D-253 in §3 |
| L0 (the legacy baseline, autonomous-run.md §1) | this file, §1 | R0 | in progress | `a1d4fafb65f9a1f51` | | |
| MS1 | [ms1-status.md](ms1-status.md) | L0 | todo | | | |
| MS2a | ms2a-status.md | MS1 | todo | | | |
| MS2b | ms2b-status.md | MS2a | todo | | | |
| MS3 | ms3-status.md | MS2b | todo | | | |
| MS4a1 | ms4a1-status.md | MS3 | todo | | | |
| MS4a2 | ms4a2-status.md | MS3 (run after MS4a1, plan §1.2) | todo | | | |
| MS4b | ms4b-status.md | MS4a2 | todo | | | |
| MS5 | ms5-status.md | MS4b | todo | | | |
| MS6 | ms6-status.md | MS5 | todo | | | |
| MS7 | ms7-status.md | MS6 | todo | | | |
| Kubernetes track (EK1–EK19) | its rows in the ledgers of MS1–MS3 (autonomous-run.md §2) | beside MS1–MS3 | todo | | | |

## 3. Decisions taken in the run

Every question answered and every plan bug fixed by autonomous-run.md §4, for the maintainer to
confirm. One row each; the D-row holds the decision itself.

| Date | Milestone, task | Question or bug | D-row | Evidence (test, model, review file) |
|---|---|---|---|---|
| 2026-10-20 | R0 | WAKE: what the legacy pipeline wait keeps after main's wake fixes (LB-14's `kick` was undefined; how RB0–RB2 keep main's blocking unit cases green) | D-246 | |
| 2026-10-20, continued | | | | `/home/labrat/wip/unified-api-impl/r0/design/wake/verdict.md`; model `design/models/legacy_wake.py` (`/home/labrat/wip/unified-api-impl/r0/design/wake/judge/legacy_wake.out`, `judge/break_wk_a.out`); `UnitTest --gtest_filter='*St20PipelineRxBlocking*:*St20PipelineTxBlocking*:*PipelineTxWakeBlock*'`: 30 passed |
| 2026-10-20 | R0 | E1a and REPEAT on main's `tv_frame_free_cb` (`8c9d48f3e`): E1a's scope, the binding's naming, SF-39's and SF-18's owners, the PR #1770 conditions | D-248 | |
| 2026-10-20, continued | | | | `/home/labrat/wip/unified-api-impl/r0/design/e1a/verdict.md` (with problem.md, e1-a.md, e1-b.md, e1-c.md, judge/); model `design/models/tx_completion.py` (`judge/tx_completion.out`); `UnitTest --gtest_filter='*St20TxExtFrameDoneTest*:*St20TxTaskletPending*'`: 17 passed |
| 2026-10-20 | R0, question SMALL (DQ3) | the kernel-socket TX queue swap after `8224aba52`: TOS, TTL and the source port come from the socket, multicast leaves with TTL 1, the NACK flows go stale | D-249 | `r0/design/small/verdict.md`; `sm-c-dq3_socket.py`, `sm-b/kernel_socket_mcast_ttl.py`; `MtDpSocketTxTest.GsoPacketsGoToTheirHeaderDestination` |
| 2026-10-20 | R0, question SMALL (DQ4) | non-temporal RX stores (`1147e8b30`) and a publication or hand-back outside the three fenced notifies | D-250 | `r0/design/small/verdict.md`; `sm-a/check_rx_fence.py`, `sm-c-dq4_fence_check.py`, `sm-b/nt_publish_model.py` |
| 2026-10-20 | R0, question SMALL (DQ5) | the bounded DMA busy-poll on the RX tasklet (`f64db786a`), per newer RTP timestamp | D-251 | `r0/design/small/verdict.md`; `sm-c-dq5_flush_model.py`; `St20RxDmaTest.*`, `St20RxDmaRedundantTest.*` |
| 2026-10-20 | R0, question SMALL (DQ6) | EK4/EK5 partly on main; the exit unlink orphans a second manager, every pytest session orphans the run's; main pins `0666` | D-252 | `r0/design/small/verdict.md`; `sm-a/manager_lock_model.py`, `sm-c-dq6_manager_model.py`; `MtInstanceManagerGoneTest.*` |
| 2026-10-20 | R0, question SMALL (DQ7) | the TSC-init queue-rate clear is compiled out on this host's DPDK 26.03.91 | D-253 | `r0/design/small/verdict.md`; `sm-c-dq7_check.py`; `MtDevRlTeardownTest.*` |
| 2026-10-20 | R0 | FM: fast metadata after `88d47ac83` (a mode of the ST40 sessions): where its binding and engine work live, the multi-packet TX unit, PE8, the lost acceptance case, who filters DIT/K, migration §4.12 | D-247 | `/home/labrat/wip/unified-api-impl/r0/design/fm/verdict.md`; |
| 2026-10-20, continued | | | | models `design/models/fastmeta_rx.py`, `fastmeta_tx.py` (`/home/labrat/wip/unified-api-impl/r0/design/fm/judge/rx_judge.out`, `judge/tx_judge.out`, `judge/files_pick.out`, `judge/check_added_cases.out`); `check.sh` with every edit (`judge/check-sh-all-edits.out`); `UnitTest --gtest_filter='St40*'`: 256 passed |

## 4. Blocked outside the run

Items that need a person or a release outside this machine (autonomous-run.md §3), and what the
run prepared for each.

| Item | Needed by | Prepared | What the maintainer must do |
|---|---|---|---|
| R0: the guard denied a Bash call ("never print environment variables"), a Python heredoc that tagged `final_model.py`'s flake8 findings with `noqa`, to add D-246's labels LW1, LW2 | M0 | `final_model.py` unchanged; design/wait-tests.md §1 gives the labels to M0; `/home/labrat/wip/unified-api-impl/denied/r0-final-model-noqa.md` | review the guard's pattern |
| The Fuzz Tests job (`fuzz_tests.yml`): a clang + ASan build of `libmtl` and the harnesses, each target fuzzed, on every pull request that changes `lib/`, `include/` or `tests/fuzz/` | the pull requests of the run's milestones | | |
| The Fuzz Tests job, continued | | this host has clang 18 only, which cannot build `lib/` (T-115), and the branch runs no CI; the run's evidence is the unit tier's corpus replays. If a clang that builds `lib/` is installed under `~/.local` (autonomous-run.md §3), N5 quotes its local run and this row records it | |
| The Fuzz Tests job, continued | | | run the job on each pull request; check that MS3's `local: *` kept the harnesses' coverage (abi.md §6, stage A) |

## 5. Final report

Written at the end of the run: what is implemented, the test evidence per milestone, the
decisions of §3 to confirm, the items of §4, and the known gaps.
