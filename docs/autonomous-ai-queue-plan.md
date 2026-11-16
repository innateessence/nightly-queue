# vibearr development plan

Date: 2026-11-15. Baseline: `5905c0f` on `main`.
Planning branch: `plan/autonomous-ai-queue-pivot`.

The [MVP release contract](vibearr-mvp.md) defines the first self-improvement loop,
runtime choice, native evidence format, validation boundary, and release scenarios.
This document maps that contract to feature branches and the later product roadmap.

## Outcome and priority

Build **vibearr**, an *arr style app for vibe coding: configure repositories
and coding agents once, add work, and let background processing deliver tested
changes. Its first customer is `innateessence/nightly-queue`. This repository's
GitHub issues become its development backlog, with one feature per issue, branch,
and PR. The web queue provides scheduling, visibility, history, and controls.

Prioritize a working self-development loop over a redesign, additional providers,
or another layer of planning infrastructure. Start with Codex, then add native
Claude Code and OpenCode behind the same execution contract. Keep Django,
PostgreSQL, Celery/Beat, Valkey, the independent runner, and the standalone `nq`
HTTP CLI. No framework migration is needed to reach this outcome.

Remove human command and plan approvals from normal execution. Configured project
policy authorizes autonomous work inside an isolated task environment. A blocked
action produces a clear error or bounded retry rather than an approval inbox.
Automatic merge is part of the MVP: the next task must use code merged by the
previous task. Automatic deployment is a later feature; delivery, merge, and a
running application upgrade have separate evidence and outcomes.

This document supersedes the sequencing and approval requirements in
[the previous feature backlog](runner-feature-branches.md) for future pivot work.
It is a development plan, not a claim that autonomous execution works today.
This planning branch changes documentation only; it does not enable execution,
alter repository instructions, publish issues, or run paid agents.

## Current code and actual blockers

| Area | Current evidence | Pivot work |
| --- | --- | --- |
| Queue and discovery | `work_queue/services.py`, `github.py`, `tasks.py`; project Autonomous setting | Reuse imports and shared transitions. Add actionable issue selection, dependencies, and priority ordering; claims currently use oldest first regardless of priority. |
| Durable execution | `runner_control/services.py`, `coding_runner/controller.py`, `journal.py`, `recovery.py` | Reuse run identity, fencing, journal, cancellation, and reconciliation. Do not rebuild a second queue or run engine. |
| Approvals | `runner_control/approvals.py`, `coding_runner/approvals.py`, `sandbox.py`, `implementation.py` | Every proposed command requires a human decision today. Replace this dispatch dependency and its waiting states with policy-based execution. |
| Backends | `codex_backend.py`, `claude_backend.py`, `opencode_backend.py` | These are candidate provider API transports, not native coding CLI integrations. All advertise `native_tools_supported: False` and `ready: False`; changing a readiness boolean cannot make them usable. |
| Lifecycle | `coding_runner/lifecycle.py`, `research.py`, `implementation.py` | Current readiness rejects native tools and requires structured proposals. Introduce a native agent session path rather than forcing real CLIs through the proposal protocol. |
| Isolation and release | `coding_runner/sandbox.py`, `release.py`, runner image and Compose profile | Existing documentation records a failed namespace probe in the hardened container. Resolve the deployed execution boundary and its tests before enabling a native agent. |
| Delivery | `coding_runner/delivery.py`, `runner_control/delivery.py` | Retain verified branch/commit/PR evidence and idempotent reporting. Add remote CI tracking and automatic merge separately. |
| Product | Settings, project overrides, runs, plans, retention, credentials already exist | Simplify the happy path and expose background activity after execution works. Old backlog descriptions of these features as missing are historical. |

## Operating contract

1. GitHub owns issue intent, discussion, and closure. PostgreSQL owns execution
   attempts, effective settings, synchronization cursors, and delivery evidence.
   Local runner storage owns workspaces and protected recovery state.
2. Once a repository and permitted issue authors are configured, new open issues
   from those authors are eligible automatically. Start with the repository owner
   for this app's backlog. An optional `vibearr:hold` label excludes work; a `ready`
   label or a per-issue click is not required by default. Other authors' issues
   remain visible but do not execute under this initial policy.
