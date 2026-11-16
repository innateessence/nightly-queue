# Coding runner gap analysis and feature branches

Historical implementation backlog. For the current development direction and
feature branches, see the [autonomous AI queue pivot plan](autonomous-ai-queue-plan.md).
The gap analysis below describes its original baseline, not the current code.

This is the implementation backlog for [the runner architecture](coding-runner.md),
based on the code at `7665437`. Branch names below are proposed implementation
branches; this document does not create or implement them.

The application has a usable queue and issue-discovery foundation. The execution
service, its control APIs, and the entire research-to-PR workflow are missing.
Build on the existing queue services rather than moving execution into Celery or
adding database access to `nq`.

## What exists and what is missing

| Area | Evidence in the current code | Gap to the architecture |
| --- | --- | --- |
| Projects and queue | `projects/models.py`, `work_queue/models.py`, `work_queue/services.py` | Projects, manual/imported items, oldest-first claims, claim tokens, and frozen checkout settings exist. No durable execution-attempt model, stage history, context, plan, configuration snapshot, or validation evidence exists. |
| Issue discovery | `work_queue/github.py`, `work_queue/tasks.py` | Imports paginate issue lists, exclude PRs, and preserve issue identity/history. They do not fetch a current individual issue or its comments. The runner needs its own context retrieval without importing Django. |
| Claim and finish API | `work_queue/api.py`, `work_queue/urls.py` | Atomic item claims and token-checked completion exist. No item-detail, run lookup, reconciliation, progress, approval, or cancellation API exists. `execution_ref` is optional, unstructured, and not unique: repeating an ambiguous claim can claim another item. |
| Completion and retries | `work_queue/services.py`, `work_queue/admin.py` | Requeue invalidates the old token and preserves checkout/branch settings, but clears execution reference, timestamps, result URL, and error summary. Success accepts an empty result URL or any valid HTTP(S) URL; it does not establish PR delivery or validation. Separate run history must preserve evidence before these fields are cleared. |
| Settings | `credentials/models.py:APISettings`, `credentials/forms.py:APISettingsForm`, `projects/forms.py:ProjectForm` | Settings contain import credential selections and the issue-sync interval. There are no execution settings, project stage overrides, setup/validation commands, schedule, timeout, reserve, or retention controls. `Project.autonomous` currently controls imported-issue eligibility; manual tasks remain claimable independently. |
| Credentials and authentication | `credentials/models.py`, `credentials/encryption.py`, `nightly_queue/api.py`, `projects/api.py` | Encrypted GitHub/GitLab credentials and a shared bearer-token API exist. Credential secrets are correctly absent from ordinary APIs. There is no runner identity, per-run authorization, selected-secret delivery, or coding-backend provider support. A submitted execution reference is not proof of ownership. |
| Scheduling and usage | `nightly_queue/settings.py`, `work_queue/services.py:claim_auto_sync` | Celery schedules issue discovery; its greeting task is a demo. There is no execution-window evaluator, global account/window usage inventory, quota adapter, pause checkpoint, or task-window counter. The issue-sync timer cannot serve as the execution schedule. |
| Execution and deployment | `docker-compose.yml`, `Dockerfile`, `pyproject.toml` | Compose runs Django, PostgreSQL, Valkey, worker, and Beat. There is no runner package/image/service/volume or agent integration. The existing image uses a live source bind mount and is not the runner image. Packaging currently installs only the standalone `nq` CLI. |
| Operator UI | `credentials/templates/credentials/settings.html`, `work_queue/templates/work_queue/index.html` | The queue displays status, a result link, and errors. There are no run/plan views, stage evidence, approval requests, gate reasons, usage readings, or recovery controls. The documented private-LAN, single-user UI is the current access model. |
| Verification | `work_queue/test_api.py`, `work_queue/test_services.py`, `work_queue/test_concurrency.py`, `work_queue/test_auto_import.py`, credential tests, `nq/test_cli.py` | Tests cover existing queue concurrency, stale tokens, retry checkout snapshots, importer pagination, settings, and encrypted credentials. No runner tests exist. CI invokes `test.sh`; runner checks will need explicit integration. |

