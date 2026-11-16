# Feature 04 independent review, round 2

Reviewed source: `729f70f51dbdb5e0ce43646fff2d98eecab10ff0` (PR 100).
Comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Reviewed source tree: `c7860796d926c08323a220db5c6baf70ff2627bb`.

**Outcome: changes required — one P2 finding.** This fresh review covers the
entire feature diff, including the server and executor supporting topics and
the two fixes after round 1. It does not clear this feature or authorize merge.
A different fresh reviewer must assess the complete feature after correction.

## P2: transient checkout failure terminates the attempt without bounded retry

Location: `vibearr_runner/delivery.py:276-281,357-371`, called from
`WorkspaceManager.prepare` in `vibearr_runner/workspaces.py:244-258` through
`NativeLifecycle.context` in `vibearr_runner/native_lifecycle.py:579-595`.

The new durable read wrapper and transient Git classification apply only to
`ls-remote`. The normal initial checkout also uses this transport for `clone`.
A completed clone process returning 128 with ordinary `Could not resolve host`
stderr raises plain `DeliveryError`. `NativeLifecycle._step` handles that as a
terminal failure and calls `finish_failure` immediately. No read budget/backoff
record is created, and the queue receives a failed completion after one attempt.
A momentary GitHub DNS/connectivity problem therefore abandons otherwise eligible
unattended work, contrary to feature 04's transient transport retry requirement
and the MVP's three-attempt/backoff contract.

Independent ordinary reproduction, without any real network or paid invocation:
`/tmp/vibearr-review04-r2-clone-repro.py` and
`/tmp/vibearr-review04-r2-clone-repro.log`. It uses the existing lifecycle fixture
for the server/executor, replaces its workspace stub with the real
`WorkspaceManager`, and mocks only the clone process's completed DNS failure;
ordinary local Git commands execute. Its output at the exact reviewed source is:

```json
{"clone_attempts": 1, "failure": "Repository operation failed; branch and any pull request are preserved.", "finish_statuses": ["failed"], "paid_starts": 0, "result": "reporting_pending", "transport_reads": null}
```

Correct this as a bounded native checkout read with durable operation identity
and tracked-process/partial-checkout reconciliation. A clone writes private local
files, so simply adding it to a blind retry loop is insufficient: confirm the
original process stopped before any cleanup, only remove a partial checkout owned
by that recorded clone, resume a completed clone's local setup without cloning
again, retain unknown process ownership, and preserve original deadlines and
pause/cancel launch checks. Keep push/PR mutation reconciliation unchanged.

## Review scope and prior fixes

Used a new full independent clone at `/tmp/vibearr-review04-r2`; the author
checkout and original workspace were not modified. Read `AGENTS.md`, feature 04
in `docs/autonomous-ai-queue-plan.md`, and the corresponding MVP execution,
validation, limits/recovery, PR-only delivery, and acceptance requirements.

Reviewed the complete changed-file set and traced controller selection/claim,
preflight account and repository checks, frozen source/context/plan/prompt,
scoped secrets, native session dispatch and readback, verified export/import and
recoverable commits, fixed nine-check validation and derived dependency profiles,
shared repair limits/original deadlines, current authority checkpoints, stable
repository identity, journaled push/PR delivery, server v2 evidence binding,
protected termination before slot release, cancellation and explicit retry,
restart/ownership loss, executor instance/presence binding, pause/deadline
controls, selective retention/tombstones, packaging, and minimal run views.
Historical server/runtime reports were context, not inherited clearance.

The round 1 direct GitHub GET issue is addressed for the reviewed GET path:
transient network/5xx/rate-limited GETs use durable three-attempt records,
backoff and rate-limit times; success clears the corresponding record; mutation
network failures remain unknown and are not blindly retried. Exhausted publication
readback retains the ownership fence. The new tests exercise these distinctions,
including restart/backoff and a completed agent plus passing validation.
The uncovered clone path is the P2 above.

