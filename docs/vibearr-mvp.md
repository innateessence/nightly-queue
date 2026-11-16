# vibearr MVP: the first self-improvement loop

This is the release contract for the first usable vibearr, alongside the
[feature branch plan](autonomous-ai-queue-plan.md). It describes work to implement,
not acceptance results already obtained. Each section names the feature that
owns it so an implementation issue can be written without inventing new policy.

## What counts as an MVP

An owner creates two small development issues in this repository. The second
depends on the first. With the services running, vibearr discovers the issues,
implements the first, independently validates it, creates a PR, waits for required
checks, merges it, and starts the second from the updated default branch. No
command approval, plan approval, ready label, manual merge, or service restart is
needed between the two issues. Failures remain visible and do not block unrelated
eligible work forever.

The first release supports one repository, one active run, native Codex, API-key
authentication, continuous processing, and GitHub polling. Existing manual tasks,
schedules, pause/cancel, and optional usage reserves keep working. Claude Code,
OpenCode, additional repositories, and the full *arr dashboard follow this loop.

Merged source improvements are available to the next task immediately. Updating
the running web app and supervisor is a separate release/deployment operation;
automatic self-updates remain feature 12. Do not call a merged PR a deployed
upgrade. The MVP must be capable of implementing feature 12 through its own queue.

## Build order and ownership

| Order | Feature | Required result before moving on |
| --- | --- | --- |
| 1 | 01 — execution policy | Native-run configuration and evidence contracts; old approval waits cannot execute |
| 2 | 02 — isolated runtime | Real task launch, status, logs, stop, restart reconciliation, and isolation on the intended Linux host |
| 3a | 03 — native Codex | One genuine noninteractive editing session with usable auth and a recorded outcome |
| 3b | 15 — isolated validation | A disposable PostgreSQL-backed test profile usable without an agent or Docker socket inside the task |
| 3c | 05 — issue selection | Only permitted, ready, dependency-satisfied work can be claimed |
| 4 | 04 — unattended execution | Native evidence accepted by the server; one tested issue produces exactly one PR |
| 5 | 06 — bootstrap | Repeatable configuration and a bounded, reproducible acceptance scenario |
| 6 | 07 — merge | Exact-head checks and merge reconciliation complete the two-issue scenario |

Features 03 and 15 depend on 02; 05 depends on 01. They are separate branches.
Features 01–07 plus 15 are the MVP, with no requirement to land the full rename
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
status reads, and merge permission. Use existing scoped credential delivery;
import and execution credentials remain separate. Request issue-comment/label
writes only when status updates use them. The native CLI receives its Codex API
credential, not a personal home directory, subscription login, GitHub credential,
queue token, or encryption key. Supporting subscription auth is a later capability
unless it is proven separately without blocking the API-key path.

Run startup diagnostics before claiming: API identity/ownership, configuration,
selected credentials and preflight grants, runtime launch, agent version/auth,
validation image/database, disk capacity, and merge/check permissions. Report a
specific component and fix. Runtime qualification and the auth smoke check can
run before production claims; they must not depend on having already completed
a qualified production run. Remove that circular legacy release requirement.

Complete that change across `runner_control/configuration.py`, native backend
readiness, `ScopedCredentials.qualification`, `Lifecycle.ready`, and native release
verification. A disabled legacy provider or a different invalid project must not
force this project's valid native configuration back through the old proof path.

One-time repository policy setup is allowed. Per-task confirmation is not. A
read-only inspection on 2026-11-15 found squash merge enabled, GitHub's native
auto-merge setting disabled, zero required approving reviews, and an active main
ruleset whose required-status list is empty. Feature 07 must configure/verify an
explicit nonempty required-check set before automatic merge is enabled. Use the
runner's merge operation; enabling GitHub's separate auto-merge feature is not a
prerequisite. This planning change does not modify repository settings.

Use a delivery identity without ruleset bypass, or remove its bypass eligibility
during the one-time repository setup. The inspected ruleset permits admin-role
bypass; using an owner's token must not silently turn that into the merge policy.
Startup diagnostics verify the chosen actor and required rules, not just that a
token has write access.

## Task environment: choose one supported deployment

