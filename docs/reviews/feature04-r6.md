# Feature 04 independent review, round 6

Reviewed source: `7a6bb025cd896b35a02878a2e7068f03030391b5` (PR 100).
Comparison base: accepted main `f97bce6d1c2d6f3824638fb52c15ee6c1b028e21`.
Reviewed source tree: `bab919035758ce38c0b05b3ca55cb10941700528`.

**Outcome: no P0, P1 or P2 finding established.** This is a fresh independent
review of the complete feature after the R5 read-attempt accounting correction,
including the earlier server and executor support. Historical reviews supplied
context, not inherited clearance. Root's own review and exact-head required CI
remain separate merge gates.

## Full review scope

Used a new full clone at `/tmp/vibearr-review04-r6`. Read `AGENTS.md`, feature 04
in `docs/autonomous-ai-queue-plan.md`, `docs/vibearr-mvp.md`, native queue/runtime
documentation, supporting reports and the entire feature diff against accepted
main. Initially fetched the exact immutable source from the author's committed
clone without executing there; later fetched GitHub and verified that PR 100's
published branch had that same commit.

Reviewed normal controller source-candidate selection and atomic claims; live
selected-account preflight and idle-probe avoidance; frozen issue, discussion,
checkout, context, plan and prompt; stable repository identity; current policy,
credential version, source, runtime and profile authority; qualified native
session preparation and same-operation reconciliation; verified export/import
and recoverable trusted Git commits; all nine fixed independent exact-tree
checks and owned derived-dependency provenance; two-repair limit and original
stage/task deadlines; push and non-draft PR publication; server version 2 receipt
relationships; protected termination before finish/claim release; pause, cancel,
retry, ownership loss and recovery; persistent executor instance and operation
presence; build controls, selective retention and tombstones; optional SDK and
standalone package boundaries; and minimum run/context/plan visibility.

The normal path uses native context/plan artifacts without command approvals.
Publication is PR-only; the feature does not claim the future CI/merge lifecycle.
The server preserves historical version 1 readback while requiring the native
version 2 exact session/workspace/profile/check/publication chain for success.
Reporting and read-only reconciliation remain possible after pause/cancel, while
unknown executor or publication outcomes retain ownership.

## Independent reassessment of the earlier findings

R1's direct GitHub read failures now use bounded durable three-attempt records,
backoff and rate-limit times. Permanent read errors and unknown mutations remain
distinct. Git-equipped tests are guarded for the Git-free app environment, with
real portable/Git verification separately exercised below.

R2's native clone path binds exact argv, repository and owned destination before
the attempt. Restart recovers tracked processes before removing a registered
partial checkout; completed clone recovery reuses it. An unregistered existing
checkout is fenced. The bounded read record survives retry/restart.

R3's `push_once` performs the original non-forced exact-commit mutation without a
redundant remote read inside the dispatched section. Readback remains separate.
R4's dispatch marker now passes through the actual Git transport to the tracked
worker gate: the worker PID is durable, current execution controls/deadline are
checked before the callback persists dispatch, and only then is exec released.
Known rejected workers are killed/reaped; post-release uncertainty is retained.

R5's read counter now reserves durably before execution and passes a scoped
attempt only to actual Git read transport. A positive known-no-launch callback
refunds that reservation; a launched or ambiguous attempt cannot be refunded.
There is no blanket refund for `BackendInterrupted`, `APIError`, unclassified
errors or crashes. HTTP reads retain conservative accounting without positive
no-launch proof. Postlaunch control/API failures and crashes retain reservations.

Independently repeated R5's full lifecycle reproduction in
`/tmp/vibearr-review04-r6-read-controls.py`, using a real local checkout/import/
candidate commit, prior fixture agent and all nine validation operations,
`NativeDelivery`, `SupervisorGit`, and actual `processes.run`. Four ordinary
pauses were introduced at the read's prelaunch checkpoint; a spawn observer
confirmed no Git read process started. Reloading the journal each cycle produced:

```json
{"attempts": [[0], [0], [0], [0]], "counts": {"entries": 4, "spawns": 0}, "executor_operations": 10, "finishes": [], "results": ["wait", "wait", "wait", "wait"]}
```

The actual tracked-worker tests also cover late blocked-child rejection,
prelaunch pause/cancel/deadline/API errors, durable dispatch before release,
postlaunch pause/API errors, a prior real DNS failure, unknown mutation readback
and crash-retained read reservations. A real queue-client POST HTTP 503 is an
ambiguous transport outcome handled by controller suspension/reconciliation;
generic `APIError` mocks do not establish a contrary production failure.

## Independent validation

All checks below ran in the reviewer's full clone at the reviewed source.

