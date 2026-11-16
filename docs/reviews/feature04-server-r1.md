# Feature 04 server independent review, round 1

Reviewed source: `216f8b7fc6da4c05652e30de67ed4875f18c6be8` (PR 94).
Comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Reviewed source tree: `28cd51499fbd1da6342dc0f9f53f2306714747df`.

**Outcome: changes required; one P2 finding.** The installed standalone runner's
portable verifier fails because a new test imports an optional package during
discovery. The earlier preliminary clean source-review status was superseded
after inspecting exact-head CI and independently checking installation behavior.
This report is historical evidence for the exact source above; it does not clear
a later fix or head. An independent fresh review is required after the author fix.

## Findings

### P2 — New contract tests break standalone runner verification without the optional executor

Location: `coding_runner/test_native_contract.py:5–6`.

The new module imports `vibearr_executor.artifacts` and
`vibearr_executor.validation_profile` unconditionally. Both runner distributions
intentionally have no required dependencies; the executor is the separate
`native-runtime` extra. `vibearr_runner.verify.portable_suite()` discovers all
installed `coding_runner/test_*.py` modules even when that extra is absent.
Consequently a normal standalone runner installation now fails portable
verification with `ModuleNotFoundError: No module named 'vibearr_executor'`, before
these tests can be skipped. The pre-existing native adapter and validation tests
guard executor imports and skip the dependent cases in this supported state.

Reproduction: build and install the canonical or compatibility runner sdist into
a clean Python 3.12 environment without the native-runtime extra; from outside
the checkout run `python -I -m vibearr_runner.verify`. Existing acceptance test
`vibearr.test_distributions.DistributionArtifactTests.test_canonical_runner_sdist_installs_without_checkout`
performs this exact sequence. The author should retain standalone import and
verification behavior by guarding optional imports and executor-dependent tests,
then verify both distribution entrypoints with and without the extra.

Exact-head GitHub Actions independently shows both runner distribution cases
failing at this import in
[Tests run 43879225760](https://github.com/innateessence/nightly-queue/actions/runs/43879225760).
The local full Django suite skipped its four distribution tests because its
application tools environment lacks build tools; its passing result does not
cover this packaging regression.

Independent local reproduction used a separate clean build environment with
setuptools 80.9.0 and wheel 0.48.0, after application tests had finished. The
canonical runner sdist acceptance test failed as expected: the installed verifier
ran 582 discovered tests and reported one import error, with 53 optional-runtime
skips. The import traceback points to the new test's line 5 in the installed
site-packages directory, outside the checkout.

### P3 — HTTP null-filter validation precedes authoritative claim replay

Location: `runner_control/api.py:118–121`.

For a previously owned run ID, `claim_run()` returns the saved run before
validating the new project filter. The HTTP view instead rejects an explicit
`project_ids: null` before reaching that replay. A request containing an existing
owned run ID, an empty configuration revision, and that null filter returns 400;
the direct service replay returns the existing run. This is a small inconsistency
with the agreed replay-before-new-filter-validation contract. Omitted filters and
empty-list replay work, and this rejection does not release ownership or claim
another item. Keep this low priority; it is not the P2 blocker.

## Scope and method

Used a separate full clone at `/tmp/vibearr-review04-server-r1`, checked out the
exact reviewed source, and used a unique PostgreSQL database and test database
on the assigned development service. No original checkout, application data,
live container, provider write, or paid model invocation was used. No author
implementation edits or merges were performed. Only this report is committed.

Read the repository instructions, MVP and feature 04 plan, agreed server contract
and preparation notes, native 03 session/outcome contracts, validation 15 session,
fixed recipe and dependency provenance contracts, full topic changes and relevant
surrounding source. Review included context/plan/intent/result/workspace identity,
all nine exact-tree checks, version 2 publication, termination, retry/claim
fencing, controller and credential changes, source refresh and repository
authority, candidate filtering, rolling status, historical readback and retention.

The server separates structural native dispatch availability from actual host
readiness: `native_dispatch_available` is true while `execution_ready` remains
false. This review does not qualify a runtime or establish complete feature 04
controller integration or a real paid issue-to-PR acceptance result.

## Independent checks

| Check | Result |
| --- | --- |
| Full PostgreSQL-backed `manage.py test --noinput` | 1,628 tests, passed; four distribution tests skipped |
| Focused native server protocol and pure contracts | 56 tests passed, including PostgreSQL concurrency cases |
| Additional independent lifecycle checks | Four passed: failed-validation repair after terminal receipts; repair blocked by unresolved validation; late terminal reporting and completion while paused/canceled; stale old claim rejected after a newer attempt |
| Additional HTTP replay observation | One passed, confirming the P3 null-filter behavior above |
| Black, Ruff, mypy | Passed; mypy checked 340 files |
| Migration and system checks | Migration 0011 applied; no pending model changes; no system issues |
| Existing-data migration 0010 to 0011 | Existing run configuration/source/result fields and evidence preserved; new fields received additive empty/null defaults |
| Topic whitespace check | Passed |
| Clean installed canonical runner sdist acceptance | Failed as expected; independently reproduced the P2 optional-executor import error |

The additional lifecycle tests and migration experiment were temporary reviewer
files outside the repository. They did not change the candidate source. The
review used ordinary functional and concurrency checks, with no additional
adversarial, exploit, or network enforcement probes.

The source and tests provide good coverage of cancellation races, unknown
executor/publication fencing, current claim and controller proof, credential
version changes, frozen comment continuation, material instruction changes,
stable repository rename observations, selected GitHub preflight grants,
read-only reconciliation scope, monotonic status, and retention of newer attempt
snapshots. These results do not remove the standalone packaging blocker above.

## CI and remaining limits

At inspection the exact reviewed head had a failed `Tests / Run test.sh` check,
successful executor boundary and native image checks, and an in-progress
`Vibearr validation / Validate candidate` check. This report does not assert
exact-head CI success or authorize merging. Root owns serial merges after the
required independent review and exact-head checks.
