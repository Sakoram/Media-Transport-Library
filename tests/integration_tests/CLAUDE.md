# tests/integration_tests/ — KahawaiTest

Frozen while the legacy API exists (`doc/unified-api/implementation-plan.md` §4.1): no new case,
no rewrite, no deletion. New-API cases go into `tests/unified_integration_tests/`
(`UnifiedKahawaiTest`). The only changes allowed here, each approved by the maintainer (in the
autonomous run, as `doc/unified-api/autonomous-run.md` §3 grants it): the `--api` run option of
`noctx/run.sh` and `run_pf.sh`, a build fix against libmtl, what a legacy bugfix needs (D-24), or
a harness bugfix (a crash, hang or flake in the harness or `test_util.*`), never in what a case
asserts. `mtl-reviewer` treats any other diff here as a BLOCKER.

Running, debugging, and interpreting these tests (binaries, CLI flags, suite map, pacing modes,
failure signatures, the one-process-per-case NoCtx rule):

@../../.github/instructions/mtl-gtest.instructions.md

Authoring a new case — tier picker and quality rules: invoke the `mtl-write-test` skill.
Running against real VFs is `mtl-system-admin`'s job, not this agent's.
