# Feature 01 independent review, round 1

Reviewed PR: [#50](https://github.com/innateessence/nightly-queue/pull/50),
`feat/autonomous-execution-policy`.
Exact implementation head: `9e75d14fa4422c27487d4f6156c883ca0947e279`.
Review date: 2026-11-16 UTC.

**Result: no P2-or-higher findings.** No implementation changes were made by this
review. This report applies only to that exact head, not subsequent feature work
or the separate rename PR.

## Scope and findings

Read the actual implementation diff and surrounding claim, controller, secret,
redaction, recovery, approval, delivery, settings, and queue code. Compared the
change with feature 01 of `docs/autonomous-ai-queue-plan.md`,
`docs/vibearr-mvp.md`, and `docs/runner-execution-policy.md`.

- Migration `0009` disables the installation's execution switch and requests
  cancellation of active legacy runs. It retains the active slot, original claim,
  approval decisions, consumed execution identity, and unknown-outcome evidence.
  It does not claim termination, release ownership, or requeue surviving work.
  Existing disabled installations remain disabled. With an independently repaired
  valid legacy claim in the migration fixture, both decision and consumption are
  still rejected specifically because cancellation is pending; this avoids
  mistaking a stale fixture claim for proof of the new cancellation fence.
- Native configuration snapshots identify native execution, the versioned policy,
  its digest, and receipt version 2. Implementation credentials remain required;
  unused research credentials neither enter native account inventory nor gain
  runtime/preflight access. Legacy snapshot credential scoping remains intact for
  history. Configuration presentation retains the broader configured references
  needed to redact settings, while native run operations do not decrypt inactive
  research credentials.
- `current_native_policy` refreshes the persisted run and item before checking
  claim ownership and cancellation. Caller-side snapshot modification cannot
  replace the persisted policy; persisted claim, digest, and cancellation changes
  invalidate a stale in-memory run. Current global pause/enable controls and a
  changed effective policy reject the binding. This helper is expressly a binding
  check, not an executor permit or a replacement for authenticated API boundaries,
  schedule, quota, runtime, or dispatch checks.
- New native approval creation, human decision, and permit consumption all reject
  before parsing or granting an action. Historical native or cancelled requests
  show no decision controls. The migration's cancellation fence also blocks the
  old endpoints for existing legacy active work.
- Policy parsing rejects unknown/missing fields, unsupported execution kinds and
  versions, non-integer resource limits, and forbidden network syntax. The receipt
  validator checks exact nested schemas, identity/digest shapes, unique validation
  executions, successful ordered timezone-aware validation, tree/push/PR binding,
  and matching successful remote checks for merge-shaped receipts.
- Legacy readiness always returns false, including with a qualified-looking old
  proof. Native work is rejected before proposal dispatch. Native progress may be
  recorded without a legacy plan, but that does not grant delivery or success:
  native `verified_delivery` writes reject, and direct success also rejects even
  when a delivery row was preinserted for the independent regression test.
- Global policy JSON and project inherit/replace validation follow the existing
  settings paths. Clearing policy is rejected; policy changes alter the
  configuration revision and policy digest. Existing form/error and project
  configuration tests continue to pass.

## Independent validation

Used a full clone at `/tmp/vibearr-review01-r1`, checked out at the exact reviewed
SHA before creating the report branch. Used Python 3.12 from
`/tmp/vibearr-tools/bin/python`, and only disposable PostgreSQL at
`127.0.0.1:32771`, database `vibearr_review01_r1` and Django's corresponding test
database. No production application, database, volume, or live runner was used.

Commands run from the clone, with `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_HOST`, and `POSTGRES_PORT` bound to that disposable
instance for Django commands:

| Command | Result |
| --- | --- |
| `/tmp/vibearr-tools/bin/python manage.py test --noinput` | 1,284 tests passed in 44.880 seconds |
| `/tmp/vibearr-tools/bin/black --check .` | 214 files unchanged |
| `/tmp/vibearr-tools/bin/ruff check .` | Passed |
| `/tmp/vibearr-tools/bin/mypy . --config-file mypy.ini` | No issues in 214 source files; existing untyped-body note only |
| `/tmp/vibearr-tools/bin/python manage.py makemigrations --check --dry-run` | No changes detected |
| `/tmp/vibearr-tools/bin/python manage.py check` | No issues |
| `/tmp/vibearr-tools/bin/python -S -m unittest coding_runner.test_policy coding_runner.test_native_evidence -v` | 18 tests passed with site packages disabled |

A separate temporary reviewer module, `/tmp/review01_probes.py`, supplied nine
additional explicitly selected Django tests. All nine passed in 0.295 seconds:

1. Modifying only the caller's policy snapshot cannot change the returned
   persisted policy.
2. A persisted digest change invalidates a stale in-memory run.
3. A persisted claim-digest change invalidates a stale in-memory run.
4. Persisted cancellation invalidates a stale in-memory run.
5. Direct native success rejects even with preexisting delivery evidence and
   leaves the item running and run active.
6. A separate research-only account with a preflight grant cannot retrieve its
   secret through native preflight.
7. Unreadable inactive research material does not block native claim/progress.
8. Migrated consumed legacy actions with a valid original claim cannot receive
   another permit.
9. Migrated pending legacy actions with a valid original claim cannot receive a
   new human decision.

These tests were run with `PYTHONPATH=/tmp:$PWD` and the command
`/tmp/vibearr-tools/bin/python manage.py test --noinput <nine explicit test labels> -v 2`.
Their output is retained at `/tmp/vibearr-review01-r1-probes.log`; the full-suite
output is at `/tmp/vibearr-review01-r1-tests.log`. The temporary probes were not
added to implementation source.

A separate standard-library-only malformed-value matrix rejected all 29 inputs:
14 receipt mutations covering booleans, UUIDs, unknown nested fields, image
identities, timestamps, timeout, exit status, PR head/repository, mixed PR/merge,
NUL and Unicode errors; plus boolean, string and negative values for each of the
five policy resource/repair fields.

GitHub's `Run test.sh` check for this exact head was also observed passing:
[CI run](https://github.com/innateessence/nightly-queue/actions/runs/43820388702).
This is supplementary remote evidence; the tests above were independently run.

## Limits

Feature 01 defines contracts and retires legacy permission paths. It does not
install or qualify an OCI executor, run native Codex/Claude/OpenCode, enforce DNS
or container isolation, validate a real candidate with the future trusted profile,
authenticate receipt execution, or publish/merge a task PR. Those are later
features, and current native readiness and delivery correctly remain unavailable.
No actual paid coding session or deployment-boundary qualification was attempted.
The receipt module's structural acceptance alone must never authorize execution
or success; server/profile/provider verification remains required when feature 04
integrates it.
