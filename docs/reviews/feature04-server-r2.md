# Feature 04 server independent review, round 2

Reviewed source: `da3aec619a5b00983e520369ba408669e9fceb21` (PR 94).
Comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Reviewed source tree: `7e0c8fdc9da3c0b50dbe8d5bc69e7e10301171af`.

**Outcome: no P2-or-higher findings.** This is a fresh review of the complete
server topic after the round 1 packaging correction. The historical round 1 P2
is independently verified fixed in both actual standalone runner distributions.
This review clears the reviewed server source within the limits below; it does
not establish full feature 04 integration or authorize a merge.

## Findings and prior correction

No new P2-or-higher defect was found in the complete topic source or independent
functional checks.

The round 1 optional-executor import defect is fixed. Executor imports in
`coding_runner/test_native_contract.py` are conditional; ten SDK-dependent tests
skip cleanly when the optional package is absent, while six portable tests still
execute. The production contract module imports without the SDK and returns an
actionable `NativeContractError` naming `vibearr-runner[native-runtime]` when an
executor-backed helper needs it. Canonical and compatibility imports resolve to
the same module. This was tested using real installed artifacts outside the
checkout, rather than relying only on mocked imports or the source test suite.

The round 1 P3 remains a nonblocking contract inconsistency:
`runner_control/api.py:118` rejects explicit `project_ids: null` before an owned
run-ID replay reaches the service's authoritative replay. Omitted filters and
empty lists retain their supported behavior. The rejection does not release an
existing claim or create duplicate work; it is not elevated to a P2 finding.

## Scope and method

Used a fresh full clone at `/tmp/vibearr-review04-server-r2`, checked out the exact
reviewed source, and reviewed all 26 changed files plus relevant surrounding
implementations. Read repository instructions, feature 04 and MVP requirements,
the agreed server contract/preparation notes, native 03 session/outcome contracts,
and validation 15 fixed-profile/session/provenance contracts.

The review traced strict immutable context and plan binding; native intent,
agent receipt and inspected workspace relationships; all nine fixed checks and
their exact committed tree/source; dependency-build provenance; publication and
version 2 delivery; original deadlines and bounded repairs; exact termination
coverage; current claim/controller, project policy and selected account/version
fences; cancellation and delayed factual reporting; retry ownership; initial
freshness and frozen continuation; stable repository identity/rename authority;
candidate selection and project filters; scoped GitHub preflight/reconciliation;
rolling status; and historical readback and retention of newer/shared snapshots.

Only ordinary source, functional, concurrency, packaging and migration checks
were performed. A unique reviewer-owned database and test database were used on
the assigned development PostgreSQL service. No live application data, provider
write, paid model invocation, adversarial/exploit probe, author implementation
edit, or merge was performed. Temporary supplemental checks remained outside the
repository; only this report is committed.

## Independent validation

| Check | Result |
| --- | --- |
| Full PostgreSQL-backed `manage.py test --noinput` | 1,629 tests passed in 55.587 seconds; four distribution tests skipped because the application tools environment lacks setuptools/wheel |
| Actual clean sdist → wheel → installed artifact tests | All four `vibearr.test_distributions` cases passed in 71.984 seconds in a separate reviewer-created Python 3.12 venv with setuptools 80.9.0 and wheel 0.48.0; canonical runner, compatibility runner, CLI, executor all covered |
| Actual installed bare runner, outside checkout with `-I` | Both canonical and compatibility portable verifiers passed; each ran 597 tests with 63 optional-runtime skips; native contract tests ran 16 with exactly ten SDK-dependent skips |
| Actual absent-SDK production behavior | Canonical/compatibility contract identity, installed module path, portable status, SDK absence, no Django import, and actionable missing-SDK exception all passed |
| Actual installed SDK production path | After installing the separately built executor wheel, all 16 native contract tests passed without skips; both canonical and compatibility portable verifiers passed all 597 tests without skips |
| Independent ordinary lifecycle checks | Eight passed; details below |
| Black and Ruff | Passed; Black checked 340 files |
| Mypy | Passed on 340 source files; existing untyped-body notes only |
| Migration/system checks | All migrations including 0011 applied on own database; no migration drift; no system issues |
| Existing-data migration 0010 → 0011 | Existing configuration/source snapshots, result fields, finish digest and evidence preserved; new native status/outcome received empty defaults and timestamps remained null |
| Topic whitespace and working tree | `git diff --check` passed; candidate implementation remained unchanged |

The eight independent functional cases confirmed:

- Late agent results, exact termination and canceled completion remain reportable
  after cancellation, global disable/pause and selected model credential revocation.
- Pause rejects a new dispatch while retaining the active slot and claim.
- Stale status leaves heartbeat and repair accounting unchanged.
- Conflicting content at the same status sequence cannot replace the saved state.
- A workspace with a different stable repository ID is rejected.
- A truncated protected check cannot claim passing validation.
- A mismatched terminal outcome retains the active claim and slot.
- Replaying an existing immutable metadata operation survives account rotation
  without registering a second dispatch identity.

The source suite also executed the PostgreSQL duplicate-dispatch, retry/dispatch,
cancel/dispatch and controller-proof races. The four source-suite skips were
covered by the separate actual clean-artifact run, so the source-suite success is
not being used as packaging evidence.

## CI and remaining limits

All four required checks passed on the exact reviewed source head:
[Native Codex image](https://github.com/innateessence/nightly-queue/actions/runs/43883328591)
and [Executor boundary](https://github.com/innateessence/nightly-queue/actions/runs/43883328616),
[Tests](https://github.com/innateessence/nightly-queue/actions/runs/43883328648), and
[Vibearr validation](https://github.com/innateessence/nightly-queue/actions/runs/43883328876).
Root must also inspect all required checks at the exact report head before any
serial merge.

The server correctly keeps `execution_ready` false and reports structural
`native_dispatch_available` separately from supervisor-required host readiness.
Server fixtures and receipt binding do not prove an actual model session,
executor operation, pause/deadline/purge deployment, GitHub mutation or complete
issue-to-PR recovery loop. The feature 04 parent owns that integration and live
acceptance evidence; this server review does not substitute for it.
