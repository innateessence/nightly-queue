# GitHub work selection

Autonomous projects discover all issues for visibility. Execution permits new open
issues from the repository owner ID by default; `permitted_author_ids` replaces
that default with explicit GitHub user IDs. Public access and collaborator status
never grant execution permission. Issue bodies and comments remain task data.

`enabled_at` records the first enablement time and survives disable/re-enable.
Older open issues wait unless `backfill_issues` is explicitly enabled. Both
`vibearr:hold` and transitional `nq:hold` exclude work. Priority labels
`vibearr:priority-high` and `vibearr:priority-low` map to high and low; otherwise
normal. Claims order priority, local creation time, then queue ID. The UI shows
queued totals separately from source-eligible executable work. Native claims also
require enabled, unpaused execution and structurally valid project configuration;
one invalid project cannot prevent claims for another valid project. Generic
manual clients do not need native backend configuration.

Write dependencies and an optional branch on separate issue-body lines:

```text
Depends on: #123, #124
Branch: feat/example
```

Dependencies are limited to the configured repository. Missing, malformed,
cross-repository, cyclic, and unmerged dependencies have visible waiting reasons.
Closing an issue or creating a PR never satisfies a dependency. The branch must
be a valid branch ref, differ from the base branch, and avoid other work-item
branches. Claim freezes branch identity; retry retains it. Fallbacks are
`vibearr/issue-N` and `vibearr/manual-ID`.

## Synchronization and upgrade

New settings default to 60-second polling. The seconds field supports 60–86400.
The migration deliberately preserves every saved legacy hours value as its
seconds equivalent, including the former one-hour default: existing user
preferences are not silently shortened. Change the Settings value to 60 for
minute polling. The legacy hours column remains for migration compatibility and
no longer controls polling.

The scheduler checks every minute independently of execution pause. A global
scheduled-attempt timestamp controls the selected interval; each project also
has an expiring UUID sync lease, attempt/success timestamps, conditional page
ETags, a retry deadline, and a redacted error. Conditional responses reuse cached
provider data; all pages are visited. Rate-limit reset and Retry-After headers
set a minimum one-minute retry delay. Failed requests never advance success.
Manual synchronization bypasses the normal interval but respects project leases
and provider backoff.

Repository and author identities are numeric GitHub IDs. Known repositories are
resolved by ID, so renames refresh canonical URLs without duplicating work. Legacy
rows are bound to that ID on their first authoritative sync. Once synchronized,
select a different repository by creating a different project. Existing imported
rows require a complete metadata refresh before execution; existing autonomous
projects receive their enablement timestamp during migration, with backfill off.

## Interfaces for features 04, 06, and 07

`work_queue.services.source_wait_reason(item)` is the shared source policy.
`claimable_items()` supplies ordered candidates; `claim_item()` reloads and checks
policy under project/item locks. Imports and merge-evidence writes share the
project lock. Stale claims cannot win a changed eligibility check.

`QueueItem.source_metadata` holds current upstream metadata. `source_snapshot`
holds claim-time title/body, repository/issue identity, revision, and metadata.
Title/body and the snapshot remain unchanged while running. Closed/held syncs
request cancellation on active runs; reopening never requeues terminal work.
Explicit retries retain branch/repository/base identity and capture the latest
saved source instructions in a new attempt.

The authenticated endpoint is:

```text
POST /api/v1/runner/runs/<run_id>/source/refresh/
{"claim_token": "<UUID>"}
```

It uses the owned run's selected issue credential, never the import credential.
It re-reads the issue and bounded paginated comments before initial launch,
checks source policy and ownership again, and returns `source_snapshot`. Comments
are saved on the first successful refresh and remain immutable afterward.
A 503 means fresh context is unavailable and launch must wait; 400 reports changed
source/eligibility; 409 reports ownership/cancellation conflict. Changes between
claim and initial launch require an explicit retry. Feature 04 must call this
before launching an agent and honor cancellation at later checkpoints. The Python
service is `refresh_issue_for_launch(item_id, claim_token, token)` with keyword
arguments. API failures never authorize launch using stale data. Base SHA, model,
validation profile, and effective policy belong in feature 04's immutable run
context alongside this source snapshot.

Only trusted provider reconciliation may call the keyword-only service:

```python
record_verified_merge(
    repository_id=repository_id,
    issue_number=issue_number,
    pull_number=pull_number,
    merge_sha=observed_merge_sha,
    merged_at=observed_merge_time,
    provenance="github-pr-read",  # or controlled bootstrap reconciliation
    operation_id=stable_operation_id,
)
```

It stores durable `MergeEvidence`, validates IDs/commit format, and rejects
conflicting evidence. Exact replay is idempotent. The caller must verify the real
GitHub repository, PR, and observed merge first; the service deliberately has no
public arbitrary-JSON evidence endpoint. Feature 06 may seed already merged
prerequisites from verified provider observations. Feature 07 records actual
verified merges through this interface, including when issue closure/reporting
is still pending. Dependency lookup uses repository ID and issue number.

Rendered queue example: [work selection screenshot](screenshots/github-work-selection.png).

The configured `Project.base_branch` stays blank when the operator chooses the
repository default. Synchronization updates `github_default_branch` separately;
new claims freeze the current effective branch, while retries retain their
original checkout branch. Source-policy revisions include the discovered default.

Lock ordering is controller (for owned transitions), settings/credentials (when
needed), projects by ID, queue items by ID, then run updates. Import and fresh
source refresh only take project/item locks and never acquire settings afterward.
Requeue takes project/item locks before updating runs to avoid an inversion with
source cancellation. Real PostgreSQL tests overlap imports with runner claims
and provider refresh with requeue under bounded statement timeouts.

Additional rendered screens: [project policy](screenshots/github-project-policy.png)
and [sync settings](screenshots/github-sync-settings.png).

Each `Run.source_snapshot` retains its own claim-time source immediately, even if
context collection never succeeds. The first successful, claim-fenced comments
refresh finalizes that run's snapshot once; retries update the queue item's next
attempt without rewriting previous run history. Run API readback and history titles
use this durable snapshot. A provider response must match both the frozen source
and the accepted current database revision; delayed older responses cannot undo
newer synchronization or authorize stale work.

The generated `vibearr/manual-` branch namespace is reserved for manual tasks.
Collision checks include both explicit and inferred branches for every source;
a conflicting imported directive cannot take a manual fallback or starve the
remaining queue. Oversized dependency numerals become visible selection errors,
and malformed foreign issue data cannot abort later project imports.