3. Issue content, comments, repository files, and research are task data. They
   cannot extend project permissions, select arbitrary credentials, or change
   deployment policy. A permitted issue author does not make every comment or
   shell snippet authoritative. Execution is authorized by the configured policy.
4. The agent may inspect, edit, install dependencies, and run project commands
   inside its task environment without human prompts. The supervisor owns claim
   tokens and GitHub writes. Agent environments receive only the backend access
   required by the selected integration, with its actual exposure documented.
5. Start with one active task and one feature branch at a time for this repository.
   Each branch starts from the configured base after prerequisites merge. Plans
   and acceptance criteria are saved artifacts, not permission checkpoints.
6. Keep the existing queue statuses. Track execution stage, wait reason, retry
   eligibility, delivery, and merge state on the run. A delivered PR is distinct
   from a merged change and from a running application upgrade.
7. Pause, cancel, schedules, timeouts, disk limits, and configured usage reserves
   remain automatic execution controls. Unsupported quota telemetry must not be
   invented; reserve enforcement remains optional and blocks when enabled without
   a usable reading. Use wall-clock and attempt caps regardless of provider quota.
8. Unknown execution or delivery outcomes reconcile before retrying. Never start
   a replacement agent just because a heartbeat expired. Never claim success from
   a model's final message without checking the workspace and validation results.

## Feature branches and dependencies

Only the planning branch is created by this change. The branches below are future
features, each with its own GitHub issue and PR. Create each from updated `main`
after its dependencies merge; do not accumulate all features on this plan branch.
The numbered IDs are planning identifiers, not existing GitHub issue numbers.

| ID | Priority | Feature branch | Depends on | Result |
| --- | --- | --- | --- | --- |
| 00 | P1 | `feat/vibearr-rename` | — | Consistent product, CLI, package, and deployment naming with upgrade compatibility |
| 01 | P0 | `feat/autonomous-execution-policy` | — | Remove human approval requirements from configured autonomous execution |
| 02 | P0 | `feat/isolated-agent-runtime` | 01 | A usable, tested execution boundary for native agent processes |
| 03 | P0 | `feat/native-codex-runner` | 02 | Real Codex CLI sessions with noninteractive operation |
| 15 | P0 | `feat/isolated-project-validation` | 02 | Trusted test profile, disposable PostgreSQL, and validation evidence outside the live stack |
| 04 | P0 | `feat/unattended-issue-to-pr` | 03, 15 | Complete background task execution, validation, and repeatable PR delivery |
| 05 | P0 | `feat/github-work-selection` | 01 | Author filtering, dependencies, holds, and deterministic priority |
| 06 | P0 | `feat/self-development-bootstrap` | 04, 05 | This repository consumes its own feature issues automatically |
| 07 | P0 | `feat/automatic-pr-merge` | 06 | Passing PRs merge and unlock dependent backlog work; completes the MVP |
| 08 | P1 | `feat/native-claude-runner` | 06 | Native Claude Code using the same task lifecycle |
| 09 | P1 | `feat/native-opencode-runner` | 06 | Native OpenCode using the same task lifecycle |
| 10 | P1 | `feat/arr-queue-dashboard` | 06 | Appliance-style queue, Activity, History, and System views |
| 11 | P1 | `feat/github-work-authoring` | 06 | Web-created GitHub work and bounded agent-created follow-ups |
| 12 | P2 | `feat/automatic-self-updates` | 07 | Deploy merged versions and recover from failed upgrades |
| 13 | P2 | `feat/parallel-project-workers` | 07 | Bounded concurrency across repositories |
| 14 | P2 | `refactor/retire-legacy-approval-stack` | 07, 08, 09 | Remove obsolete runtime and approval plumbing after migration |

The MVP consists of 01–07 and 15. Feature 15 keeps a new ID so existing plan
references do not change. The rename in 00 can land independently and does not
delay execution work. Branch 05 can proceed after 01 while runtime work proceeds
through 02, then 03 and 15, then 04. Features 08–11 and the full rename do not delay
the Codex loop or automatic merging. Dependencies describe merge order, not a
request to use multiple development agents now.

