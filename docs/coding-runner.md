# Coding runner architecture and implementation plan

## Status

The initial Codex implementation is available as an optional `coding-runner`
Docker Compose service. It supports repository policies, execution windows,
isolated workspaces, validation, branch delivery, PR creation, and persistent
interruption journals. See [setup and recovery](codex-runner.md). Claude Code and
OpenCode adapters remain planned. A real Codex-to-GitHub acceptance run is still
required before enabling unattended production execution.

## Responsibilities

| Component | Owns |
| --- | --- |
| Django application | Projects, queue state, settings, and result display |
| PostgreSQL | Durable application data |
| Celery Beat and worker | Scheduled GitHub issue discovery |
| Coding runner | Execution schedule, claims, workspaces, agents, validation, Git operations, PR delivery, and completion reporting |
| Codex, Claude Code, or OpenCode | Repository inspection and task implementation within a workspace |
| Optional Ollama service | Model inference for a compatible coding backend |

The runner owns the full claim-to-finish lifecycle. Agent output supplies evidence
and context; the runner verifies command results, validation results, and PR
delivery before reporting success.

## Container and process layout

- Add a separately built `coding-runner` service and package. Keep `nq` independent
  of the runner and Django.
- Use the existing HTTP API at `http://nightly-queue:8000` on the Compose network.
  The runner has no direct database or Celery broker dependency.
- Process one item at a time initially. Claim only when capacity is available and
  the configured execution window is open.
- Run as a non-root user with an isolated workspace per task. Do not mount the live
  application source, host home directory, or host Docker socket into the agent's
  environment.
- Persist execution metadata, logs, checkout state, and delivery results in a
  dedicated runner volume. Define retention and cleanup behavior.
- Install Git, GitHub tooling, and the selected coding backend. Start with the
  target projects' build dependencies; add project-specific images when needed.
- Pin coding tool versions in the image. Model authentication and selection remain
  backend-specific configuration.

## Task lifecycle

1. Check execution settings and runner credentials before claiming work.
2. Persist a unique run ID, then call `POST /api/v1/items/claim/` with
   `execution_ref` set to `runner:<run-id>`. An empty queue returns `item: null`;
   wait for the polling interval before checking again.
3. Persist the item ID, claim token, and returned checkout settings. Use the
   top-level `repository_url`, `base_branch`, and `branch_name`. Resolve a blank
   base branch to the repository default and record the resolved branch.
4. Clone/fetch the repository into the task workspace. Create or reuse the returned
   branch, preserving prior work and an existing PR during retries. Do not discard
   changes or force-push automatically.
5. Run the selected coding agent with the task title, instructions, issue metadata,
   and applicable repository guidance. Enforce the task timeout.
6. Inspect the diff and run project validation. An agent's success message or exit
   status alone does not establish that the task is complete.
7. Commit changes, push the branch, and create or reuse a PR against the resolved
   base branch. Record the commit and real PR URL before reporting completion.
   Include validation evidence and link the originating issue when applicable.
8. Call `POST /api/v1/items/<id>/finish/` using the exact claim token. Send
   `status: succeeded` and the PR URL in `result_url` after verified delivery.
   Report known execution failures with `status: failed` and a concise
   `error_summary`. Preserve useful logs and any PR already created.
9. Continue while the execution window and task limit permit. Closing the window
   prevents new claims; the current task may finish within its timeout.

Success means delivery for review. It does not mean CI passed remotely, the PR
merged, or the GitHub issue closed. The runner does not merge PRs automatically.
No-change results need an explicit policy before unattended operation; they must
not produce fabricated commits, PR URLs, or successful delivery reports.

## Settings and credentials

Keep execution scheduling separate from the existing 1h/4h/8h/12h/24h issue sync
setting. Plan Django settings for enabled/paused execution, continuous or nightly
windows with an explicit timezone, task timeout, and tasks per window. Provide
backend/model defaults and project overrides for backend, model, setup commands,
and validation commands. Read settings through an authenticated runner API that
must be implemented; the current API exposes neither execution settings nor these
project overrides. Paused execution must prevent new claims.

Configure the queue API token, GitHub clone/push/PR credentials, coding backend
authentication, and any Ollama endpoint as runner secrets or environment settings.
The current project credential API does not expose secrets. The initial runner
uses separately configured credentials and does not receive the application's
credential encryption key or database credentials.

Keep credentials and claim tokens out of agent prompts and published logs. Pass
task inputs as structured data or files, never as interpolated shell commands.
Repository and issue content must not grant access to other projects, host files,
or infrastructure. Define how requests requiring approval are surfaced and paused;
do not silently bypass required approvals to keep an unattended run moving.

## Interruption and recovery

The API now provides `GET /api/v1/runner/runs/<run-uuid>/` to reconcile a run by
its exact `runner:<run-uuid>` execution reference. It returns the current claim
token only while the item is running. An unknown or requeued execution returns
`item: null`; duplicate references return HTTP 409. The API has no heartbeat,
expiring lease, general item-detail endpoint, or requeue endpoint.
Interrupted items stay running until an operator stops the original
runner and agent, then uses **Requeue failed/interrupted work** in admin. Requeueing
invalidates the old claim token and preserves branch and checkout settings.

Persist run state before and after external operations. Distinguish implementation
failure from completion-reporting failure: a delivered PR must remain recoverable
when the finish request fails. Do not rerun the agent or create a second PR solely
because completion reporting failed.

Do not blindly repeat claims or finish calls after ambiguous network timeouts.
The request may already have succeeded; repeated completion returns HTTP 409.
The initial runner uses the reconciliation API to confirm completion or retry
reporting a persisted outcome for the same live claim. It stops for operator
review after ambiguous claims, interrupted agent runs, or ambiguous PR creation.
It never automatically repeats the agent. Extend and test restart and
cancellation behavior before adding automatic requeueing or lease expiry; prevent
two surviving agents from operating on the same task.

## Implementation order and acceptance

1. Add the runner package, image, Compose service, isolated volumes, and lifecycle
   loop. Prove authenticated connectivity without claiming production work during
   startup checks.
2. Add execution settings, project overrides, migrations, and authenticated runner
   configuration/reconciliation endpoints. Test scheduling, pause behavior, and
   timezone boundaries independently of issue sync.
3. Implement one coding backend behind a common adapter interface, then add Codex,
   Claude Code, and OpenCode adapters as separately verified integrations.
4. Implement checkout, validation, branch push, and repeatable PR creation. Test
   using temporary local repositories and stubbed provider responses.
5. Add persistent run logs, timeout handling, shutdown behavior, approval handling,
   reconciliation, and recovery. Test interruption after claim, push, PR creation,
   and completion reporting, including stale claims and ambiguous responses.
6. Update the UI with execution settings and run visibility. Run a controlled
   end-to-end task in a designated test repository: queued → running → succeeded,
   with a real branch and PR. Verify a failing task reports failed with useful
   diagnostics. Verify restarts do not duplicate agent runs or PRs.

The initial service covers the lifecycle and Codex adapter with environment and
file configuration. Django execution settings, project overrides in the UI,
execution settings UI, per-window task budgets, other backends, and broader automatic
recovery remain follow-up work. The existing queue API and issue importer remain
the foundation throughout this work.

Per-item web logs now provide execution history and live command output through
bounded, authenticated uploads. Web settings and a global runner dashboard remain
follow-up work.
