# tests/unified_integration_tests/ — UnifiedKahawaiTest

Integration tests of the new API: a copy of the `KahawaiTest` harness, rewritten suite by suite
(`doc/unified-api/implementation-plan.md` §4.1). A ported case keeps its suite and case name, so
one `--gtest_filter` selects the twins in both binaries. The binary is
`build/tests/UnifiedKahawaiTest`; through MCP it is `run_gtest`, `run_noctx_tests` and
`run_noctx_pf_tests` with `binary=unified`, and `noctx/run.sh` and `run_pf.sh` take
`--api unified`.

Running, debugging, and interpreting these tests (CLI flags, suite map, pacing modes, failure
signatures, the one-process-per-case NoCtx rule), the same as for `KahawaiTest`:

@../../.github/instructions/mtl-gtest.instructions.md

Authoring a new case — tier picker and quality rules: invoke the `mtl-write-test` skill.
Running against real VFs is `mtl-system-admin`'s job, not this agent's.