### 00 — Rename the application to vibearr

- Use **vibearr** throughout the web UI, page titles, navigation, app metadata,
  documentation, screenshots, examples, and release artifacts. The name reflects
  an *arr style application for vibe coding. README branding starts on this
  planning branch; functional names change in this feature.
- Rename the CLI command and distribution, Django project/configuration modules,
  runner package/distribution where applicable, container services and images,
  default database names, environment variables, and application data paths.
  Define a single naming map in the PR so install, development, CI, and deployment
  instructions agree. Keep migration import paths working and persisted labels
  stable where changing them would strand existing data.
- Provide a documented transition from `nq`, `NQ_*`, and existing deployment names:
  retain CLI/configuration aliases for a defined migration period, give new names
  precedence, and report conflicting configuration. Keep existing tokens and the
  encryption key valid; renaming must not reset authentication or credentials.
- Preserve PostgreSQL and runner volumes explicitly during the Compose rename.
  Changing a service, Compose project, or default database name must not create
  an apparently empty installation. Migrate protected runner paths with ownership
  and active-run reconciliation intact; never start a second controller against
  copied state. Update backup, restore, and rollback instructions.
- Track a GitHub repository rename separately from local naming. Update canonical
  URLs, badges, issue templates, package links, and bootstrap defaults when that
  rename is authorized. Existing issue, PR, repository, and delivery identities
  must reconcile across redirects without importing duplicate work.
- Acceptance: a fresh vibearr installation and an upgrade with existing queue
  items, encrypted credentials, run history, and runner state both work. New and
  compatibility CLI/configuration names are tested. CI, package installation,
  Compose wiring, backup/restore, and all internal documentation links pass.

### 01 — Autonomous execution policy

- Replace mandatory per-command browser decisions with a versioned project policy
  that grants workspace execution and defines network, resource, and delivery
  limits. A project-level execution enable switch is configuration, not a recurring
  approval workflow. Existing disabled projects stay disabled during migration.
- Update `AGENTS.md`'s rule requiring confirmation for all untrusted-source
  execution to describe bounded autonomous execution and rejection of permission
  changes embedded in task data. Update the architecture and operations docs in
  the same PR. The current rule remains in effect until that change lands.
- Define the native execution policy and version 2 delivery evidence contract
  described in the MVP. Remove native configuration's `command_approval` and
  mandatory separate research-stage capability requirements. Ordinary setup,
  implementation, and validation use that policy rather than a per-command broker.
  Keep legacy proposal execution disabled; do not refactor every old proposal loop
  before implementing native sessions. Do not manufacture approval records.
- Migrate pending approval waits to an explicit stopped/needs-retry outcome;
  preserve historical decisions. Existing approved actions must not silently run
  after an upgrade. Hide creation/decision controls for autonomous projects.
- Acceptance: configuration/API contract tests distinguish native policy from
  legacy approvals; forbidden capabilities fail without opening an inbox; stale
  claims and changed policy cannot authorize execution; upgrades preserve history
  and stop pending legacy actions. Actual native dispatch is proven in 02–04.

### 02 — Isolated native agent runtime

- Implement the MVP's fixed OCI executor service using Docker on a dedicated
  Linux runner host. Do not require nested bubblewrap. Prove Git, Python 3.12,
  dependency installation, resource limits, restart-stable container identity, and
  full descendant termination on the actual target host. Pin the task image;
  branch 03 adds the native agent binary and verifies its runtime requirements.
- Keep the web app and supervisor outside agent workspaces. Do not expose the
  production checkout, application database, encryption key, Docker socket,
  unrelated tasks, or supervisor journal to agent processes. Give agents their own
  disposable Git metadata and import file changes into a separate trusted checkout;
  publishing remains supervisor-owned. Only the trusted executor has engine access.
- Separate dependency setup from credentialed model traffic. Define enforceable
  egress and private-network restrictions and explicitly test the chosen model
  auth mechanism. Native tools cannot inherit unrestricted supervisor secrets.
  A model credential available to a CLI may be exposed to that CLI's tools unless
  an actual broker isolates it; document and verify this boundary.
