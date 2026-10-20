# Design models

The models in [models/](models/) are the exhaustive evidence behind the decisions they name. Each
needs only the Python 3 standard library (`stress.c`: a C compiler and Linux) and explores every
interleaving under sequential consistency. Task M0 makes `run_models.py` the one entry point of
the model job, with each model's printed table as its expected result
([implementation-plan.md](../implementation-plan.md) §8.2, "The wait evidence"); C1w copies the
model files into `tests/unit/core/wait_model/`. The wait protocol's cases, mutants and pause-hook
tests are [wait-tests.md](wait-tests.md).

| File | What it checks | Decision | Copied by | Run (from `models/`) |
|---|---|---|---|---|
| [final_model.py](models/final_model.py) | the wait protocol of [wait-protocol.md](../wait-protocol.md): the object path, the queue path and posts, with 23 mutants ([wait-tests.md](wait-tests.md) §1) | D-158–D-169; G-52, G-95, G-142 | C1w, C1q1, C1q2 | `python3 -I final_model.py [CASE ...]`; exit 1 unless every case and mutant gives its result (about 6 s) |
| [wait_flush.py](models/wait_flush.py) | the count-bounded flush of one library loop and its sleep check | D-142 | C1w | `python3 -I wait_flush.py`; the design cases print LOST=0, the "return ignored" variant LOST > 0 (under 1 s) |
| [stress.c](models/stress.c) | random-yield stress on the real kernel: a nightly smoke test, not evidence | D-158–D-169 | C1w (nightly job) | `cc -O2 -pthread stress.c -o stress; ./stress ROUNDS [1]` |
| [provide_model.py](models/provide_model.py) | the `mtl_rx_provide` hand-back and its ledger rule ([contract.md](../contract.md) §9.12) | D-152; G-137 | MS2b, into `tests/unit/core/` | `python3 -I provide_model.py` (about 5 min) |
| [ring_bound.py](models/ring_bound.py) | the smallest results-ring bound per reap discipline, P = 1…5, with and without the hold | D-208; G-144 | M0, into `run_models.py` | `python3 -I ring_bound.py`; exit 1 unless each discipline gives its expected bound: D-208's with the hold, and none within the cap without it (under 1 s) |
| [pin_ring.py](models/pin_ring.py) | the hold and the REPEAT pin rule at the level of whole units: ring, deadlock and EOS for D-212 (a `BEHIND` drop or a short unit releases its parity's pin), rejected rules and mutants; not the repeat uses or the pin bit | D-209, D-212; G-164 | M0, into `run_models.py` | `python3 -I pin_ring.py`; exit 1 unless every row gives its expected result (about 10 s) |
| [trace_d207.py](models/trace_d207.py) | the shortest run in which D-207's reap-then-acquire meets a full ring once a unit completes out of order without the hold | D-209 (why the hold) | M0, into `run_models.py` | `python3 -I trace_d207.py`; prints the 11-step run, exit 1 if there is none |
| [w_noe2.py](models/w_noe2.py) | the stores that must run E2: a W publish, with a RESULTS reaper and with an ACQUIRE waiter beside a second acquirer, and a HELD → FREE pin release with results off; each store without E2 loses a wake-up, the rule does not | D-209, D-212; G-143 | M0, into `run_models.py` | `python3 -I w_noe2.py`; exit 1 unless every skip is LOST and every rule is `ok` (under 1 s) |
| [legacy_wake.py](models/legacy_wake.py) | the legacy wake modes LW1–LW2 of a re-based pipeline (DP, then WT) against a model of today's st20p `BLOCK_GET` wait: 40 cases, every result among today's, the main unit cases' properties on both, the mutants of every step ([wait-tests.md](wait-tests.md) §1.2) | D-246; LB-14, LB-15 | RB0, into `run_models.py` | |
| legacy_wake.py, continued | | | | `python3 -I legacy_wake.py`; exit 1 unless every case and mutant gives its result (under 1 s); `--any-deadline`, `--wt-only` |
| [tx_completion.py](models/tx_completion.py) | TX video frame completion on main's `tv_frame_free_cb` with E1a: two frames, three units, one or two legs, recovery step by step against foreign frees, legacy and core sessions, teardown mid-frame, the MS2b re-bind; mutants of the verdict order, the builder's reference and MF1's claim | D-248, D-123; SF-39 | E1a, into `run_models.py` | |
| tx_completion.py, continued | | | | `python3 -I tx_completion.py`; exit 1 unless every case and mutant gives its result (about 11 s) |
| [fastmeta_rx.py](models/fastmeta_rx.py) | a fast metadata unit sink on the ST40 RX dedup of `a6fc9093` (ported): today's "next timestamp" text, "every leg passed" and D-247's rule (a newer timestamp as the marker, the two-leg gap wait, two units in flight, the late check), over every loss pattern and merge of two legs, a reorder inside a leg, a dead leg, | | | |
| fastmeta_rx.py, continued | a sender restart and the sequence wrap; two mutants | D-247; G-167 | N3, into `run_models.py` | `python3 -I fastmeta_rx.py`; exit 1 unless D-247's rule holds everywhere and the old text, the naive late check and the mutants fail (about 15 s) |
| [fastmeta_tx.py](models/fastmeta_tx.py) | ST 2110-41 §7 with the checker of `fmd_build_packet_test.cpp` (ported): multi-packet units on a per-unit and a carried schedule, the receiver's due time, and one packet per unit | D-247; G-167 | N4f, into `run_models.py` | |
| fastmeta_tx.py, continued | | | | `python3 -I fastmeta_tx.py`; exit 1 unless the per-unit reading breaks §7, the carried one holds and a 64 KiB unit is cut by the due time, and one packet per unit passes (about 1 s) |
