# GitHub work selection

Autonomous projects discover all issues for visibility. Execution permits new open
issues from the repository owner ID by default; `permitted_author_ids` replaces
that default with explicit GitHub user IDs. Public access and collaborator status
never grant execution permission. Issue bodies and comments remain task data.

`enabled_at` records the first enablement time and survives disable/re-enable.
Older open issues wait unless `backfill_issues` is explicitly enabled. `vibearr:hold` excludes work. Priority labels
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

## Synchronization

New settings default to 60-second polling. The seconds field supports 60–86400.
The scheduler checks every minute independently of execution pause. A global
scheduled-attempt timestamp controls the selected interval; each project also
has an expiring UUID sync lease, attempt/success timestamps, conditional page
ETags, a retry deadline, and a redacted error. Conditional responses reuse cached
provider data; all pages are visited. Rate-limit reset and Retry-After headers
set a minimum one-minute retry delay. Failed requests never advance success.
Manual synchronization bypasses the normal interval but respects project leases
and provider backoff.

Both scheduled and manual synchronization move queued issues to Closed when
GitHub reports them closed. They remain in All work and the Closed filter, but
leave the queued count and filter. Issues first discovered closed use the same
status. Reopening these unexecuted issues queues them again; running, succeeded,
and failed work keep their execution status and history. Closure never supplies merge evidence.

Repository and author identities are numeric GitHub IDs. Known repositories are
resolved by ID, so renames refresh canonical URLs without duplicating work. Projects are bound to that ID on their first authoritative sync. Once synchronized,
select a different repository by creating a different project. Imported work requires a complete metadata refresh before execution. Backfill is disabled by default.

## Supervisor interfaces

`work_queue.services.source_wait_reason(item)` is the shared source policy.
`claimable_items()` supplies ordered candidates; `claim_item()` reloads and checks
policy under project/item locks. Imports and merge-evidence writes share the
project lock. Stale claims cannot win a changed eligibility check.

`QueueItem.source_metadata` holds current upstream metadata. `source_snapshot`
holds claim-time title/body, repository/issue identity, revision, and metadata.
Title/body and the snapshot remain unchanged while running. Closed/held syncs
request cancellation on active runs; reopening never requeues succeeded or failed work.
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
claim and initial launch require an explicit retry. The supervisor must call this before launching an agent and honor cancellation at later checkpoints. The Python
service is `refresh_issue_for_launch(item_id, claim_token, token, runner=None, run_id=None)` with keyword
arguments. Owned runs must supply their runner and run ID. The HTTP request shape is unchanged. API failures never authorize launch using stale data. Base SHA, model,
validation profile, and effective policy belong in the immutable run context alongside this source snapshot.

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
public arbitrary-JSON evidence endpoint. Explicit bootstrap may seed already merged prerequisites from verified provider
observations. Human-merge reconciliation records actual verified merges through
this interface, including when issue closure/reporting
is still pending. Dependency lookup uses repository ID and issue number.


The configured `Project.base_branch` stays blank when the operator chooses the
repository default. Synchronization updates `github_default_branch` separately;
new claims freeze the current effective branch, while retries retain their
original checkout branch. Source-policy revisions include the discovered default.

Lock ordering is controller and runner identity (for owned transitions),
settings/credentials (when needed), projects by ID, queue items by ID, then run
updates. Source refresh fetches without locks, then rechecks its original runner,
controller proof, run, credential selection, and claim before applying anything.
Import only takes project/item locks and never acquires settings afterward.
Requeue takes project/item locks before updating runs to avoid an inversion with
source cancellation. Real PostgreSQL tests overlap imports with runner claims
and provider refresh with requeue under bounded statement timeouts.


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


Fresh repository metadata participates in launch authorization. Owner transfers
update the saved owner ID before checking the default author policy; explicitly
configured author IDs keep their intended meaning. Both the configured base and
the current remote default are protected task branch names, including when those
branches differ. Frozen checkout settings are retained; a new default that equals
the task branch blocks launch.

Only a successfully claimed, frozen branch reserves its name. Unclaimed issue
directives, including foreign or held issues, cannot reserve other work's names.
Competing admitted issues for the same branch are resolved by the normal atomic
claim order; the losing candidate then waits for the branch collision.

Normal project edits lock the current project after settings and retain all
server-owned GitHub identity, default-branch, enablement, and synchronization fields.
A stale name/form save cannot erase a synchronization result or permit repointing
a bound repository. Synchronization also fences a changed configured repository
before binding the response to work.


Repository observations use the server-owned `github_authority_version` together
with the complete accepted repository ID, owner ID, default branch, and canonical
URL. Both polling and launch refresh capture that state before network collection
and compare it under the project lock before applying any source changes. Every
accepted observation advances the version, including identical metadata, so a
newer accepted observation fences earlier in-flight replies. Import issue pages
and the final synchronization cursor are also bound to the accepted authority
state. A conflict waits for a fresh collection; it cannot restore an old owner,
unprotect a new default, overwrite a rename, cancel work, or finalize comments.
Normal project edits preserve this version with the other server-owned metadata.
Projects start at authority version zero; accepted provider observations advance it without changing author/backfill settings.