- Replace the old approval-bound release path with automated, versioned runtime
  qualification: real boundary tests, pinned-image provenance, auth diagnostics,
  and revocation on material runtime changes. Keep failure diagnostics actionable;
  a readiness switch alone cannot bypass a failed environment.
- Acceptance: actual image/host tests prove allowed writes and installs, denied
  access to host/control-plane files and networks, resource limits, cancellation
  of all descendants, and recovery after restart. The explicit deployment boundary
  verifier must test the selected runtime, not a retired namespace assumption.

### 03 — Native Codex adapter

- Add a separate native-session backend contract: prepare, start, stream events,
  inspect outcome, cancel, and reconcile/resume when supported. Keep legacy API
  proposals explicitly distinct while callers migrate.
- Pin and invoke the actual Codex CLI in noninteractive mode using argument
  arrays and prompt files/stdin. Verify exact flags against that pinned version;
  disable interactive approval prompts within the task boundary. Do not build
  another general shell/edit agent loop on top of raw model API calls.
- Normalize progress, errors, session identity, changed files, and available usage
  into existing run artifacts. Check usable authentication without assuming an
  interactive login, API key, and subscription session are interchangeable.
- Support dedicated Codex API-key authentication first. Subscription sessions are
  an additional capability, not a bootstrap prerequisite. Use a clean per-run
  configuration and no inherited plugins/MCP connections; capability diagnostics
  must work before a production run has qualified, without circular release gates.
- Acceptance: fixture processes test parsing, output limits, missing auth, hangs,
  and signals; a real bounded task edits a temporary repository, runs a test,
  exits without prompts, and records its genuine session/result. Interrupt and
  restart without duplicating an unknown remote invocation.

### 15 — Isolated project validation

- Build the trusted validation manifest and pinned Python 3.12/Git test image for
  this repository before the delivery lifecycle depends on it. Use a fresh
  PostgreSQL sidecar and run-specific credentials, volumes, and network. The
  executor launches resources; candidate code receives no engine socket.
- Cover formatting/lint/types, migration/system checks, Django tests, CLI tests,
  portable runner tests, and Git acceptance. Run real boundary qualification on
  the executor host. Select commands from the configured immutable profile, not
  candidate-edited scripts. Dependency changes rebuild a candidate image in an
  isolated executor build phase; production dependencies never change in place.
- Emit profile/image/tree digests, required command IDs, actual outcomes, and log
  references for version 2 delivery evidence. Run checks without model/GitHub
  credentials. Clean up only run-owned disposable resources after recording results.
- Add `.github/workflows/vibearr-validation.yml` with workflow `Vibearr validation`
  and job `Validate candidate`, using the same trusted profile. Keep the current
  `Run test.sh` check until its full coverage is deliberately consolidated later.
- Acceptance: a known passing candidate passes, a deliberate failing test fails,
  candidate edits cannot silently skip required checks, repeated runs have no DB
  state leakage, and the live app/database/runner are unaffected. CI runs the same
  profile; demonstrate the whole path without any native agent or paid model call.

### 04 — Unattended issue to PR

- Add a native lifecycle path: claim → fresh issue/context → isolated checkout →
  agent session → validation → push/PR → report. Keep lightweight plan artifacts
  and acceptance criteria; do not require a separate research model invocation
  for every straightforward task.
- Reuse frozen checkout settings, run fencing, scoped credentials, operation
  journals, existing Git delivery, redaction, schedules, and retention. Native
  agent output must not skip independent configured validation or diff inspection.
- Implement version 2 delivery validation on both supervisor and server, including
  `runner_control/delivery.py`. Bind the tested tree, configured validation profile,
  native session, pushed head, and PR identity; preserve legacy historical readback.
  The current validator's consumed-approval/plan-handoff requirements do not apply
  to native sessions. Reject incomplete, mixed-version, or agent-attested evidence.
- Implement at most two repair attempts per run for failed validation initially.
  Retry transient transport errors with bounded backoff; reconcile ambiguous
  paid calls, process execution, push, and PR creation before doing anything again.