This is a source review, not a claim that the current test suite was run. Preserve
existing behavior and tests while adding runner-specific contracts. `AGENTS.md`
still describes the former `hello/` layout; the actual owners are `projects/`,
`credentials/`, and `work_queue/`.

## Boundaries and decisions to settle early

- Add a Django app, provisionally `runner_control/`, for runner settings, run
  records, plans, approval requests, and control APIs. Keep queue transitions in
  `work_queue/services.py` and encrypted secret storage in `credentials/`.
  Add a separate `coding_runner/` runtime with no Django, database, broker, or
  `nq` imports. Give it its own dependency manifest and image build.
- Django stores durable run metadata, context snapshots, plan versions, redacted
  events, and validation/delivery summaries. The runner volume holds checkouts,
  protected recovery state, and full local logs. Upload redacted display evidence
  through HTTP; the UI must not depend on sharing the runner filesystem.
- Keep queue statuses `queued`, `running`, `succeeded`, and `failed`. Model stages
  and waits on the run. An approval, schedule, or usage wait retains the claim;
  it does not finish or requeue the item. Preserve the existing admin recovery
  path and its requirement to stop the previous execution first.
- Keep existing CLI and general queue API behavior compatible. Add authorization
  for a bootstrap-provisioned runner identity, then enforce runner ownership on
  runner APIs. Apply stricter success evidence to runner-owned executions without
  silently changing generic clients. Do not infer that ownership from a string
  beginning with `runner:`.
- Establish the effective project configuration and all participating backend
  accounts before claiming. The current project list is insufficient for this.
  Include accounts introduced by project overrides in the global reserve check;
  an account below reserve blocks all projects. Snapshot effective execution
  settings atomically with a runner claim, using a configuration revision to
  detect changes between preflight and claim.
- Implement durable run identity and an operation journal before external side
  effects. Reconciliation must distinguish an unreceived request, an active
  claim, and a completed run. A lost response never authorizes another claim or
  another PR. Start with one runner controller and one task at a time; prevent
  overlapping controllers, including after a restart.
- A non-root container alone does not separate agent code from supervisor
  credentials or other task workspaces. Define an enforceable agent sandbox and
  command-approval boundary before selecting a live backend. Keep API tokens,
  claim tokens, Git credentials, and unrelated task files outside agent access.
  The supervisor performs credentialed Git/GitHub operations.
- Resolve backend authentication and allowance capabilities with a documented
  integration probe. This review establishes no claim about current Codex,
  Claude Code, OpenCode, or Ollama quota APIs. Unsupported usage reporting must
  block processing when reserve enforcement is enabled; token counts are not a
  substitute. Usage enforcement may be explicitly disabled in settings.
- Set explicit policies for DST, overlapping windows, task-window counting,
  paused-time accounting, approval invalidation, cancellation, and no-change
  outcomes in the branches below. Proposed first-release defaults: no automatic
  lease expiry/requeue, no forced Git resets/pushes, and no-change work reports
  `failed` with an explanation rather than fabricating delivery.

## Branch map

Dependencies are merge prerequisites. Create each branch from the updated
integration branch once its prerequisites land; use stacked branches only when
their shared contracts are already settled. All live execution remains disabled
until the controlled release check in branch 12.

| # | Proposed feature branch | Depends on | Reviewable result |
| --- | --- | --- | --- |
| 01 | `feat/runner-run-api` | Existing queue | Durable attempts, runner ownership, claim reconciliation, and evidence APIs |
| 02 | `feat/runner-settings` | 01 | Web-managed settings, project overrides, effective configuration |
| 03 | `feat/runner-credentials` | 01, 02 | Backend credential support and scoped selected-secret delivery |
| 04 | `feat/runner-service` | 01 | Separate image/service, persistent journal, authenticated startup probe |
| 05 | `feat/runner-execution-gates` | 02, 04 | Schedule, pause, global reserve, and capacity enforcement |
| 06 | `feat/runner-workspaces-approvals` | 02, 03, 04 | Isolated task workspace and enforceable human approval workflow |
| 07 | `feat/runner-context-plans` | 01, 03, 06 | Complete issue context, versioned plan contract, and plan view |
| 08 | `feat/runner-codex-backend` | 03, 05, 06, 07 | First backend adapter, capability checks, and saved research plans |
| 09 | `feat/runner-plan-implementation` | 05, 06, 07, 08 | Saved-plan execution, diff inspection, and recorded validation |
| 10 | `feat/runner-pr-delivery` | 09 | Verified commit/push/PR delivery and evidence-backed completion |
| 11 | `feat/runner-recovery` | 05–10 | Verified crash recovery, cancellation, and pause/resume |
| 12 | `feat/runner-operations` | 11 | Complete operator views, retention, CI, and controlled release checks |
| 13 | `feat/runner-claude-backend` | 12 | Separately verified Claude Code integration |
| 14 | `feat/runner-opencode-ollama` | 12 | Separately verified OpenCode integration and optional Ollama wiring |