Feature 02 implements an OCI task container launched through a trusted executor
using the existing Docker tooling on a designated Linux runner host. Do not spend
the MVP trying to nest bubblewrap inside the current hardened runner container.
The old namespace implementation can remain for historical tests, but it is not
the native execution path and cannot accidentally determine its readiness.

The executor is a small, authenticated service on the supervisor's control
network. It is the only component allowed to access the container engine. It
accepts registered image/profile IDs and run-owned workspace handles, not arbitrary
Docker arguments, host paths, mounts, or shell commands. It derives paths under
its private state root and journals container IDs before returning launch results.
Its engine access makes it a trusted host component; deploy it on a dedicated
runner host and keep its API and engine socket outside every task environment.

Use a read-only image root, an unprivileged task user, bounded writable workspace,
temporary HOME, CPU/memory/PID/storage limits, and no host namespaces, privileges,
production mounts, engine socket, or control-plane credentials. Each run gets its
own network and disposable test database. The supervisor prepares a private Git
checkout; the agent gets a separate copy and its own disposable Git metadata.
After the agent stops, import verified file changes into the supervisor checkout.
Do not trust agent-controlled hooks, Git config, refs, archives, or filesystem
links when committing or publishing. Include additions, deletions, modes, and
symlink/path rejection in change-import tests.

Agent traffic leaves through an executor-managed egress proxy with an explicit
model/package endpoint allowlist. Deny direct egress and host, LAN, metadata,
control-plane, production DB, and broker addresses, including DNS rebinding and
IPv6 paths. Permit only that run's disposable test DB when needed. The validation
phase has no model credential and no external network. Prebuild its dependencies
in a pinned image; feature 15 defines how dependency changes create a replacement
image in the same isolated build boundary. No task gets to start containers.

A native CLI may expose its backend credential to its own tools. For the first
release, use a dedicated backend account/key with provider-side spending limits
where available, redact known credential values, and record this residual exposure.
Do not claim isolation of the key from tool code unless a real credential broker
provides it. Local time/attempt limits cannot guarantee a dollar cap.

The executor contract is retry-stable `start(run_id, operation_id, profile_id)`,
`status(operation_id)`, bounded log reads, and idempotent `stop(operation_id)`.
Status distinguishes not-started, running, exited with code, and unknown. A lost
launch response is resolved by lookup, not by starting another container. Stop
must establish that the complete task container and descendants are gone. Add
this path to `coding_runner.verify --boundary` with a versioned runtime/profile
report. Qualification is an automated deployment check, not an approval screen.

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
current `runner_control/delivery.py` requires a plan handoff and consumed human
approval records for each validation command. That validator cannot be reused
unchanged. Keep version 1 historical readback; do not forge approvals or route
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
and Git acceptance. Deployment boundary checks run through the executor on the
designated host, never as nested Docker calls inside the agent. Record checks
that are inapplicable with an explicit profile rule; do not accept arbitrary skips.

Run validation in a fresh environment without backend/GitHub credentials. Use
throwaway DB credentials and ports/volumes unique to the run. Execute against the
candidate source, but select commands and required check IDs from the trusted
profile. Candidate edits to `test.sh`, workflows, or validation manifests do not
silently weaken the running profile. Qualify a changed profile separately before
adopting it. Do not launch the live-stack `test.sh` from task code.

## Limits, recovery, and visible states

Feature 04 owns this lifecycle; 07 adds the final CI/merge states:

```text
queued → preparing → agent → validating → delivering → waiting for CI → merging → finished
```

Keep queue statuses unchanged and record these stages on the run. PR-only mode
finishes at verified delivery. Automatic-merge mode keeps the active run and claim
through CI/merge, then records both delivery and merge evidence before success.
This reuses owned-run credential delivery and prevents a dependent task starting
too early. Do not finish at PR creation and then require an obsolete claim to merge.

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
and at most two repair sessions after the initial attempt. Remote CI failures and
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
| Pause/schedule closes | Stop new launches, checkpoint/stop the current container, preserve workspace and ownership; resume only after confirmed termination |
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

## Exact-head merge and completing an issue

