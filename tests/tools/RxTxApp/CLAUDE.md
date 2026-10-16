# tests/tools/RxTxApp/ — RxTxApp (legacy API, frozen)

Frozen while the legacy API exists (`doc/unified-api/implementation-plan.md` §4.1): no new JSON
kind or key, no rewrite, no deletion. New-API work goes into `tests/tools/UnifiedRxTxApp/`. The
only changes allowed here, each approved by the maintainer (in the autonomous run, as
`doc/unified-api/autonomous-run.md` §3 grants it): a build fix against libmtl, what a legacy
bugfix needs (D-24), or a bugfix in the framework (arguments, JSON parser, app core), never in
what a test checks. `mtl-reviewer` treats any other diff here as a BLOCKER.

C rules for any edit here:

@../../../.github/instructions/mtl-c-coding.instructions.md
