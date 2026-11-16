# Feature 05 independent review, round 1

Reviewed PR [#55](https://github.com/innateessence/nightly-queue/pull/55),
`feat/github-work-selection`, at exact commit
`d7e68936e4c8abd60f71a12443d1ccad677a2737`.

This report applies only to that commit, before the subsequent main/rename
integration. It does not clear any later head. Review ran in a separate full clone,
`/tmp/vibearr-review05-r1`, with its own PostgreSQL database. This change contains
only the review report; it changes no implementation or tests.

**Result: four P2 findings. Do not merge this reviewed implementation yet.**
All four were independently reproduced and sent to the implementation owner.

## P2: manual fallback branch collisions crash claims and block later work

Location: `work_queue/services.py:190–192`, `:297–302`, `:313–325`.

`source_wait_reason()` returns immediately for manual items, bypassing branch
collision validation. An imported issue can request the exact fallback branch of
an unclaimed manual item. The imported item sees no reserved branch on the manual
row and claims successfully. Claiming the manual item then violates
`unique_project_work_branch` rather than returning a visible waiting reason.

Reproduced on PostgreSQL:

1. Enqueue a manual item with ID `N`.
2. Import an eligible owner issue with `Branch: vibearr/manual-N` and
   `vibearr:priority-high`.
3. `claim_next()` claims the imported item.
4. The next `claim_next()` raises `IntegrityError: duplicate key value violates
   unique constraint "unique_project_work_branch"`.

The manual item remains queued and eligible. If it sorts before other work,
every later generic or native claim repeats the exception, so a conflict in one
project can block ready work in other projects. UI executable counts also include
the unclaimable manual item. Preserve generic manual compatibility while applying
shared branch safety to manual fallbacks; claims must not rely on the database
constraint as their collision response.

## P2: explicit retry destroys the previous attempt's source snapshot

Location: `work_queue/services.py:429–434`, `:304–310`; related
`runner_control/services.py:123–133` and `runner_control/models.py:44–46`.

The new source snapshot exists only on `QueueItem`, shared by every `Run` for that
item. `requeue_items()` clears that snapshot and replaces the title/body with the
latest source data without first preserving the original snapshot on the previous
run. The next claim writes a new snapshot to the same row. No source-history row,
context, or evidence is automatically saved at claim or successful refresh.

Reproduced with real `claim_run()` calls and the actual configuration service:

1. Claim an owner issue titled `First attempt instructions`.
2. Successfully refresh source with its first discussion comments.
3. Import edited source titled `Second attempt instructions` and a new revision.
4. Explicitly requeue, then claim a second run.
5. Reload the first run and its item.

Observed: the first run is retained as `obsolete`, but its item title and
`source_snapshot.title` both read `Second attempt instructions`; the original
comments are absent. `run.contexts.exists()` and `run.evidence.exists()` are both
false. This is a supported pre-context failure/retry path, so a future executor
saving a complete context cannot recover the lost attempt. Persist the source
snapshot per attempt, including the first successful comment refresh, and keep it
available after requeue without rewriting old history.

## P2: a stale launch read can authorize instructions superseded by a newer sync

Location: `work_queue/services.py:466`, `:478–487`; stale import guard at
`:107–113`.

`import_issue()` correctly rejects an upstream revision older than the saved
`issue_updated_at`. The launch refresh ignores that outcome and compares only the
network response revision against the frozen snapshot. It never checks that the
persisted current revision still equals that snapshot under the final locks.

Reproduced:

1. Claim revision A with body `Original instructions`.
2. Import revision B with a later `updated_at` and body `New instructions`.
3. Return revision A from the launch provider read, as can happen when a poll
   records B while the launch read for A is in flight.
4. Call `refresh_issue_for_launch()` with the current token.

Observed: refresh returns successfully with revision A and `Original instructions`,
while the database's current revision is B and its current source body is
`New instructions`. The stale import was rejected, but authorization still used
that rejected response. The launch must instead require explicit retry when the
current persisted source revision differs from the attempt snapshot. Add a
regression overlapping the newer import with the older in-flight fresh read.

## P2: an oversized dependency number aborts all issue synchronization

Location: `work_queue/services.py:45–57`; uncaught exception in
`work_queue/tasks.py:124`.

The dependency regex accepts arbitrarily many decimal digits. Conversion to
`int` happens before the `2**63` bound. Python 3.12 rejects a decimal numeral
longer than its default 4,300-digit conversion limit with `ValueError`; the
importer catches only `GitHubAPIError` and `ValidationError`.

Reproduced through actual `import_auto_issues()` with controlled provider reads,
a valid encrypted import credential, and an issue from foreign author ID 1337:

```python
body = "Depends on: #" + "9" * 5000
```

Observed: the task raises `ValueError: Exceeds the limit (4300 digits) for integer
string conversion`. The project lease is released, but `sync_error` remains empty.
The entire task aborts before later issues/projects can reconcile. A roughly 5 KB
issue body is sufficient, and discovery intentionally includes untrusted authors.
Bound numeral length before conversion and record a visible invalid-dependency
reason. Verify that later valid issues and projects still synchronize.

## Scope and positive checks

Read repository `AGENTS.md`, feature 05 in `docs/autonomous-ai-queue-plan.md`, the
work-selection/context requirements in `docs/vibearr-mvp.md`, and
`docs/github-work-selection.md`. Reviewed all changed production modules,
migrations, templates, and tests, plus the surrounding configuration, ownership,
credential, run, and immutable-context contracts.

Checked author IDs/default owner policy, enablement/backfill, holds/closure,
incomplete metadata, priority ordering, dependency syntax/cycles/merge evidence,
repository identity/rename binding, conditional pagination, leases/backoff,
separate attempt/success timestamps, source refresh, and retry behavior.
`record_verified_merge()` remains an internal service without an arbitrary public
JSON evidence-write route. Its ID/commit validation and conflict/idempotence
behavior are covered by the passing suite and code inspection. Actual verified
provider reconciliation remains the caller's responsibility as documented.

The reviewed default-base fix keeps blank operator `base_branch` separate from
`github_default_branch`, updates the discovered default on reconciliation, and
freezes the effective base on the claimed item. The associated changed-default and
explicit-base tests passed. The importer no longer calls `Project.save()` while
holding its project lock. Requeue takes project/item locks before run mutations.
Both real PostgreSQL overlap tests passed: import versus native claim and source
refresh versus requeue. These fixes do not address the findings above.

Additional actual API/rendered-template probes returned:

- Owned GitHub source refresh: HTTP 200; both issue and comment reads received
  `owned-run-selected-token`, distinct from the configured import token.
- Source refresh for the obsolete run: HTTP 409.
- Queue page: HTTP 200 with held-source waiting reason and separate queued total.
- Project and API settings pages: HTTP 200.
- Project form: explicit permitted author IDs `[42]` accepted.

## Validation performed

Python: shared read-only `/tmp/vibearr-tools/bin/python` (3.12).
Database: disposable PostgreSQL `vibearr_review05_r1` on `127.0.0.1:32771`;
Django also created/dropped its own `test_vibearr_review05_r1`. No production or
port-7777 state was accessed.

Commands below ran from the isolated clone with the disposable database settings:

```text
python manage.py migrate --noinput
  PASS: all migrations applied to the empty review database.
python manage.py test work_queue projects credentials runner_control --noinput
  PASS: 779 tests in 24.204 seconds.
python manage.py test --noinput
  PASS: 1,331 tests in 54.297 seconds.
black --check .
  PASS: 220 files unchanged.
ruff check .
  PASS.
mypy . --config-file mypy.ini
  PASS: 220 source files; existing untyped-function note only.
python manage.py makemigrations --check --dry-run
  PASS: no changes detected.
python manage.py check
  PASS: no issues.
```

Separate Python probes used real models/transactions/configuration and controlled
provider responses to reproduce each finding. They are review experiments under
`/tmp`, not added implementation tests. The existing suite passes despite the
four failures.

Limits: no paid agent execution, real GitHub issue mutation, live provider
rate-limit event, runtime qualification, or browser screenshot verification was
performed. Rendered UI probes used Django's client. Native actual execution is
feature 04's responsibility; its absence is not itself a feature 05 finding.
