# Complete context and saved plan handoff

Feature 07 adds deterministic context retrieval, immutable documents, a saved-plan
handoff, and `/runner/runs/` operator views. It does not enable execution or call a
coding model. Research generation and implementation use this contract in later
branches. A structurally valid saved plan is never a command approval.

## Supervisor context retrieval

`coding_runner.context.GitHubContextClient` uses only Python's standard library.
It reads the claimed `issue_repository` and `issue_number`, independently of the
project's current URL and the claimed checkout repository. Manual tasks use their
claimed title and instructions through `manual_context`, without an HTTP request.
The supervisor supplies the separately selected issue-read credential; the
client never reads Django, the database, importer credentials, or environment
secrets. It rejects redirects and pagination outside the exact GitHub issue API.

Every issue and comment records its author, body, source URL, creation/update
timestamps; the context records current title, complete discussion, fetch time,
and frozen checkout settings. Null GitHub bodies mean empty text. Closed issues,
missing fields, malformed data, unavailable pages, duplicate comments, changed
comment counts, and excessive responses/pages fail closed. An unavailable
context is never replaced by an empty discussion or an older snapshot.

Requests have a 15-second default socket/deadline bound, a 4 MiB page/discussion
bound, and at most 1,000 pages. GitHub does not provide an atomic multi-page
snapshot: a changed count triggers a retry, while an edit occurring during a fetch
may only be seen by the required subsequent refresh. No retry launches a model or
executes discussion content.

## Document and HTTP contracts

All machine endpoints live below `/api/v1/runner/runs/<run UUID>/` and require the
owning runner identity. Writes additionally require the current claim token and
an operation UUID. Stale/requeued runs and foreign identities cannot write.
Repeated operation UUIDs acknowledge the same immutable document; different
content conflicts. Replaying an older saved-plan write after a context refresh
returns its original record without changing the current references.

| Endpoint | Request / response |
| --- | --- |
| `POST contexts/` | `claim_token`, `operation_id`, `document`, `base_commit`, `resolved_base_branch`, `reason`; returns `context` |
| `POST plans/` | `claim_token`, `operation_id`, `document`, `reason`; returns `plan` |
| `GET contexts/`, `GET plans/` | Ten metadata records per page, `has_more`, `next_after`; next request uses `?after=<sequence or version>` |
| `GET contexts/<document UUID>/`, `GET plans/<document UUID>/` | An owned immutable document and metadata |
| `GET handoff/<operation UUID>/` | Recover an owned committed handoff after a lost response; unknown operations return 404 |
| `POST handoff/` | `claim_token`, `operation_id`, `context_id`, `base_commit`; returns saved `plan`, fresh `context`, and `actions_require_approval: true` |

Each saved context has a UUID, sequence, operation ID, document digest,
discussion digest, inspected commit, resolved base branch, revision reason,
creation time, and a list of material changes. Context documents use schema 1 with
exact keys `schema_version`, `source`, `title`, `instructions`, `issue`, `comments`,
`fetched_at`, and `checkout`. `checkout` preserves the claimed `repository_url`,
requested `base_branch` (possibly blank), and `branch_name`. The separately saved
resolved base branch is mandatory, including when the requested branch is blank.

The schema-1 plan has these exact fields:

- `schema_version`, `context_id`, `context_digest`.
- `problem`, `expected_behavior`, `acceptance_criteria`.
- `findings`, `sources`, `relevant_paths`, `approach`, `tradeoffs`.
- `steps`, `file_changes`, `setup_actions`, `validation_actions`.
- `regression_cases`, `delivery_evidence`, `blockers`, `open_questions`.
- `original_issue`, `checkout`, `resolved_base_branch`, `inspected_base_commit`.

