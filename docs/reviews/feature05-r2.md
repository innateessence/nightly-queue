# Feature 05 independent review, round 2

Reviewed commit: `d7bf22282e7a378e2bfe16f02be39708851207dc` on
`feat/github-work-selection` (PR #55).

Review workspace: an independent full HTTPS clone at
`/tmp/vibearr-review05-r2`, with report branch `review/feature05-r2`.
The review used Python 3.12 and the disposable PostgreSQL database
`vibearr_review05_r2` on the designated development instance. It did not use
production services or data.

Result: **four reproduced P2 findings; not a clean review**. The review stopped
before a complete audit. This report records the findings and evidence already
obtained; no implementation changes are included.

## P2: A delayed source refresh mutates the next attempt

Location: `work_queue/services.py:508` (the `import_issue` call), followed by the
ownership recheck at `work_queue/services.py:521`.

A refresh checks its claim before provider requests, but persists the returned
issue before rechecking ownership. If the operator retries the item while that
request is pending and another run claims the same unchanged source revision,
the old response passes `expected_revision`. Its import updates current metadata
and may request cancellation on the new active run. Only afterward does the old
refresh reject its replaced claim.

Reproduction used the existing fixture configuration and a controlled provider
callback: claim a first run; during its read, explicitly requeue and claim a
second run; return closed issue metadata with a different revision. Observed:

```text
ValidationError ['Item is not running under this claim.']
second run: active canceled: True
source after stale call: closed cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc Returned after retry
```

The rejected operation has already changed the next attempt. Its claim and
active-run authority must be checked under the appropriate locks before any
source or cancellation mutation, and retained through that mutation. The existing
requeue overlap test returns unchanged provider data and asserts queue status and
token, so it does not detect this effect.

## P2: Unpermitted queued branch directives block permitted work

Location: `work_queue/services.py:229`, in `branch_wait_reason`.

Branch collision checks treat every other issue's requested branch as a
reservation, including issues whose authors are not permitted to run. An ordinary
issue from another author can therefore make an otherwise eligible owner issue
unclaimable by naming its branch, including its inferred fallback. This also
affects source checks before launch.

Reproduction: import permitted owner issue #100 with its normal fallback; then
import issue #101 from author ID 1337 with `Branch: vibearr/issue-100`. Observed:

```text
owner before outsider: ''
owner after outsider: Requested branch collides with another work item.
foreign: Issue author is not permitted.
global claim: None
```

Unadmitted, unclaimed source directives must not become authoritative branch
reservations. Preserve collision protection for legitimate reservations and
frozen claimed branches without allowing ineligible backlog to block permitted
work.

## P2: Fresh repository-owner metadata is ignored

Location: `work_queue/github.py:205` through `work_queue/github.py:214`, in
`read_issue`; the equivalent metadata handling is at
`work_queue/github.py:218` in `issue_comments`.

The fresh repository read checks the stable repository ID and uses its canonical
URL, but discards its owner ID and default branch. With the default author policy,
a repository transfer can therefore leave the former owner's issues authorized
even after the fresh-read operation has observed the new owner. The final source
check uses the old saved `github_owner_id`.

Reproduction used the actual `read_issue` and `issue_comments` functions with
controlled `github_read` responses. A project with saved owner ID 42 and empty
`permitted_author_ids` claimed an issue authored by ID 42. Fresh repository
metadata returned the same repository ID 99, a new canonical URL, and owner ID
777. The unchanged issue and empty comments were returned normally. Observed:

```text
fresh launch authorized former-owner issue: From former owner
saved owner: 42 fresh provider owner: 777
```

Fresh authorization must account for observed source-policy metadata changes,
either rejecting the changed policy pending reconciliation or reconciling and
fencing it before another attempt is authorized. A stable repository ID alone
does not establish that its owner-based author policy is unchanged.

## P2: Stale Project.save overwrites server-owned state

Location: `projects/models.py:75` through `projects/models.py:92`, particularly
the unrestricted `super().save` at line 92. Synchronization writes the same
server-owned fields through `QuerySet.update` in `work_queue/tasks.py`.

A project instance loaded before synchronization retains old identity and sync
fields. An unrelated settings edit then saves that instance in full, overwriting
newer server-owned fields. The settings lock in `Project.save` is not shared by
the importer's updates, and the method neither refreshes these fields under a
project lock nor restricts the update to editable configuration.

Reproduction: load an unsynchronized project instance; apply the sync-path
identity update with repository ID 12345, owner ID 42, and default branch `main`;
change only the stale instance's name and save. Observed:

```text
identity after unrelated settings save: None None ''
repository replacement now accepted: https://github.com/example/different.git
```

This can erase the stable repository binding and allow a repository replacement
that the synchronized-project guard is meant to reject. The same full save can
overwrite leases, timestamps, cached pages, and errors. Operator configuration
writes should preserve server-owned state and coordinate with project locking.

## Validation and limits

Read the repository instructions, feature 05 plan in
`docs/autonomous-ai-queue-plan.md`, `docs/vibearr-mvp.md`, and
`docs/github-work-selection.md`. Inspected the selection, synchronization,
configuration, claim, source-refresh, history, model, migration, API, and related
test paths. The reviewed commit was verified with `git rev-parse HEAD`.

Observed successful commands:

```text
python manage.py migrate --noinput
python manage.py test work_queue.test_selection work_queue.test_concurrency work_queue.test_auto_import work_queue.test_auto_queue work_queue.test_manual_sync runner_control.test_secrets --keepdb --noinput
```

Both used `/tmp/vibearr-tools/bin/python` and explicit environment variables for
the designated disposable PostgreSQL instance. Migration application succeeded.
The focused suite reported **147 tests passed in 3.923 seconds**, including its
PostgreSQL concurrency tests.

The four controlled service-level reproductions above completed against that
disposable database. They were separate from the committed test suite and are
not implementation changes.

A full `python manage.py test --keepdb --noinput` run was started, reporting
1,358 discovered tests and no system-check issues. Its final result was not
observed before the review stopped; no full-suite success is claimed. Formatting,
Ruff, mypy, migration drift checks, browser checks, live provider integration, and
native execution qualification were not independently completed in this round.
Earlier-round fixes were inspected and covered by the focused tests, but this
round did not complete all independent verification of those fixes.

Review stopped at the parent's explicit instruction after a platform content
flag. No further probes or tests were performed after that instruction. This
report does not establish the absence of additional P2 or higher findings and
does not qualify native execution or recommend merging PR #55.