- Distinguish failed, canceled, delivered, and no-change outcomes. In the MVP,
  no-change work fails with an explanation; it never invents a commit or PR.
- Add the minimum missing visibility to existing run pages: stage, age/heartbeat,
  wait reason, repair count, validation outcome, and PR/merge links. Pause/cancel
  and explicit retry are usable without waiting for the later dashboard feature.
- Acceptance: a real issue produces one branch and one PR without command/plan
  approval; a failed check prevents successful delivery; restart after claim,
  agent completion, push, PR creation, and finish reporting creates no duplicates.
  Pause/cancel and usage/schedule waits preserve ownership and useful diagnostics.

### 05 — Actionable GitHub backlog

- Extend imported metadata with stable repository/issue identity, author, labels,
  upstream update time, and dependencies. Keep import and claim decisions in
  shared services. Poll on a configurable short interval with rate-limit handling;
  retain scheduled reconciliation even if webhooks are added later.
- Apply project-configured author filtering to automatically admit owner issues.
  Exclude closed issues, PRs, held work, existing deliveries, and unmet dependencies.
  Before starting, refetch issue state and record an immutable context revision.
  Later edits do not silently mutate an active run's instructions.
- Use GitHub repository/user IDs as stable identity, a 60-second default polling
  interval, conditional requests, a per-project sync lock, and rate-limit backoff.
  Define label/dependency/branch syntax, initial backlog cutoff/backfill, and
  fresh-read failure behavior as in the MVP.
- Order high/normal/low priority, then creation time and ID; define explicit label
  mapping for imported issues. Detect missing/cyclic dependencies and display the
  reason. A prerequisite is fulfilled by recorded merge evidence, not simply by
  having a PR or an issue that somebody closed without completing the work.
- Before feature 07, reconcile manually merged prerequisite PRs so dependent work
  can proceed. Closed-unmerged PRs stop with a visible reason. No automatic requeue
  on reopen; an explicit retry creates a new attempt and preserves delivery history.
- Apply source eligibility consistently to UI counts and generic/runner claims.
  The built-in runner also filters valid native configuration; generic clients
  retain manual-task compatibility. Skip invalid runner project configuration
  rather than starving valid work; preserve genuinely global execution gates.
- Acceptance: author and hold filters, pagination, priority ties, stale updates,
  cycles, repository changes, duplicate imports, concurrent claims, and manual
  tasks all have explicit tests. Ineligible backlog entries cannot starve ready work.

### 06 — Bootstrap this repository as its own customer

- Add an idempotent bootstrap command and short setup guide for
  `innateessence/nightly-queue`. Configure its base branch, permitted owner,
  Codex connection, one active worker, execution schedule, and test profile.
  Retain separate issue-import and delivery credentials and show missing setup
  in one diagnostic view. Never overwrite existing secrets or project choices.
- Convert feature sections in this plan into one issue each, with the proposed
  branch, prerequisites translated to real issue links, acceptance criteria, and
  test expectations. Use a stable plan feature key to avoid duplicate issues.
  During implementation, publish only with the user's authorization to write to
  GitHub; this planning change publishes nothing.
- Select feature 15's existing validation profile during bootstrap. Do not defer
  isolated testing until this branch or launch the live-stack `test.sh` from agents.
- Add a repeatable acceptance driver/report for the MVP's two dependent issues,
  deliberate validation failure, restart points, and exact-head checks. This branch
  proves discovery/execution/delivery; 07 extends the same driver to real merges.
  Seed tools mark already merged prerequisites complete, so bootstrapping never
  waits for the queue to implement its own missing execution prerequisites.
- Acceptance: an owner-created issue for a small real improvement is discovered,
  implemented, tested, and delivered without intervention; a second independent
  issue runs next. Restart the service during one run and reconcile correctly.
  Measure issue-to-start and issue-to-PR times and record failures honestly.

### 07 — Automatic merge and issue completion

- Add a project delivery mode: PR delivery or automatic merge. Enable automatic
  merge for this repository after required checks are configured. Do not add a
  human review requirement to the autonomous path; configure GitHub repository
  rules consistently rather than attempting to bypass required reviews.