Feature 07 polls an explicit required-check set from configuration, with expected
workflow/check producer identities. The initial repo profile includes the existing
`Tests` workflow (`.github/workflows/test.yml`, job `Run test.sh`) and feature 15's
`Vibearr validation` workflow (`.github/workflows/vibearr-validation.yml`, job
`Validate candidate`). Both are required for this repository. Resolve and store
their real workflow/producer IDs when installed. Runtime boundary qualification
is additionally required when the configured runtime/profile changes. An empty
required set, absent check, wrong producer/SHA, canceled/skipped result, pending
run, or stale validation cannot authorize merge. Check workflow/profile changes against the trusted base
configuration, not the candidate's preferred list of checks.

Use squash merge, consistent with the repository's linear-history rule. Immediately
before merging, re-read issue eligibility, PR identity, latest base/head, and
required check results. Use GitHub's expected-head-SHA merge parameter and normal
branch rules without admin bypass. If the base changed since validation, prepare
an updated candidate and revalidate within the shared repair budget. A 403/405/409
or ambiguous timeout requires inspection; it is not success and does not justify
changing protection rules automatically.

Update a delivered feature branch with a normal merge of the latest base and a
new validated commit, preserving old commits and PR identity. Do not force-push
or rewrite delivered history. Version the context/base and validation evidence for
that candidate; the merge API must check the new head, not reuse the old receipt.

After a lost merge response, query PR merge state and record the real merge commit.
Close the original issue only after that merge is verified, then acknowledge the
idempotent completion operation and release the active slot. A lost issue-update
response is reconciled with GitHub. Merge success with pending issue reporting
must never launch another agent. Dependents use verified merge evidence, so a
temporary issue-reporting error does not erase completed development work.

## Bootstrap backlog and release acceptance

Features 01, 02, 03, 15, 04, 05, 06, and 07 must be implemented by ordinary
development before the complete loop exists. Do not promise that an unimplemented
queue will build its own prerequisites. Feature 06 provides a local seed manifest
and idempotent issue-export tooling; publishing the initial batch is one explicit
setup action. Keep stable feature keys and translate dependencies to real issue
numbers. Later permitted owner issues are admitted automatically.

Feature 06 writes the acceptance driver; 07 extends it to require real merges.
Use this repository (or an explicitly configured test fork for rehearsals), real
Codex authentication, and finite issue contents with clear acceptance criteria.
Run two small source changes, not documentation-only or model-only smoke tests:

1. **Issue A:** add a status filter to the queue, with tests for each status and
   malformed input. The current queue filters title, priority, and project.
2. **Issue B, depends on A:** preserve that status in filter links/clear behavior,
   with a regression test. It must start from a base containing A's merge commit.

Store rendered browser screenshots for these UI changes as required by the
repository guidelines. The driver collects them alongside the test results;
synthetic screenshots cannot substitute for the implemented candidate pages.

Also run a deliberately failing validation case and the interruption scenarios
below. Do not publish tasks or make paid calls while merely editing this plan.

| Scenario | Evidence required for the MVP release |
| --- | --- |
| Normal two-issue loop | Issue IDs, discovery timestamps, distinct runs/branches/PRs, real sessions, validation manifests, check SHAs, merge SHAs, and B's base containing A |
| No human gates | No approval records consumed, no ready-label transition, and no manual command/plan/merge decisions during the run |
| Isolation | Actual host tests deny task access to engine/control-plane/production resources and verify descendant termination and egress policy |
| Validation failure | A real nonzero test exit prevents delivery/merge, repairs are bounded, and the next independent task can run |
| Interrupted execution | Restart after launch response loss and during agent/validation work; the same operation is reconciled, with no duplicate active process |
| Interrupted delivery | Restart after push, PR creation, merge, and completion reporting; exactly one PR/merge and accurate issue state remain |
| Policy/concurrency | Concurrent claims, stale tokens, a second controller, foreign author, hold, closed issue, and unmet dependency cannot bypass eligibility/fencing |
| Moving base/head | Old checks cannot merge a new head; changed base is revalidated; exhausted repair budget leaves an accurate PR/failure |
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
  explains why engine access belongs to a trusted host component.