Codex is a proposed first integration, not a dependency of the architecture.
If its supported authentication, tool controls, or pause behavior cannot meet the
contract, use the first backend that can and rename branch 08. Each additional
backend still needs its own verified integration. Branches 13 and 14 can proceed
independently after the first backend release; full planned backend coverage
requires both.

### 01 — Durable runs, ownership, and reconciliation

Add the `runner_control` app, migrations, and service/API contracts. Model an
attempt separately from its queue item, with runner ID, unique run UUID,
configuration/context/plan references, stage, wait reason, timestamps, and
append-only evidence. A fresh retry creates a new attempt; a restart resumes the
same one. Never persist plaintext claim tokens in display artifacts.

Extend the existing claim transaction for authenticated runner requests to link
the item and run atomically. Require a unique run UUID and allow at most one
active claim for the initial controller. Add owned-run/item lookup and
reconciliation. Define repeat-claim behavior so the same UUID cannot consume a
second item. Persist an empty-queue outcome as well; use a new UUID for a later
poll. Add authenticated, bounded, retry-safe progress/artifact writes.

Acceptance: test concurrent duplicate claims, lookup after a lost claim response,
finish reconciliation after a lost response, empty-queue retries, foreign-run
access, stale tokens after admin requeue, and duplicate evidence uploads.
Requeue must preserve prior run evidence and mark its ownership obsolete. No
claim tokens or credential values may appear in ordinary item/run responses.

### 02 — Runner settings and effective project configuration

Add a Coding runner section to Settings and stage overrides to project
create/edit pages. Include enabled/paused, continuous or weekday/timezone
windows, polling, tasks per window, task/stage timeouts, optional reserve,
research/implementation backend/model/instructions, credential selections,
setup/validation commands, and retention. Keep issue-sync settings independent.
Default execution to disabled.

Expose a versioned authenticated configuration API containing effective project
settings, credential references, required capabilities, and configuration errors.
Define inheritance for blank versus explicitly cleared values. Show effective
values and missing configuration in the UI. At this stage distinguish structural
validity from live backend readiness; neither is permission to execute yet.

Acceptance: test model/form/API validation, override inheritance, no secret
serialization, and atomic claim snapshots. Import interval changes must not alter
execution windows. Global pause/schedule/reserve changes are read at subsequent
gates; backend/model/instruction edits affect future tasks, preserving the active
run's snapshot. Update the project Autonomous help text so it describes current
discovery behavior accurately.

### 03 — Selected credentials and backend connections

Extend the encrypted credential model and forms for the chosen backend
authentication schemes, including any structured/session material actually
required. Add migration coverage and protected references. Keep runner issue-read
and repository/PR credential selections distinct from the import selection; do
not silently fall back to the import token or a project's generic credential.

Add a runner-only delivery endpoint that resolves selected credentials for an
authorized preflight account or owned active run. It must not decrypt arbitrary
credential IDs supplied by the caller. Authorize pre-claim usage reads from the
configured account inventory without granting all stored-secret access.
Return non-cacheable secret responses, redact error/log paths, and define rotation
and revocation behavior for active runs. Never deliver the Django encryption key
or database credentials.

Acceptance: selected credentials work; CLI credentials and unrelated runner/run
identities cannot retrieve secrets; unselected/revoked selections fail closed;
missing encryption keys and wrong provider/kind produce useful redacted errors.
Verify secrets stay out of settings responses, saved plans, UI readbacks, and
agent environments. Infrastructure still provisions runner API authentication.

### 04 — Separate runner runtime and persistent service