The round 1 application Git dependency issue has availability guards on the three
new Git fixture classes. All fixture tests execute in the independent Git-equipped
PostgreSQL run below. A separate guard simulation makes `shutil.which` return
`None` before importing those modules: all 28 selected tests skip without error.
This simulation is evidence of the guards, not a substitute for the intentional
Git-free Django image CI. Runner verification and trusted validation must retain
real Git-equipped coverage; skipping the same checks everywhere is insufficient.

No additional P0/P1/P2 functional defect was established. In particular, a real
queue-client POST source-refresh HTTP 503 becomes `AmbiguousRequest`, which
propagates to controller pause/reconciliation; a generic `APIError` mock would
misrepresent that path and is not reported as a terminal source-read bug.

## Independent checks

| Check at reviewed source | Result |
| --- | --- |
| Full PostgreSQL-backed `manage.py test --noinput` | 1,741 tests in 64.235 seconds, OK; four distribution tests skipped because shared app tools lack build packages |
| Actual artifact tests, separately and sequentially | All four `vibearr.test_distributions` tests passed in 72.382 seconds using own Python 3.12 venv with setuptools 80.9.0 / wheel 0.48.0; canonical runner, compatibility runner, executor and CLI sdist/wheel/install paths exercised |
| Full `python -m vibearr_runner.verify` | 790 tests in 63.709 seconds; four errors in inherited acceptance test doubles that still replace `Lifecycle.ready` with a one-argument lambda, while the controller now supplies state; no complete passing verifier claim |
| Git-availability guard simulation | All 28 selected lifecycle/workspace/supervision tests skipped cleanly, no errors; actual Git-free image CI still required |
| Black / Ruff / mypy | Passed; Black and mypy checked 373 source files |
| Own database migrations / drift / system checks | All migrations applied; no pending model migrations; no system-check issues |
| Diff whitespace | `git diff --check c56dfc7...HEAD` passed |
| Supplemental actual lifecycle clone failure | Confirmed P2 above; one clone call, terminal failed finish, zero paid starts |

Logs: `/tmp/vibearr-review04-r2-tests.log`,
`/tmp/vibearr-review04-r2-distributions.log`,
`/tmp/vibearr-review04-r2-runner-verify.log`,
`/tmp/vibearr-review04-r2-gitfree-guards.log`,
`/tmp/vibearr-review04-r2-style.log`,
`/tmp/vibearr-review04-r2-migrations.log`,
`/tmp/vibearr-review04-r2-migration-check.log`.

The author independently identified the four legacy acceptance fixture errors
and is correcting the test doubles. This report records the exact reviewed
source's failing verification honestly; it does not clear that later change or
invent a second runtime defect from the fixture mismatch. The final runner check
must pass on the eventual exact source head.

Tests used only reviewer-owned databases `vibearr_review04_r2` and
`test_vibearr_review04_r2` on the assigned disposable PostgreSQL service. No live
application database, author service/journal/credential, GitHub source mutation,
paid model call, exploit/adversarial probe, deployment control, or merge was used.
Only this review report is committed.

## Acceptance and CI limits

The author-operated actual issue 99 run was still active when this report was
written. The final one-PR/all-nine-check/exact-commit proof and acknowledged
server v2/termination readback were not available to this reviewer. The first
cancellation and earlier qualified runtime artifacts do not establish successful
final issue-to-PR completion. This report makes no final real-acceptance claim;
the next fresh reviewer/root must inspect final proof, distinct restart processes
at each required waypoint, source revisions used, and server acknowledgement.

The unchanged executor qualification was already independently reviewed in the
supporting topic and root review. This round traced its control code and tests
but did not repeat expensive OCI qualification or control the active acceptance
services. Source/unit fixtures and provider CI cannot replace that actual proof.

Required provider checks at the exact report head are to be recorded separately.
Root alone may merge a review PR serially after its own exact-head CI/review;
this historical finding must remain visible even after the feature is corrected.