| Check | Result |
| --- | --- |
| Full PostgreSQL `manage.py test --noinput` | 1,776 tests in 65.620 seconds, OK; four distribution tests explicitly skipped because shared app tools lack build packages |
| Actual distribution artifact tests | All four passed in 71.088 seconds in an own Python 3.12 venv with setuptools 80.9.0 and wheel 0.48.0; actual sdist, wheel and isolated install paths for executor, canonical/compatibility runner and CLI |
| Full `python -m vibearr_runner.verify` | 820 tests in 65.954 seconds, OK, including named Git/recovery acceptance |
| Runner package discovery | 684 tests in 33.569 seconds, OK |
| Executor package discovery | 171 tests in 3.618 seconds, OK; one non-failing closed-socket ResourceWarning |
| Git-availability guard simulation | 26 lifecycle/workspace tests all skipped cleanly with `shutil.which` patched before import; this is a guard simulation, not execution in a Git-free image |
| Black / Ruff / mypy | Passed; Black/mypy checked 377 source files |
| Migrations / drift / system | All migrations, including 0011, applied on own database; no model drift; system checks clean |
| Whitespace | `git diff --check f97bce6d...HEAD` passed |
| Independent full lifecycle read-control reproduction | Four waits, zero read spawns, zero consumed attempts, same ten original operations, no finish |

Logs are `/tmp/vibearr-review04-r6-{django,distributions,verify,runner,executor,
gitfree,black,ruff,mypy,migrate,read-controls}.log` (the brace expression identifies
separate files). Initial bare discovery commands omitted the package top level
and produced import errors; corrected package discovery and the canonical
portable verification passed. Those invocation errors required no source change.

Only reviewer databases `vibearr_review04_r6` and `test_vibearr_review04_r6` were
used on the assigned disposable PostgreSQL instance. No original workspace,
author runtime/service/database/state, source publication, paid model call,
adversarial probe, unrelated cleanup or merge was performed. Only this report
is committed.

## Real acceptance checked independently, with limits

Public `docs/acceptance/feature04-issue99.json` result exactly equals private
`/tmp/vibearr-feature04-acceptance/issue99-delivery.json` (SHA256
`46c4e00f47678fc759b0efdd54825e2638745181de7ec4e51cd9c255078ed16a`).
Its delivery equals captured server `verified_delivery`; public termination equals
captured server `native_termination` in `third-server-readback.json` (SHA256
`19a84da4ebc8937041422205b5dd45f809c5d49306a4bf535eb2124c1f836ffb`).
Finish digests match and current evidence/termination validators accept that
historical chain. The server capture contains the latest ten of eleven evidence
records, so it is not a complete captured intent history.

Run `d913fa25-5e89-4725-8d24-b381d95d51fa` delivered original commit
`a44468bf797fd3d0dba0b7e39378ac9df4abf878`, tree
`268c088c788109c9daebef22cc887c7324b9ab8b`, with all nine actual OCI exit-zero
checks and no repairs. Ten original executor operations terminated, all 46 local
processes stopped, and two publication stages have known PR identity. Reporting
acknowledgement is true with finish digest
`65583dad5cec222a323518d510afc16a9ca7013bf2227b8f6076e25b7568cd81`.
The evidence records 169 supervisor generations and distinct PIDs at agent,
push, PR and completion-reporting checkpoints. Native session
`01a1e51b-2843-78e1-af02-cb7d4278ffcb` used Codex CLI 0.169.0.

Read-only provider lookup confirms [PR 104](https://github.com/innateessence/nightly-queue/pull/104)
is non-draft and was subsequently merged on November 16 at 16:49:16 UTC after a
later review-document-only head `40408acc33637f70e9a122d382d67c75a3d18e11`.
That later provider merge does not alter the historical native evidence's
original head. Both acceptance screenshots are retained; the delivered screenshot
was visually inspected and shows the matching run, native session, zero repairs,
passed validation and PR identity.

The paid run used immutable supervisor `24386a2d11df53e159be0b3e09e54658c31c7370`,
not the later launch/read fixes or this reviewed head. Their positive push argv
is unchanged and their control boundaries have real nonpaid process regression
coverage; no current-source paid acceptance is claimed. The public history
truthfully retains all three attempts, including explicit cancellation and the
second prepublication failure after a wrong profile registry restart. No fourth
paid task was requested or run.

The executor source binding remains
`bb447de81c9a7956daf800981033c0325949e316126c81ac8d975bf7c4c74b6f`, with protocol 1,
qualification version 3 / Q13 and a separate persistent instance identity.
Earlier actual Q13/control evidence was inspected; unchanged expensive OCI
qualification was not repeated. Live failing-validation/repair and the later
two-issue CI/merge bootstrap/MVP acceptance remain separate release work.

## Report integration

Required exact-report-head provider check results and report PR identity are
recorded in the review handoff. Root alone may serially squash-merge the report
after its own review and four-check exact-head gate. This report adds no production
code and makes no claim that historical paid evidence was generated on a later
source revision.
