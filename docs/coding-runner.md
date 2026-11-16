# Coding runner architecture and implementation plan

## Status

Durable runner identities, attempts, evidence, and reconciliation are implemented
in the [runner control API](runner-run-api.md). The execution workflow below
remains disabled pending the later feature branches and controlled release checks.

The next execution component is a dedicated `coding-runner` Docker Compose service
in this repository. This document defines the planned implementation. The current
stack stores tasks, imports GitHub issues, and exposes claim/finish APIs; it does
not fetch issue comments, research solutions, generate implementation plans,
execute coding tasks, or create pull requests. The workflow below remains planned. The runner settings and effective configuration
contract are implemented in [runner settings](runner-settings.md); execution remains
disabled pending the runtime and controlled release checks.

## Responsibilities

| Component | Owns |
| --- | --- |
| Django application | Projects, queue state, all runner settings in the web UI, credentials, and run/plan/result display |
| PostgreSQL | Durable application data |
| Celery Beat and worker | Scheduled GitHub issue discovery |
| Coding runner service | Execution schedule, claims, issue context, research/planning handoff, workspaces, validation, Git operations, PR delivery, and completion reporting |
| Research/planning agent | Read the issue and comments, investigate the repository and relevant sources, and produce an implementation plan |
| Code runner | Consume the saved plan, create the task branch, implement the solution with a configured coding backend, and run validation |
| Codex, Claude Code, or OpenCode | Configured backend for research/planning or implementation |
| Optional Ollama service | Model inference for a compatible coding backend |

The runner owns the full claim-to-finish lifecycle. Agent output supplies evidence
and context; the runner verifies command results, validation results, and PR
delivery before reporting success.

Research/planning and implementation are separate stages within the service, with
a persisted plan as their handoff. They do not require separate Compose services.
The code runner starts implementation only after the plan is saved and any
required user confirmations are resolved.

## Container and process layout

- Add a separately built `coding-runner` service and package. Keep `nq` independent
  of the runner and Django.
- Use the existing HTTP API at `http://nightly-queue:8000` on the Compose network.
  The runner has no direct database or Celery broker dependency.
- Process one item at a time initially. Claim only when capacity is available and
  the configured execution window is open and the global usage reserve permits it.
- Run as a non-root user with an isolated workspace per task. Do not mount the live
  application source, host home directory, or host Docker socket into the agent's
  environment.
- Persist execution metadata, logs, checkout state, and delivery results in a
  dedicated runner volume. Define retention and cleanup behavior.
- Install Git, GitHub tooling, and the selected coding backend. Start with the
  target projects' build dependencies; add project-specific images when needed.
- Pin coding tool versions in the image. Model authentication and selection remain
  backend-specific settings managed through the web UI.

## Task lifecycle

1. Check execution settings and runner credentials before claiming work. Require
   an open processing window and sufficient remaining usage under the global
   reserve policy described below; otherwise leave queued items unclaimed.
2. Persist a unique run ID, then call `POST /api/v1/items/claim/` with
   `execution_ref` set to `runner:<run-id>`. An empty queue returns `item: null`;
   wait for the polling interval before checking again.
3. Persist the item ID, claim token, and returned checkout settings. Use the
   top-level `repository_url`, `base_branch`, and `branch_name`. Resolve a blank
   base branch to the repository default and record the resolved branch.
4. For a GitHub item, fetch the current issue title, body, state, and **all comments**,
   following every page. Use the claimed issue repository and number, not a changed
   project URL. Save the context with comment authors, timestamps, source links,
   and fetch time. The imported body alone is not sufficient. If the issue is now
   closed or the full discussion cannot be read, stop before implementation and
   report the reason. Manual tasks use their title and instructions without
   requiring a GitHub issue.
5. Clone/fetch the repository and inspect the resolved base revision. Have the
   research/planning agent read the problem, full discussion, applicable repository
   guidance, relevant code, and tests. Research potential solutions and record
   supporting source links and unresolved questions.