Add `coding_runner/`, its own dependency manifest, a separately built non-root
Dockerfile, and a `coding-runner` Compose service with a dedicated volume. Copy
runtime code into the image. Install pinned Git/GitHub tooling; add pinned coding
tools in their integration branches. Do not inherit the app source mount,
database environment, encryption key, broker access, home mounts, or Docker socket.

Implement an HTTP client with bounded requests and explicit handling for 401,
409, invalid responses, and ambiguous timeouts. Add atomic local journal writes,
protected ownership/claim state, a controller lock, bounded backoff, and graceful
shutdown hooks. Startup checks authenticate and read configuration; they never
claim work. The loop remains unable to start model work until later branches.

Acceptance: build and run the image without Django; verify user, mount, secret,
and network dependency boundaries; restart without losing journal state; reject
two controllers sharing execution ownership. Startup with an unavailable queue
must retry safely and leave all items queued. Existing `nq` installation and its
standard-library tests must remain independent of the runner.

### 05 — Execution schedule and global usage gates

Implement one reusable gate evaluator called before claims, stages, and model
invocations, plus periodic checks during supported long-running calls. Inputs
include current global controls, effective participating accounts, fresh usage
readings, capacity, and persisted task-window counts. Use fake adapters to test
the policy before branch 08 supplies a real backend. Publish readings, freshness,
reset information, next window boundary, and blocking reasons for the UI.

Support overnight windows anchored to their starting weekday. Set and document
DST and overlap behavior before coding: a proposed policy shifts nonexistent
boundaries to the first valid instant, opens at the earlier repeated boundary,
closes at the later repeated boundary, and counts overlapping windows as one
processing interval. Define a stable interval ID and persist counters across
restarts. For continuous mode, use a local-calendar-day task limit. Charge an
attempt once at claim, not again on resume; retain its original window identity.

Acceptance: test pause, outside-window work, midnight, DST changes, overlaps,
window limits, and restarts. Test reserve disabled and thresholds 0/40/100;
strictly above, exactly at, and below; multiple accounts/windows; override
accounts; stale/missing/unsupported readings; and reset/resume. Unknown usage
blocks when enforcement is enabled. No gate may switch accounts to bypass the
reserve. A waiting run retains its claim and resumes before any new claim.

### 06 — Workspaces and action approvals

Prepare isolated per-task workspaces from the claimed top-level repository and
branch settings. Resolve and record the remote default when base branch is blank.
Separate supervisor recovery state and credentialed operations from agent-visible
files. Implement an enforceable filesystem/process/tool boundary; acceptance must
test access rather than relying on prompt instructions. Reject backends that
cannot honor this boundary or the required action approval protocol.

Add approval request models, APIs, and a basic web inbox now, before any live
backend. Store the proposed action, source/provenance, workspace/base revision,
and relevant content digest, plus the user's decision. Require confirmation
before executing commands received from issues, comments, repository content,
or research. A plan is not authorization. Include repository setup scripts and
test commands in this rule. Backend tool calls must pass the same enforcement
boundary; a direct backend shell tool cannot bypass it.

Acceptance: an injected command remains data until approved; denial prevents its
execution; changing an approved action or its bound source invalidates approval.
No unattended auto-approval is allowed. Test cross-task file access, inherited
secrets, malicious argument values, and child-process containment. Pass structured
input and argument arrays; any explicitly requested shell interpretation must
itself be reviewed. Preserve approval waits across restarts with CSRF-protected
operator decisions under the existing UI access model.

### 07 — Complete issue context and versioned plans

Implement a runner-side GitHub context client for the claimed issue repository
and number. Fetch current issue fields and every comment page, recording author,
timestamps, URL, and fetch time. Do not import `work_queue/github.py`, which depends
on Django project validation. Share fixtures or a pure utility only if needed.
Manual items use their claimed title/instructions without an issue request.

Persist a context snapshot and a versioned plan schema covering problem,
acceptance criteria, findings/sources, relevant paths, chosen approach, file
changes, setup/validation actions, blockers, original issue, checkout settings,
and inspected base commit. Add APIs and a basic context/plan view. Validate the
handoff structurally; unresolved blockers prevent implementation. On retry or
handoff, compare current base/discussion with the snapshot, assess changes, and
retain superseded plans and reasons for revision.