- Keep the run active through remote CI and merge in automatic-merge mode, using
  its existing claim for scoped secret delivery. Finish only after verified merge
  and idempotent completion reporting. PR-only mode still finishes at delivery.
  Include both outcomes in native evidence; do not overload a PR URL as merge proof.
- Track remote check runs and required status contexts for the exact PR head.
  Merge only the expected repository/base/head with all required checks passing;
  missing, pending, failed, or stale results are not success. Serialize merges
  per repository and revalidate after base changes with bounded repair attempts.
- Bootstrap must detect the current empty required-check list and require a
  nonempty configured set with producer/workflow identities. Use an expected-head
  SHA guard and squash merge; the app's own merge loop does not require GitHub's
  separate auto-merge setting. Bound CI waits and share repair limits across local
  tests, remote tests, and changed-base repairs.
- Verify that the delivery actor has no ruleset bypass; explicitly address the
  current admin-role bypass during initial setup rather than treating an owner's
  token as permission to ignore branch rules.
- Use verified merge identity to complete the issue, remove the branch according
  to policy, and unlock dependents. Use retry-safe status comments with a stable
  run marker. A delivered-but-unmerged run remains visible and reconcilable.
- Acceptance: head changes invalidate old checks; permission errors, conflicts,
  required reviews, and lost merge responses produce accurate states; retries do
  not merge a different commit, close unrelated issues, or duplicate comments.
- Release acceptance: the real two-issue scenario merges both changes without
  human gates; the second starts from a base containing the first merge. Every
  required evidence row in the MVP release contract passes before calling it ready.

### 08 — Native Claude Code adapter

- Implement the native session contract using pinned Claude Code programmatic
  execution, documented event output, explicit noninteractive tool permissions,
  per-run authentication, cancellation, and session recovery where supported.
- Reuse runtime, budgets, validation, and delivery; do not translate Claude Code
  into the existing Messages API proposal adapter. Choose backends per project
  or task with a frozen selection and visible configuration.
- Acceptance: pass the common adapter conformance suite and real issue-to-PR,
  cancellation, missing-auth, and restart scenarios. Match genuine usage units;
  unsupported allowance reporting cannot masquerade as a remaining percentage.

### 09 — Native OpenCode adapter

- Run pinned OpenCode programmatically in a per-run session/server environment;
  isolate its state and tools from unrelated projects. Configure permissions to
  finish or fail without interactive questions. Support one proven provider first;
  optional Ollama follows the same contract when available.
- Reuse the native session interface and common checks. Preserve configured
  backend choice on recovery; automatic provider switching is not a shortcut
  around exhausted allowances or a failed run.
- Acceptance: the common conformance suite and a real issue-to-PR run pass;
  provider/model errors and server reconnection cannot reuse another run's session.

### 10 — *arr style operations UI

- Evolve existing Django templates and static assets: Queue for ready/held work,
  Activity for stages and bounded live logs, History for attempts and PR/merge
  outcomes, and System for connections, disk, health, and next processing window.
- Make connection setup, project policy, backend selection, schedules, and default
  validation commands understandable in the web app. Surface why work is waiting
  and offer pause, cancel, retry, priority, and hold controls. Remove approval inbox
  navigation after the migration; errors link to diagnostics instead.
- Acceptance: screenshots and browser tests demonstrate configuration → queued
  issue → activity → tested PR/merge. All views escape task/log content, redact
  secrets, and enforce existing CSRF protection on mutations.

### 11 — GitHub work authoring and bounded follow-ups

- For GitHub-backed projects, let the queue form create an issue and import it
  immediately. Use a durable operation key and reconcile ambiguous create results;
  retain local manual tasks for projects that do not use GitHub as their backlog.
- Allow agents to propose follow-up issues through a supervisor-owned API with
  repository binding, deduplication, parent run, and a small per-run limit.
  Configure whether agent-authored follow-ups execute automatically; cap recursion
  depth and total generated work so self-development cannot create endless chores.
- Acceptance: retries and sync do not duplicate issues; generated issues respect
  project policy and budgets; external comments cannot redirect publication to
  another repository or turn a failed task into an unbounded work generator.