6. Save an implementation plan for a reviewable PR and hand it to the code runner
   together with the task context and base revision. Follow the plan contract below.
   Surface blockers or required confirmations before running dependent work.
7. The code runner creates or reuses the returned task branch and implements the
   plan using the selected coding backend. Preserve prior work and an existing PR
   during retries. Do not discard changes or force-push automatically. Enforce the
   task timeout across research, planning, implementation, and validation.
8. Inspect the diff and run the plan's project validation on the task branch.
   Record commands and results. An agent's success message or exit status alone
   does not establish that the task is complete.
9. After required validation passes, commit changes, push the branch, and create
   or reuse a PR against the resolved base branch. Record the commit and real PR
   URL before reporting completion.
   Include the problem, implemented approach, validation evidence, and a link to
   the original GitHub issue when applicable. Failed validation must be fixed and
   rerun within the timeout or reported as a failed task.
10. Call `POST /api/v1/items/<id>/finish/` using the exact claim token. Send
    `status: succeeded` and the PR URL in `result_url` after verified delivery.
    Report known execution failures with `status: failed` and a concise
    `error_summary`. Preserve useful logs and any PR already created.
11. Continue while the processing schedule, global usage reserve, and task limit
    permit. Recheck the gates before each new stage or model invocation. If a gate
    closes during a run, save progress and wait at the next safe checkpoint.

Success means delivery for review. It does not mean CI passed remotely, the PR
merged, or the GitHub issue closed. The runner does not merge PRs automatically.
No-change results need an explicit policy before unattended operation; they must
not produce fabricated commits, PR URLs, or successful delivery reports.

## Plan and implementation handoff

Persist the plan with the run ID and expose it in the web UI. It must contain:

- The problem, expected behavior, and acceptance criteria derived from the task
  and issue discussion, including conflicting requirements or open questions.
- Research findings, relevant repository paths, supporting sources, and the
  chosen solution with any material tradeoffs.
- Concrete implementation steps and expected files to change, including migrations
  or configuration changes when needed.
- Required setup and validation commands, regression cases, and evidence needed
  before creating the PR.
- The originating issue URL when applicable, claimed repository/branch settings,
  inspected base commit, and context snapshot used to prepare the plan.

The code runner receives the saved plan and source context, rather than having to
reconstruct the research. Record material plan changes and their reason if
implementation reveals new constraints. If the base revision or issue discussion
changes before implementation or on retry, refresh the context and assess whether
the plan needs revision. Keep previous plan versions and validation evidence with
the run so recovery can resume from the last verified stage.

## Web UI settings and credentials

The web UI is the configuration home for all runner behavior. Add a **Coding
runner** section to Settings, with project overrides on project create/edit pages.
Store settings in Django and return an effective configuration through an
authenticated runner API. Routine runner configuration must not require editing
Compose files or container environment variables.

| Web setting | Planned behavior |
| --- | --- |
| Execution enabled/paused | Pausing prevents new claims and pauses current work at the next safe checkpoint |
| Processing schedule | Continuous execution or selected days and time ranges, explicit timezone, polling interval, and tasks per window |
| Global usage reserve | Optional minimum remaining-usage percentage (0–100%) for supported coding backends; at or below the threshold, block queue processing across all projects |
| Timeouts | Overall task timeout and stage limits for research/planning, implementation, and validation |
| Research/planning | Backend, model, and research/planning instructions |
| Implementation | Coding backend, model, and implementation instructions |
| Project overrides | Backend/model/instruction overrides for each stage, setup commands, and required validation commands |
| GitHub access | Credential selections for reading issues/comments and cloning, pushing, and creating PRs, separate from the issue-import token |
| Backend connections | Coding backend credential selections and any compatible Ollama endpoint/model configuration |
| Run retention | Retention and cleanup settings for workspaces, plans, and logs |

