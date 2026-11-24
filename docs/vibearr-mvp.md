# vibearr MVP: the first self-improvement loop

> Historical implementation reference. Deployment and authentication instructions
> here are superseded by the [three-container runtime](docker-only-runtime-plan.md)
> and [current operations](docker-operations.md). Do not start the old worker fleet,
> use model API keys, or reuse its qualification evidence for the integrated runner.

This is the release contract for the first usable vibearr, alongside the
[feature branch plan](autonomous-ai-queue-plan.md). It describes work to implement,
not acceptance results already obtained. Each section names the feature that
owns it so an implementation issue can be written without inventing new policy.

The [Codex-first software factory plan](software-factory-plan.md) defines the next
runner milestone: intake, saved specification, build/check/review rounds and a
recorded decision before delivery. The runtime MVP below remains the prerequisite;
F1–F6 extend its two-issue acceptance to the full Codex role pipeline. Claude Code
and OpenCode qualify against that same contract in F7/F8. These stages are planned,
and this revision does not enable execution or change deployment qualification.

## What counts as an MVP

The [Docker-only runtime contract](docker-only-runtime-plan.md) is mandatory.
Every service, coding CLI, test, migration, qualification, browser, backup and
recovery tool runs in Docker. No host-installed runtime/tools, systemd/cron,
lingering, host configuration changes, host socket/API credentials, privileged
containers, host namespaces or arbitrary host path mounts. Compose owns service
lifecycle and named volumes. This supersedes previous host-executor signoffs.