### 12 — Automatic self-updates

- Publish versioned application and runner images from merged commits. A separate
  narrowly configured deployment component, outside agent workspaces, pulls a
  selected release channel. Do not give agents the production Docker socket.
- Drain active work, back up database and runner state, apply compatible migrations,
  replace services, and run health checks. Record deployed commit/image identity.
  Failed health checks restore the prior compatible deployment; irreversible schema
  changes require a documented restore path and cannot promise automatic rollback.
- Acceptance: a merged self-improvement becomes the running version automatically;
  in-flight tasks survive the drain/restart; a failed upgrade recovers without
  launching a second controller or mixing incompatible server/runner versions.

### 13 — Bounded concurrency

- Add configured worker capacity across repositories and shared backend-account
  budgets. Retain one active implementation and serialized delivery per repository
  initially; add same-repository concurrency only with explicit branch/base logic.
- Acceptance: concurrent claims cannot duplicate work, one noisy project cannot
  starve others, shared usage limits apply across workers, and orphaned processes
  cannot acquire replacement ownership simply by timing out.

### 14 — Retire legacy approval and proposal infrastructure

- Remove unused approval creation/decision APIs, inbox templates, obsolete broker
  paths, and proposal-only provider workers after the native adapters are proven.
  Preserve necessary historical records through a deliberate archive/schema
  migration; do not edit already-applied migrations to erase old approvals.
- Consolidate architecture and operations documentation around the implemented
  lifecycle. Remove obsolete release evidence dependencies without removing
  working boundary tests, authentication, ownership fencing, or recovery journals.
- Acceptance: existing data upgrades, historical runs remain readable, ordinary
  APIs and `nq` remain compatible, and no live code references a human approval wait.

## Validation and delivery discipline

Every feature PR lists its concrete behavior, migrations/configuration changes,
and evidence. Use Django `TestCase` for services/API behavior, migration tests for
state transitions, and standard-library `unittest` for the independent runtime
and CLI. Follow repository Black, Ruff, mypy, migration, and system checks.
Run appropriate focused tests while developing and required CI before merging;
deployment boundary tests run explicitly on the target supported environment.

Maintain a common native-adapter conformance suite plus real acceptance per
backend. Stubs prove protocol handling; they do not prove authentication, sandbox
compatibility, cancellation, or unattended tool execution with the real CLI.
Record exact versions and environment. Do not claim live qualification from
portable test success or a model-generated success summary.

## Milestones

- **Identity, branch 00:** the application ships as vibearr with a tested migration
  from the previous command, configuration, package, and deployment names.
- **M1, branches 01–04 plus 15:** one real Codex task becomes a tested PR without any human
  command or plan approval. This is the first execution milestone.
- **M2, branches 05–06:** new owner issues in this repository are picked up in the
  background and delivered repeatedly. Use this loop to implement later features;
  the execution, selection, validation, and bootstrap branches must first land
  through ordinary development.
- **M3, branch 07:** passing self-development PRs merge automatically and unblock
  dependent features. This is the MVP release: the full two-issue scenario and
  failure/recovery matrix pass. No per-PR approval is part of the happy path.
- **M4, branches 08–11:** Codex, Claude Code, and OpenCode share one queue, with
  usable appliance controls and GitHub-backed work creation.
- **M5, branches 12–14:** merged changes update the running app, capacity grows,
  and obsolete approval infrastructure is removed.

## Provider references

These official references were checked while preparing the plan. They establish
programmatic entry points, not compatibility with this app's current runtime.
Recheck CLI flags, auth, permissions, output, and resume behavior against the
version pinned in each adapter feature.

- [Codex noninteractive execution](https://developers.openai.com/codex/noninteractive/)
  documents `codex exec` for scripted operation.
- [Claude Code programmatic execution](https://code.claude.com/docs/en/headless)
  documents `claude -p`, structured output, and tool permission configuration.
- [OpenCode CLI](https://opencode.ai/docs/cli/) documents `opencode run` for
  automation; [permissions](https://opencode.ai/docs/permissions/) describes its
  tool permission configuration.