Acceptance: test paginated/empty comments, unavailable pages, malformed data,
closed issues, manual work, changed project URL, and changed base/discussion.
Incomplete context cannot be treated as an empty discussion. Closed or unreadable
issues stop before implementation and report the reason. Never start
implementation before a valid plan is saved. This branch can use submitted test
plans; actual research generation arrives with branch 08.

### 08 — First backend and research/planning stage

Define a common backend interface for readiness/authentication, capabilities,
research, implementation, usage readings with account/window/unit/reset/freshness,
progress, and graceful interruption. Implement the proposed Codex adapter with a
pinned tool version and a documented integration probe. Record unsupported
features explicitly; reserve enforcement with unsupported quota readings blocks.
Model authentication and selection stay web-managed, outside image configuration.

Wire the research/planning stage to read the saved context and inspected checkout,
observe approvals/gates, investigate relevant code/sources, and submit a plan
through the contract in branch 07. Keep tokens out of prompts and backend logs.
Validate returned artifacts rather than trusting a free-form success message.

Acceptance: deterministic fake-process tests cover valid/malformed output,
authentication failure, tool approval requests, model errors, usage refresh,
timeout, pause, and shutdown. Run an explicitly authorized backend smoke check
before claiming the integration works. Prove research cannot invoke
implementation, publish changes, or bypass required confirmation. Test global
blocking across the first backend's distinct configured accounts.

### 09 — Plan-driven implementation and validation

Implement the stage machine from saved plan through branch creation/reuse,
implementation, diff review, and project validation. Recheck context and plan
validity before implementation; record material plan amendments. Use the task's
returned branch name and resolved base revision. Preserve existing work and stop
for conflicts; never reset or force-push automatically.

Run approved setup/validation actions under the workspace boundary, capture
command, revision, exit code, and redacted evidence, and inspect the resulting
diff. Revalidate after any further implementation change. Apply task and stage
budgets to active execution across research, planning, implementation, and
validation. Persist accumulated active time; exclude schedule, reserve, and human
approval waits. Stop supervised child process groups on timeout.

Acceptance: use temporary local repositories and fake backends for manual and
issue tasks. An agent's zero exit code without required validation cannot advance
to delivery. Failed checks may be repaired and rerun only within the remaining
budget. Test conflicts, unapproved commands, no-change outcomes, pause checkpoints,
and total budgets across stage transitions. No-change uses the explicit failed
outcome described above until a separate policy change is approved.

### 10 — Repeatable GitHub delivery and completion

After validating the final diff/revision, create a commit, push the task branch,
and create or reuse a PR against the recorded base. Search by actual repository,
head, and base; do not trust an arbitrary URL emitted by an agent. Preserve an
existing PR and branch on retry. Include problem, implementation, validation
evidence, and original issue link in the PR description. Never auto-merge or close
the originating issue.

Journal intent and observed results around commit, push, PR creation, and finish.
Record verified commit/head/base and real PR identity in Django before reporting
`succeeded` through the existing finish lifecycle. Enforce runner-owned success
requirements in server services as well as the runtime. Known failures report a
concise error and preserve any created PR. Reporting failure after successful
delivery must enter reconciliation, without rerunning implementation.

Acceptance: local Git repositories plus stubbed GitHub responses cover push
failure, existing PRs, wrong-repository/head URLs, failed validation, empty URLs,
and ambiguous create/finish responses. Repeat delivery must find the existing PR.
Generic queue API success behavior remains covered independently.

### 11 — Restart recovery, cancellation, and resumable waits

Complete the recovery coordinator using the journal and APIs built earlier.
Reconcile remote ownership and external effects before resuming the last verified
stage. A delivered PR with unconfirmed finish retries reporting only. Persist
wait reasons, approvals, process lifecycle, active-time accounting, branch state,
and unfinished operation records. Never start a new claim while owned work needs
reconciliation or awaits a gate.

Define cancellation as a recorded request followed by confirmed backend/child
termination and a final failed/cancelled explanation on the run; retain the
existing queue statuses. Fence obsolete writers and stop the previous execution
before another controller can resume. An expired heartbeat alone cannot prove an
old agent stopped, especially before GitHub side effects. Do not add automatic
lease expiry or requeue in this release. Keep uncertain ownership blocked for
operator reconciliation. If a backend invocation's outcome cannot be recovered,
require operator reconciliation rather than invoking the model again blindly.

