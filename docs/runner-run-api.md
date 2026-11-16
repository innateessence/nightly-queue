# Durable runner control API

This is the first runner feature. It adds durable ownership and reporting, not an
execution engine. The identity probe advertises `execution_enabled: false`.
Backend execution remains disabled until the controlled release checks in
`runner-feature-branches.md` succeed. Existing `nq` and general queue API clients
keep their existing authentication, payloads, and completion behavior.

## Bootstrap

Apply migrations, then provision a separate, high-entropy runner token through
infrastructure secret injection. Set `NQ_RUNNER_API_TOKEN` to at least 32
characters and run:

```sh
python manage.py provision_runner controller
```

Only a SHA-256 digest is stored; no token value is printed or returned. The token
must differ from `NQ_API_TOKEN` and from every other runner identity's token.
`--token-env VARIABLE` selects a different environment variable. Repeating the
command rotates and enables the same identity; old bearer tokens immediately
stop authenticating. `python manage.py provision_runner controller --disable`
revokes access without destroying runs. Rotation/revocation serializes with
runner writes. It does not stop processes or release a claim. Stop the previous
runner and agent before using the existing admin requeue action.

Runner requests use `Authorization: Bearer <runner-token>`. The generic API token
cannot authenticate runner APIs, and runner tokens cannot authenticate generic
queue APIs. Future settings/secret endpoints use
`runner_control.auth.runner_api_view(method)` and `request.runner_identity`.

## HTTP contract

All paths below start with `/api/v1/runner/`. POST bodies are JSON objects.
Successful writes, including exact replays, return HTTP 200. Authenticated
responses carry `Cache-Control: no-store`. Invalid input returns 400; invalid or
revoked authentication returns 401; foreign and nonexistent runs both return 404;
ownership conflicts and conflicting operation replays return 409.

| Method/path | Request/result |
| --- | --- |
| GET `identity/` | `schema_version: 1`, `runner: {id, name}`, and capability flags |
| POST `runs/claim/` | `{run_id: UUID, configuration_revision?: string}`; returns `{run, item}` |
| GET `runs/active/` | Own active `{run, item}`, or `{run: null, item: null}`; never claims |
| GET `runs/<UUID>/` | Own reconciliation result `{run, item}` |
| POST `runs/<UUID>/claim/` | `{}`; recover the claim token for an owned active run |
| POST `runs/<UUID>/progress/` | `{operation_id: UUID, claim_token: UUID, stage, wait_reason?, context_reference?, plan_reference?, data?: object}` |
| POST `runs/<UUID>/artifacts/` | `{operation_id: UUID, claim_token: UUID, kind, data: object}` |
| GET `runs/<UUID>/evidence/` | Paginated evidence; optional `after` integer cursor and `kind` filter |
| POST `runs/<UUID>/finish/` | `{claim_token: UUID, status, result_url?, error_summary?}`; returns `{run, item}` |

Only the two POST claim/recovery paths return `item.claim_token`, and only while
the run still owns active work. Ordinary item/run responses omit claim tokens,
token digests, and credential payloads. Owned item responses include the frozen
top-level `repository_url` and `base_branch` independently of the current project.

Persist the run UUID locally before a claim. It is globally unique and identifies
one attempt or one empty-queue observation forever. A duplicate claim returns the
same attempt and never consumes another item. An empty outcome remains empty even
after work arrives; use a new UUID for the next poll. A completed or obsolete UUID
also never claims new work. At most one runner run can own work globally; a new
UUID while any run is active returns 409. A singleton transaction lock and a
database active-slot constraint enforce this across identities and controllers.
There is no automatic expiry, lease, or requeue.

After a lost claim response, GET the saved run UUID (404 means the request did not
commit), then recover its token with the protected POST if active. After a lost
finish response, GET the run and inspect `state` and `result_status`. Repeating
finish with the same token and normalized result returns the durable completion;
a different token or result returns 409. Do not create a fresh UUID to resolve an
ambiguous operation.

Runs have `active`, `completed`, `empty`, or `obsolete` state. Queue statuses remain
unchanged. Stages are `claimed`, `context`, `research`, `planning`, `implementation`,
`validation`, `delivery`, `waiting`, and `finished`; only finish sets `finished`.
Progress updates change only explicitly supplied fields. Clear `wait_reason` on
resumption. Context and plan references identify versioned artifacts supplied by
later features. The run holds a frozen configuration snapshot and revision. The
feature-02 hook `locked_configuration_snapshot(project, expected_revision)` runs
inside the claim transaction before queue mutation and returns `(revision, dict)`.
Until feature 02 replaces it, only an empty revision is accepted.

## Evidence and completion

Progress and artifacts are append-only through the API. A UUID operation ID is
unique across all evidence kinds for its run. An exact sanitized replay returns
the original entry; different content returns 409. Replaying older progress never
rolls back newer stage/reference updates. Each request is limited to 64 KiB,
20 JSON nesting levels, finite numbers, and 1,000 evidence entries per run.
Uploads require the active claim; obsolete or completed claims cannot append.

Clients must redact credentials before uploading display evidence. The API also
redacts secret-like keys (`token`, `secret`, `password`, `authorization`, private
key, encrypted payload), and the current bearer/claim token in nested keys and
values. It cannot recognize arbitrary secrets pasted under innocuous keys; full
local logs belong in protected runner storage. Secret delivery and the stronger
credential boundary are implemented in later branches.

Run responses include the latest ten evidence entries, `evidence_count`, and
`evidence_has_more`. For complete recovery, page `evidence/` from `after=0`.
Responses contain at most ten entries with `id`, `operation_id`, `kind`, `data`,
and `created_at`, plus `has_more` and `next_after` (null at the end). Filtering by
`kind=delivery` or `kind=validation` supports targeted recovery. Do not interpret
a truncated inline evidence list as a complete history.

Runner success requires the latest `validation` artifact to contain `passed: true`
and a nonempty `commands` list of `{command: string, exit_code: 0}`, plus the latest
`delivery` artifact with `verified: true`, `commit_sha` (40/64 lowercase hex
characters), and `pull_request_url` matching `result_url`. The result must be an
HTTPS PR URL with path `/<owner>/<repo>/pull/<number>`, without embedded credentials.
These are trusted supervisor reports; this API does not independently query Git
or GitHub. Real command/delivery verification belongs to implementation and PR
delivery features. A failed run can report a concise error and preserve any
existing PR URL. Generic clients retain their previous success rules; using an
`execution_ref` string beginning with `runner:` never establishes ownership.

Admin requeue retains run evidence, completion results, configuration, and checkout
settings; it marks prior ownership obsolete and invalidates the queue token. A
retry uses a new UUID and token. Run history protects queue items from deletion;
the queue removal view reports this instead of discarding evidence. Retention and
operator run views are deferred to the operations feature.