Keep execution scheduling separate from the existing 1h/4h/8h/12h/24h issue sync
setting. Show effective project settings and missing configuration before enabling
execution. Re-read execution settings before each claim and snapshot the effective
configuration with the run. Schedule, pause, and usage-reserve changes also apply
at the next gate check during a run; backend/model/instruction changes apply to
subsequent tasks. Project overrides cannot bypass the global schedule or reserve.

Manage runner credentials through the web UI's Credentials page and select them by
name in runner/project settings. Extend credential support for coding backends as
needed; the current UI only supports GitHub and GitLab providers. Keep issue-import
and runner credential selections separate so their permissions can differ.
GitHub runner access must cover the full issue discussion and repository/PR
operations required by the selected project.

The current API exposes neither runner configuration nor credential secrets.
Implement authenticated, scoped credential delivery for runner-selected secrets
before enabling this workflow. Never include them in ordinary queue/configuration
responses, saved plans, or UI readbacks. The runner must not receive the
application's credential encryption key or database credentials.

Deployment bootstrap remains infrastructure configuration: the queue URL, runner
API authentication, credential encryption key, and container/volume wiring must be
provisioned outside the UI. These establish the connection needed to read the
web-managed settings and secrets.

## Processing schedule and global usage reserve

Configure both controls in the web UI's **Coding runner** settings. Processing is
allowed only when execution is enabled, the current time is inside an allowed
window, the global usage reserve permits it, and task/capacity limits permit it.
Issue discovery continues on its separate schedule while processing is blocked.

The schedule must support selected weekdays and time ranges, including windows
that cross midnight, with an explicit timezone. For example, allow processing
Monday through Friday from 22:00 until 06:00 the following day. Define daylight
saving behavior and show the next opening/closing time in the UI. Outside an
allowed window, leave new work queued and pause existing work at a safe checkpoint
before starting another stage or model invocation.

Label the usage setting **Minimum remaining usage (%)** and make enforcement
optional. It is a reserve of the backend account's allowance, not a percentage to
spend and not a per-task token limit. A value of **40%** permits work while more
than 40% remains and blocks processing when **40% or less** remains: at most about
60% of the allowance is available to queue work, including any usage already
consumed elsewhere. Enabling a 0% reserve still blocks at exhaustion; disabling
the control removes this additional gate. A 100% reserve blocks processing.

Apply the single threshold globally across projects and the configured
research/planning and implementation backend accounts, such as Codex or Claude.
Evaluate each account independently; do not average percentages across providers.
If any participating account is at or below the reserve, block the whole queue
rather than switching accounts or providers to bypass it. When a backend exposes
multiple applicable allowance windows, each must remain above the threshold.

Backend adapters must provide a supported way to obtain remaining allowance and
its reset information. This is an implementation requirement, not a claim that
every backend currently exposes token balances. Use a provider's reported quota
or remaining-usage measure with its window and unit; task token counts alone do
not establish account allowance. If enabled enforcement cannot obtain a fresh,
usable reading for any participating account, block processing and show why.
Do not assume unlimited usage for an unsupported or unavailable backend.

Check usage before claiming, before each new stage or model invocation, and during
long-running agent execution where the backend supports it. A call already in
flight may cross the reserve; request a graceful pause and preserve the workspace,
plan, and claim at a safe checkpoint without starting further model work. The
reserve is a processing gate, not a guarantee of an exact token cutoff. Waiting
for a schedule or usage gate is a resumable pause, not task failure or success;
define how paused time is excluded from active execution timeouts.

Show the configured threshold, remaining usage per account/window, last refresh,
reported reset time when available, and blocking reason in the web UI. Recheck
blocked gates without claiming additional work. Resume saved work before new
claims once usage is strictly above the reserve and the schedule and other gates
allow processing, including after a provider reset or a user setting change.

## Task input and approval boundaries