Acceptance: inject termination and lost responses before/after claim, context or
plan upload, model execution, validation, push, PR creation, and finish. Test
schedule/reserve/approval pauses across restarts, cancellation while a process
runs, admin requeue with stale tokens, second-controller attempts, and the case
where local state is missing but Django has an owned run. Assert no duplicate
model execution or PR for already verified work and no gates bypassed on resume.

### 12 — Operator visibility, retention, and release verification

Complete run history/detail views showing context, plan versions, stages, waits,
approvals, validation, errors, commits, and PR links. Show effective settings,
quota per account/window, freshness/reset times, and why the whole queue is
blocked. Add recovery/cancellation controls and a visible distinction between a
delivered PR and pending completion reporting. Preserve the current private-LAN
access model; record that runner machine authorization is a separate boundary.

Implement web-managed retention for workspaces, uploaded artifacts, and logs.
Do not remove active/paused runs, pending reporting evidence, approval context,
or the data needed to reconcile a delivery. Define post-retention behavior for
history and broken/missing local volumes. Add disk-pressure behavior that stops
new claims with a useful reason rather than deleting recoverable work.

Extend `test.sh` and CI for the runner image, standard-library/runtime tests,
control APIs, migrations, and recovery scenarios. Update README, architecture
status, and stale AGENTS layout references; document bootstrap, upgrades,
credential permissions, volume backup, retention, and operator recovery. Include
screenshots for settings, run detail, approvals, and mobile views.

Acceptance: in a designated test repository, explicitly authorize any required
actions and verify a real queued → running → succeeded task produces a branch,
passing validation, and a PR. Verify a failed task has useful diagnostics;
restart after PR creation without producing a second PR; change pause/schedule/
reserve through the UI and observe safe resume. Only then make execution
available for normal use, with its enabled setting still opt-in.

### 13 — Claude Code backend

Implement Claude Code as a separate adapter using the shared contract. Pin the
tool, add web credential/model choices, document supported authentication and
quota capabilities, and verify action approval and process interruption. Keep
research and implementation backend selections independent.

Acceptance: run the adapter contract suite and an authorized smoke check. Test
mixed-backend stages, separate accounts, stale/unavailable allowance readings,
and global reserve enforcement without averaging or provider fallback. Complete
the controlled delivery/recovery scenario for this adapter before enabling it.

### 14 — OpenCode and optional Ollama

Implement OpenCode behind the same contract with pinned tooling and web-managed
provider/model settings. Add optional Ollama Compose wiring only for a supported
configuration; the service is infrastructure, while routine endpoint/model
selection belongs in the UI. Record capability limitations, including unsupported
account-allowance reporting, explicitly.

Acceptance: test endpoint/model validation, unreachable inference, tool approvals,
timeouts, pause/recovery, and mixed-backend stages. Unsupported quota readings
must block when the global reserve is enabled; a local endpoint does not imply
unlimited remaining account allowance. Run the adapter contract suite and an
authorized controlled delivery scenario before enabling the integration.

## Suggested delivery sequence

1. Merge 01, then 02 and 04 can proceed independently. Merge 03 after 02.
2. Build 05 and 06 independently once their prerequisites land. Review the
   ownership, schedule, secret, and action-approval contracts before proceeding.
3. Merge 07 → 08 → 09 → 10 for the first complete workflow. Keep it confined to
   development/test execution while recovery is still being verified.
4. Merge 11 → 12 to qualify the first backend release. Run the controlled release
   checks before enabling ordinary queue execution.
5. Develop 13 and 14 independently, each with its own integration evidence.

Every branch includes its own meaningful tests, model migrations where needed,
and documentation. Use Django `TestCase` for control APIs/models,
`TransactionTestCase` for migration/concurrency behavior, and `unittest` with
temporary repositories and fake HTTP/backend processes for the runtime. Run the
repository's Black, Ruff, mypy, migration, system, and applicable test checks on
implementation branches. Preserve the standalone CLI suite throughout. Live
provider calls and real PR creation are controlled integration checks, separate
from deterministic CI.