The current deployment code must be simplified to four always-on containers:
app, production PostgreSQL, independent runner and one execution worker.
[S1–S4](docker-only-runtime-plan.md#required-simplification-branches) replace the
old topology-specific branch sequence; D1–D4 remain engineering acceptance
categories. No Celery/Beat/Valkey, permanent gateway/account/proxy/test-DB sidecars
or mandatory startup-job containers are part of the release target. Keep claims
disabled; a plan change is not permission to resume
paid execution or deploy an incomplete replacement.

An owner creates two small development issues in this repository. The second
depends on the first. With the services running, vibearr discovers the issues,
implements the first, independently validates it, and creates a PR with evidence.
A human reviews the current PR and required checks, then merges it. Read-only
reconciliation verifies that merge before the second starts from the updated
default branch; a human also merges the second PR. No command approval, plan
approval, ready label, or service restart is needed between the two issues.
Failures remain visible and do not block unrelated
eligible work forever.

Use `delivery_mode: "pull_request"` and keep automatic merge disabled throughout
the MVP and factory rollout. Feature 07 is deferred until sustained operation
demonstrates maturity and a human explicitly approves a separate limited rollout.
Agent review and a readiness verdict never authorize merging.

The first release supports one repository, one active run, native Codex with an
explicitly selected verified connection, continuous processing and GitHub polling.
Account lifecycle runs inside the runner and fits the same four-container budget.
Existing manual tasks,
schedules, pause/cancel, and optional usage reserves keep working. Claude Code,
OpenCode, additional repositories, and the full *arr dashboard follow this loop.

Merged source improvements are available to the next task immediately. Updating
the running web app and supervisor is a separate release/deployment operation;
automatic self-updates remain feature 12. Do not call a merged PR a deployed
upgrade. The MVP must be capable of implementing feature 12 through its own queue.

## Build order and ownership

Compact-stack work S1–S4 is release-blocking; the table retains original feature
ownership, not permission to accept a host-based implementation.

| Order | Feature | Required result before moving on |
| --- | --- | --- |
| 1 | 01 — execution policy | Native-run configuration and evidence contracts; old approval waits cannot execute |
| 2 | 02 — isolated runtime | Real task launch, status, logs, stop, restart reconciliation, and isolation in the Docker-only worker stack |
| 3a | 03 — native Codex | One genuine noninteractive editing session with usable auth and a recorded outcome |
| 3b | 15 — isolated validation | A disposable PostgreSQL-backed test profile usable without an agent or Docker socket inside the task |
| 3c | 05 — issue selection | Only permitted, ready, dependency-satisfied work can be claimed |
| 4 | 04 — unattended execution | Native evidence accepted by the server; one tested issue produces exactly one PR |
| 5 | 06 — bootstrap | Repeatable configuration and a bounded, reproducible acceptance scenario |
| 6 | 05/06 — human-merge reconciliation | A human merges each PR; verified provider merge identity and ancestry unlock dependent work without retaining the completed delivery claim |

Features 03 and 15 depend on 02; 05 depends on 01. They are separate branches.
Features 01–06 plus 15, human-merge reconciliation and compact-stack work S1–S4
are the MVP. Automatic merge (07) is outside this release, with no requirement to land the full rename
first. Product branding is vibearr now; feature 00 completes executable, package,
and deployment naming after the execution path is stable. Bootstrap uses actual
configured names and stable IDs rather than assuming the repository was renamed.

## One-time setup and startup diagnostics

Feature 06 owns one idempotent bootstrap path. It configures the actual repository
ID and default branch, permitted GitHub user IDs, a Codex model/account, validation
profile, delivery mode, and runner identity. It never copies the live checkout or
production database into a task. It can print intended changes before saving and
never replaces existing secrets implicitly.

The setup needs GitHub issue reads, repository clone/push and PR writes, check and
status/merge-observation reads. Runner merge permission is not a prerequisite.
Use existing scoped credential delivery;
import and execution credentials remain separate. Request issue-comment/label
writes only when status updates use them. The native CLI receives only the access
required by its selected verified backend profile. Persistent account state and
renewal remain protected in runner storage; no host-home login, GitHub credential,
queue token or encryption key enters the task. Qualify each supported auth mode
without adding an account sidecar or making unverified renewal a hidden fallback.

Run startup diagnostics before claiming: API identity/ownership, configuration,
selected credentials and preflight grants, runtime launch, agent version/auth,
validation tools/local database, disk capacity, and PR-write/check-read permissions. Report a
specific component and fix. Runtime qualification and the auth smoke check can
run before production claims; they must not depend on having already completed
a qualified production run. Remove that circular legacy release requirement.

Complete that change across `runner_control/configuration.py`, native backend
readiness, `ScopedCredentials.qualification`, `Lifecycle.ready`, and native release
verification. A disabled legacy provider or a different invalid project must not
force this project's valid native configuration back through the old proof path.

One-time repository policy setup is an explicit operator action. Human PR review
and merge are required; per-command and per-plan approvals are not. A read-only
inspection on 2026-11-15 found squash merge enabled, GitHub's native auto-merge
disabled, zero required approving reviews, and an empty required-status list.
Those are historical observations, not acceptable rollout policy. Setup must
verify required current-head checks and human review protection, preserve existing
rules, and keep runner/GitHub automatic merge disabled. This planning change does
not modify repository settings or authorize applying setup changes.

Use a delivery identity without ruleset bypass, or remove its bypass eligibility
during the one-time repository setup. The inspected ruleset permits admin-role
bypass; using an owner's token must not silently turn that into the merge policy.
Startup diagnostics verify the chosen actor and required rules, not just that a
token has write access.

## Task environment: one phase-switching worker

Compose preprovisions app, production PostgreSQL, runner and one worker. The app
includes supervised import scheduling using DB leases. The runner calls the worker
directly and includes account lifecycle and operation-scoped network mediation.
No runtime component controls the host daemon or launches containers, and no
service gets a Docker socket or privileged Docker-in-Docker capability.

The worker sequentially runs credential-free setup, selected model sessions,
read-only reviewers and fresh independent validation. Its immutable image includes
pinned test PostgreSQL/browser binaries. Start disposable local test databases,
app servers and browsers only for the phase that needs them; their resources and
descendants count against the worker's actual limits. Stop/discard them and clear
all phase state before reuse; no permanent test-DB or browser sidecars.

Trusted worker PID 1 validates registered recipes, imports bounded verified
source/context, runs task code under a different unprivileged UID, seals receipts
and confirms all descendants stopped. Reset all workspace, Git metadata, HOME,
caches, credentials and test data between phases and before the next job.
Export a sealed candidate and import a verified fresh copy for independent checks;
validation cannot inherit the builder's environment or DB. Slot reuse is not a fresh
container; actual cross-job isolation and boot/operation reconciliation must pass.
Unknown stop or lost result retains the fence and cannot become success.

Use read-only pinned images, named volumes, bounded tmpfs and actual Compose
resource ceilings. A requested limit the slot cannot enforce requires a matching
slot or rejection. Restrict task networking to operation-scoped runner mediation
and its own local test services; validation has no public egress. Deny
host/LAN/metadata, production, runner account/control and worker management ports.
No task capabilities, privilege escalation, host namespaces, devices or host paths.

The supervisor's Git checkout and journal stay private to its volume. Agents get
verified source copies and disposable Git metadata. Git publication stays with
trusted supervisor code. Never import task-controlled hooks/config/refs or unsafe
archive paths. Credentials come from explicitly saved connections, not host homes
or inherited agent logins. Model credentials may be visible to their own CLI tools;
record that residual exposure without passing control-plane secrets to the task.

Retain retry-stable prepare/start/status/logs/stop/export, operation identity,
deadlines, immutable context, verified change import and protected termination
receipts. Version the changed runtime and worker-boot evidence. Qualification code
runs in containers and can execute before a production claim or paid model call.
Old direct-host launcher reports cannot qualify this architecture.

The [Docker-only plan](docker-only-runtime-plan.md) specifies S1–S4, retained D1–D4
engineering categories, dependency
artifacts, database resets, operator tools and release gates. No host-runner,
engine-socket or manual-approval fallback is supported.

## Work selection and context

Feature 05 extends the importer with repository ID, author ID, labels, update
revision, and same-repository dependencies. Default to the configured owner's
issues; collaborator/public issues do not run just because the project is public.
Poll every 60 seconds by default with conditional requests, a per-project sync
lock, pagination, and GitHub rate-limit reset/backoff. Keep issue discovery alive
when execution is paused. Store the last successful sync separately from attempts.

Bootstrap previews the initial eligible backlog before enabling execution. Default
to issues created since the saved enablement timestamp, with explicit project-level
backfill configuration for older open issues. Import older issues for visibility
without accidentally executing the entire historical backlog. New owner issues
need no additional label or decision.

Use `vibearr:hold` as the hold label; recognize `nq:hold` during transition. Map
`vibearr:priority-high` and `vibearr:priority-low` to queue priorities, otherwise
normal. Define dependencies in the issue template as `Depends on: #123, #124`.
Reject malformed, missing, cross-repository, and cyclic dependencies with a visible
reason. Only verified merge evidence satisfies a dependency; closing an issue
alone does not. A bootstrap seed records prerequisites already merged normally.

An issue template may supply `Branch: feat/example`; validate it as a Git ref,
reject the base branch and collisions with another issue/run, and freeze it at
claim. Otherwise use `vibearr/issue-<number>` (and a distinct manual-task fallback).
Keep branch identity on retry. This makes the branch names in the feature plan
usable rather than silently replacing every one with the old `nq/<item-id>` name.

Keep source eligibility in `work_queue/services.py`: autonomous project, permitted
author, open/not held, no prior delivery, and dependencies satisfied. Apply it to
imported work in UI counts and both generic and runner claims; order high/normal/low,
then creation time and ID. Preserve generic clients' ability to claim manual work
without configuring the built-in native backend. The built-in runner additionally
filters for enabled execution and valid native configuration, including for manual
tasks. Show queued totals separately from currently executable counts. Invalid
projects cannot starve valid projects; global pause/runtime/usage gates still stop
the whole controller. Filter and claim atomically, with a fresh issue read before
agent launch. API failure means wait, not permission to use stale state.

Snapshot issue title/body/comments, base SHA, policy revision, model, validation
profile, and acceptance criteria. Comments remain data, regardless of their author.
New discussion does not silently change a running session. Closure or hold is a
cancellation signal at the next checkpoint; later instruction changes are saved
for an explicit new attempt. Reopening alone does not requeue completed work.

## Native execution, evidence, and validation

Feature 03 pins a real Codex CLI version and verifies its help/configuration and
JSON event format. Use noninteractive execution with policy set by the supervisor,
a fresh per-run HOME/configuration, and no inherited MCP/plugin/user configuration.
Repository instructions inform coding conventions but cannot load tool credentials,
expand egress, or replace task policy. Persist the session ID and launch operation
before advancing. Capture bounded events and the final workspace, not just stdout
or the agent's statement that it finished. A plan may be a short saved artifact;
a second research model call is not mandatory.

Feature 01 defines and 04 implements a version 2 native delivery contract. The
original proposal validator required a plan handoff and consumed human approval
records for every command. It cannot qualify native or Docker-only execution. Keep version 1 historical readback; do not forge approvals or route
native evidence through proposal-only validation. Reject mixed-version writes.

The trusted supervisor's version 2 evidence binds:

- Run/operation IDs, execution kind, runner ownership, effective configuration
  and policy revisions, and immutable issue/context identity.
- Runtime and validation image digests, agent version/session, inspected base SHA,
  validated candidate tree, final commit, branch, and pushed head SHA.
- Validation profile/version and command IDs, actual exit codes, timeouts, run
  times, bounded log references/digests, and the tested tree/commit for every check.
- PR repository ID/number/base/head, remote check evidence, selected delivery mode,
  and, for merge mode, the observed merge commit and issue-completion operation.

The server verifies ownership and current policy, required profile coverage,
identity relationships, and successful outcomes. Only the authenticated supervisor
may attest execution; issue text, agent events, and arbitrary JSON cannot do so.
The pushed commit must have the tested tree; changes after validation invalidate
it. Native success cannot depend on legacy plan/action/approval fields.

Feature 15 supplies the trusted validation profile before 04 integrates it. The
profile is selected from configured immutable manifests outside the candidate
checkout. For this repository it includes Black, Ruff, mypy, migration/system
checks, Django tests with a fresh PostgreSQL DB, CLI tests, portable runner tests,
and Git acceptance. Boundary checks run in qualification containers against the
actual worker stack, with no host Python or nested daemon inside an agent. Record checks
that are inapplicable with an explicit profile rule; do not accept arbitrary skips.

Run validation in a fresh environment without backend/GitHub credentials. Use
throwaway DB credentials and fresh local clusters/databases/roles in the worker,
separate from production and earlier phases. Execute against the
candidate source, but select commands and required check IDs from the trusted
profile. Candidate edits to `test.sh`, workflows, or validation manifests do not
silently weaken the running profile. Qualify a changed profile separately before
adopting it. Do not launch the live-stack `test.sh` from task code.

## Limits, recovery, and visible states

Feature 04 owns execution and delivery; 05/06 own human-merge reconciliation:

```text
queued → preparing → agent → validating → delivering → finished (PR delivered)
PR awaiting human review → human merge → verified merge → dependents eligible
```

Keep queue statuses unchanged and record execution stages on the run. Finish at
verified PR delivery with outcome `delivered`, release the claim and worker, and
show human-review/merge status separately. Independent work may run while a PR
awaits review. A current, read-authorized project reconciliation path verifies
actual provider merge evidence without resurrecting the completed claim. Only
then may dependent work start. Closed-unmerged PRs remain unsatisfied dependencies.

Map successful completion to `succeeded` and failed/no-change/canceled completion
to `failed`, with a structured run outcome/reason distinguishing them. Preserve the
existing generic queue status vocabulary. Waiting and recoverable uncertainty stay
on an owned `running` item; they are not terminal success or an implicit requeue.

Recheck global pause/schedule, project policy, ownership, and cancellation before
each agent/validation launch and before push, PR creation, or merge. Read-only
reconciliation, termination, and reporting of already completed side effects stay
available while processing is paused; otherwise a pause could prevent safe cleanup.

Initial defaults are one active run, 30 seconds controller polling, 30 minutes
aggregate agent time, 20 minutes aggregate validation time, 30 minutes CI wait,
and at most two repair sessions after the initial attempt. In the factory
extension, reviewer-requested changes also consume this budget and all role
sessions count toward aggregate agent time. Remote CI failures and
base-update repairs share that same repair budget. Bound each transport retry to
three attempts with backoff and honor GitHub rate-limit headers. A run held by
schedule/operator pause preserves its claim; it has a 24-hour maximum elapsed
lifetime before a terminal diagnostic. All defaults are configurable and recorded.

Timeout/cancel can release ownership only after executor termination is confirmed.
If termination or an external side effect remains unknown, retain the fence and
surface recovery-required state even past the timeout; do not launch another run
against an uncertain surviving process. Ordinary confirmed failures release the
slot and let independent work proceed.

| Event | Required behavior |
| --- | --- |
| Pause/schedule closes | Stop new launches, checkpoint/stop the task process tree, preserve workspace and ownership; resume only after confirmed termination |
| Cancel/issue closes/hold | Terminate descendants, preserve evidence, finish without delivery; cancellation racing with a completed merge reports the actual merge |
| Failed validation | Feed diagnostics to the same task within the shared repair budget, then fail with useful logs |
| No diff or no meaningful change | Fail with a no-change reason; no fabricated commit, PR, or issue closure in the MVP |
| Agent exits/crashes | Use persisted exit/workspace evidence; an unknown paid-call outcome stops that attempt and is never blindly replayed |
| Supervisor restarts | Reconcile the same executor operation/session and claim before advancing; never infer termination from a missing heartbeat |
| Push/PR response is lost | Look up the exact repository/branch/head and operation marker; reuse only an identical result |
| CI stays pending or fails | Bound the wait/repair; retain PR identity and mark the failed outcome so independent work can proceed |
| Claim revoked/config changes | Fence all writes; stop on permission/auth changes; use the saved snapshot for ordinary model/instruction changes |
| Disk pressure | Stop new claims, retain active data, and report the storage problem |

Use existing run pages for stage, wait reason, last heartbeat, age, retry count,
validation summary, PR/merge links, and redacted error. Add the minimum missing
fields/controls in the owning branches; the full dashboard is not required.
Failed/canceled tasks require explicit retry through the UI/API after confirming
termination. This is retrying work, not approving commands. A retry creates a new
attempt, retains history, and reconciles an existing branch/PR before changing it.

## Human merge and completing an issue

Publish the validated head with check/review evidence for human inspection. Required
GitHub checks and human review apply to the current PR head under normal branch
protection. Historical checks for another head/base do not establish readiness.
If a delivered candidate changes, obtain new validation/review evidence; a repair
after run completion is an explicit new attempt that retains the same PR identity.
Never force-push delivered history or weaken repository rules to obtain a pass.

The runner must not call a merge endpoint, enable auto-merge, or close an issue
as implemented when it merely delivers a PR. A human decides whether to merge.
Read-only reconciliation verifies repository ID, PR identity, actual merged state,
merge commit and base ancestry; it records the human merge actor and timestamp.
Record merged work even when an observation response is lost, without rerunning
the agent. A closed issue or closed-unmerged PR is not merge evidence.

Issue closure may follow GitHub's PR closing reference on human merge; any later
supervisor reporting must be explicitly scoped, idempotent and tied to the
verified merge. Reporting failure does not erase completed development work or
authorize another agent. Reconciliation survives controller restarts and uses
current project read authorization, not an obsolete delivery claim.

The technical [automatic merge reference](automatic-merge.md) is retained for
deferred feature 07. Its old no-human-merge acceptance does not apply to this MVP.
The [factory maturity gate](software-factory-plan.md#later-automatic-merge-maturity-gate)
requires sustained evidence and explicit human rollout approval before reconsidering it.

## Bootstrap backlog and release acceptance

Features 01, 02, 03, 15, 04, 05, 06, and human-merge reconciliation must be implemented by ordinary
development before the complete loop exists. Do not promise that an unimplemented
queue will build its own prerequisites. Feature 06 provides a local seed manifest
and idempotent issue-export tooling; publishing the initial batch is one explicit
setup action. Keep stable feature keys and translate dependencies to real issue
numbers. Later permitted owner issues are admitted automatically.

Feature 06 writes the acceptance driver, including verification of real human merges.
Use this repository (or an explicitly configured test fork for rehearsals), real
Codex authentication, and finite issue contents with clear acceptance criteria.
Run two small source changes, not documentation-only or model-only smoke tests:

1. **Issue A:** a small queue behavior change with tests and acceptance criteria
   that the selected baseline does not already satisfy.
2. **Issue B, depends on A:** extend A's new behavior with a regression test that
   requires A's implementation. It must start from a base containing A's human merge.

Choose and freeze the concrete issue contents before the run. The earlier status
filter/link-preservation examples have since shipped; do not replay them as proof
of new development on the current baseline.

Store rendered browser screenshots for these UI changes as required by the
repository guidelines. The driver collects them alongside the test results;
synthetic screenshots cannot substitute for the implemented candidate pages.

Also run a deliberately failing validation case and the interruption scenarios
below. Do not publish tasks or make paid calls while merely editing this plan.

| Scenario | Evidence required for the MVP release |
| --- | --- |
| Compact footprint | Exactly four runtime containers including selected account auth; normal setup/coding/check/browser phases add none, startup is in-service, and explicit tooling jobs exit after use |
| Phase isolation | Agent state/credentials/processes cannot survive into validation; local test DB/admin storage is isolated and reset; cleanup failure fences the worker |
| Normal two-issue loop | Issue IDs, discovery timestamps, distinct runs/branches/PRs, real sessions, validation manifests, check SHAs, human merge actors/timestamps and merge SHAs, and B's base containing A |
| Human merge boundary | Human review and merge for each PR, zero runner merge calls or auto-merge enablement, no command/plan approval records consumed and no ready-label transition |
| Awaiting human review | Delivery releases the claim/worker; independent work can proceed, dependent work cannot start before a verified merge, and closed-unmerged PRs stay unsatisfied |
| Isolation | Actual containerized probes deny host/engine/control-plane/production access and verify descendant termination, cross-job reset and egress policy |
| Validation failure | A real nonzero test exit prevents delivery/merge, repairs are bounded, and the next independent task can run |
| Interrupted execution | Restart after launch response loss and during agent/validation work; the same operation is reconciled, with no duplicate active process |
| Interrupted delivery | Restart after push/PR creation and during observation of a human merge/completion reporting; exactly one PR and accurate merge/issue state remain |
| Policy/concurrency | Concurrent claims, stale tokens, a second controller, foreign author, hold, closed issue, and unmet dependency cannot bypass eligibility/fencing |
| Moving base/head | Old checks/reviews do not mark a new candidate ready; changed base is revalidated; exhausted repair budget leaves an accurate PR/failure for human review |
| Operator controls | Pause/cancel and unsupported quota enforcement behave as configured and remain visible |

Store a redacted release report with source revision, image/profile digests, exact
versions, commands/results, and artifact/PR links. Automated assertions should
fail on missing evidence rather than emit a checklist marked complete by the
agent. The release requires every row; fakes cover protocol edge cases but do not
replace the real two-issue run or actual deployment boundary tests.

## References and limits of this review

The plan review inspected the queue/services, runner configuration and lifecycle,
approval-bound delivery validator, secret scoping, runtime/process recovery,
Compose, test scripts, CI workflow, and read-only GitHub repository/ruleset state.
It did not execute a native coding agent or qualify the deployment. These remain
implementation acceptance work, not unresolved product-policy decisions.

- [Codex noninteractive execution](https://developers.openai.com/codex/noninteractive/)
  documents structured events and explicit permissions for scripted sessions.
- [GitHub pull request merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
  documents the expected-head-SHA guard and merge outcomes.
- [Docker Engine security](https://docs.docker.com/engine/security/)
  explains daemon authority and why runtime services must not receive the host socket.