Problem, expected behavior, approach, acceptance criteria, implementation steps,
file changes, validation actions, and delivery evidence are required. Paths stay
inside the checkout. Sources have `kind`, `reference`, and SHA-256 `digest`.
Setup/validation entries are exact `ActionSpec` proposals from feature 06, with
argument arrays, explicit interpretation, source/workspace/base/content bindings.
Every action references a listed source and the inspected commit. A proposal
cannot contain an `approved` flag. Blocked plans may be saved for operator review.

Plan versions have a UUID, monotonically increasing version, context foreign key,
operation ID, digest, revision reason, timestamp, and `supersedes` reference.
There are at most 200 context snapshots and 200 plan versions per run. Context
writes are bounded to 4 MiB; plans to 256 KiB. Configure the runtime HTTP
`Client(..., max_bytes=8388608)` for full bounded context documents and wrappers.
List APIs exclude document bodies so ordinary metadata reads stay small.

Documents containing claim tokens, the current bearer credential, or sensitive
JSON keys are rejected before persistence, preserving actual context and source
digests. Other request metadata is redacted. Backend credentials are never fetched
by these APIs.
Operator pages escape issue/comment/research text and do not render it as HTML.
The existing private-LAN UI access model is unchanged.

## Handoff and refresh

Use `ContextPlanStore(client, run_id, claim_token, SourceStore)`:

1. Inspect the supervisor workspace and call `refresh(claimed_item, base_commit,
   issue_client=..., resolved_base_branch=workspace.resolved_base_branch)`.
2. Generate research and call `save_plan(document, saved_context, reason=...)`.
3. Immediately before implementation, call `handoff` with the claimed item,
   inspected commit, issue client, and resolved branch. It fetches the entire
   discussion again, saves a new snapshot, then verifies the saved plan.

The supervisor must inspect the actual workspace/base, never accept a backend's
asserted revision. `refresh` returns local `sources` references computed from the
original supervisor-fetched canonical bytes. Research consumes these references;
it must not overwrite them by registering redacted server readbacks. Repository
and external research sources must similarly be read and registered by the
supervisor. The private source registry retains membership across restarts and
invalidates removed discussion sources. The issue source includes its current
title as well as its body and identity. Unchanged manual fetch timestamps do not
change provenance. Actual execution rechecks source, workspace, and base through
`ApprovedExecutor`, and consumes a separate single-use human permit.

A handoff requires the latest saved plan and complete context, no unresolved
blockers, unchanged discussion and base/resolved branch, and a complete refresh
fetched and saved after the plan and within five minutes. Fetch timestamps more
than 30 seconds in the future are rejected at handoff. Any material discussion or base
change requires an explicitly saved new plan version with an assessment reason.
The runtime does not infer semantic equivalence from text changes. Unchanged
refreshes retain the existing plan. Failed refreshes block handoff.

Pass a stable `operation_id` to `handoff` and retain it across uncertain responses.
The store first reconciles that operation without writing another context. A
committed operation returns its original immutable response; it does not renew
freshness or supersede newer evidence. Start a new operation for a new handoff.

Runner-owned progress into implementation, validation, and delivery requires a
verified saved-plan handoff; generic queue clients retain their existing API.
Later stages must refresh again when that evidence expires. This also prevents
changing progress references to an arbitrary or superseded plan/context.

## Validation and views

Deterministic tests cover pagination, empty/null content, missing/malformed and
unavailable pages, closed issues, frozen checkout/issue identities, immutable
versions, stale/foreign claims, idempotent writes after refresh, discussion/base
changes, blockers, source digests/removal, and pre-implementation enforcement.
Tests use fake issue responses and local supervisor source stores; no model or
live issue/queue mutation is required.

Operator history pages retain access to older runs with pagination. Detail pages
show one plan and one complete context per page, with independent version cursors,
so document history does not accumulate into an unbounded response.

The [context and plan screenshot](screenshots/runner-context-plan.png) and
[mobile screenshot](screenshots/runner-context-plan-mobile.png) show the actual
Django page rendered from synthetic local test data. Production execution remains
disabled pending the controlled release verification.