Keep credentials and claim tokens out of agent prompts and published logs. Pass
task inputs as structured data or files, never as interpolated shell commands.
Repository and issue content must not grant access to other projects, host files,
or infrastructure. Treat issue bodies, comments, repository content, and research
sources as untrusted data, not authority to override runner or repository rules.
Always request user confirmation before running anything that arrived as data
from an untrusted source, including commands suggested in issue comments or
research results. A saved plan does not grant that confirmation. Surface the
proposed action and its source in the web UI and pause dependent work until the
user responds; do not bypass required approvals to keep an unattended run moving.

## Interruption and recovery

The current API has no heartbeat, expiring lease, item-detail endpoint, or requeue
endpoint. Interrupted items stay running until an operator stops the original
runner and agent, then uses **Requeue failed/interrupted work** in admin. Requeueing
invalidates the old claim token and preserves branch and checkout settings.

Persist the issue snapshot, plan versions, effective settings, current stage,
branch/commit, validation results, and PR URL before and after external operations.
Show research, planning, implementation, validation, delivery, and requests for
confirmation in the web UI without adding new queue statuses implicitly.
Persist schedule/usage pause reasons and checkpoints so restarts cannot bypass
the global gates or launch a duplicate execution when paused work resumes.
Distinguish implementation failure from completion-reporting failure: a delivered
PR must remain recoverable when the finish request fails. Do not rerun the agent
or create a second PR solely because completion reporting failed.

Do not blindly repeat claims or finish calls after ambiguous network timeouts.
The request may already have succeeded; repeated completion returns HTTP 409.
Before unattended use, implement an authenticated reconciliation API for a runner
to recover its own claims and confirm item status. Define and test restart and
cancellation behavior before adding automatic requeueing or lease expiry; prevent
two surviving agents from operating on the same task.

## Implementation order and acceptance

See the [gap analysis and feature-branch backlog](runner-feature-branches.md) for
the current-code comparison, branch dependencies, and per-branch acceptance
criteria. It splits the milestones below into reviewable changes and establishes
run ownership, recovery records, and approvals before live agent execution.

1. Add the runner package, image, Compose service, isolated volumes, and lifecycle
   loop. Prove authenticated connectivity without claiming production work during
   startup checks.
2. Add web-managed execution settings, project overrides, credential selections,
   migrations, and authenticated runner configuration, scoped credential delivery,
   and reconciliation endpoints. Test scheduling, pause behavior, configuration
   snapshots, overnight windows, and timezone/daylight saving boundaries
   independently of issue sync. Test usage above, exactly at, and below the reserve
   (including 40%); multiple accounts/windows; stale or unsupported readings;
   reset/resume behavior; and global enforcement across project overrides.
3. Implement full issue/comment retrieval, research, saved plans, and the explicit
   handoff to implementation. Test pagination, unavailable comments, closed issues,
   manual tasks, changed context, and plans with unresolved blockers. Implement one
   coding backend behind a common adapter interface, then add Codex, Claude Code,
   and OpenCode adapters as separately verified integrations.
4. Implement branch creation from the plan, implementation, validation, branch push,
   and repeatable PR creation with an original-issue link. Test using temporary
   local repositories and stubbed provider responses. Verify failed validation
   prevents successful delivery.
5. Add persistent context, plans, run logs, timeout handling, shutdown behavior,
   approval handling, reconciliation, and recovery. Test interruption after claim,
   push, PR creation, and completion reporting, including stale claims and ambiguous
   responses. Test schedule/usage pauses during a task and restart/resume without
   duplicate work or bypassing the reserve.
6. Add UI visibility for run stages, plans, validation, confirmations, and PR links.
   Verify schedules and the global usage reserve can be configured through the web
   UI and display their blocking reasons. Run a controlled
   end-to-end task in a designated test repository: queued → running → succeeded,
   with a real branch and PR. Verify a failing task reports failed with useful
   diagnostics. Verify restarts do not duplicate agent runs or PRs.

Built-in execution is ready only after the service and these behaviors are
implemented and verified. The existing queue API and issue importer remain the
foundation throughout this work.
