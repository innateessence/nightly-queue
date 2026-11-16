# Unattended queue execution

The supervisor claims an eligible task, freezes its source and checkout, and runs
the selected native coding agent inside the qualified OCI runtime. It imports
the agent's verified files, commits the candidate, and independently validates
that exact Git tree before publishing one pull request. Context and plan records
describe the work; they require no command or plan approvals.

## Configure the worker

Apply the application migrations, including `0011_native_run_protocol`. Keep the
supervisor's private journal on a persistent volume and configure its queue URL,
runner bearer token, executor URL and private executor token file. The supervisor
uses the existing controller-volume proof, claims, schedules and usage reserves.
It cannot recover ownership using a new empty volume.

Register and qualify the agent image and every fixed validation profile using
the [isolated executor](isolated-executor.md). Set
`VIBEARR_VALIDATION_PROFILE_FILE` to a private operator-owned JSON file containing
the configured immutable `ValidationProfile.data()`. Select matching
`execution_policy.runtime_profile` and `execution_policy.validation_profile`
values in the project's runner settings. The supervisor never accepts commands,
images or passing-test claims supplied by the model as validation authority.

For this Python/Django repository, the agent image needs reviewed application
requirements preinstalled and a pinned disposable PostgreSQL sidecar. See
[native Codex](native-codex.md). Candidate dependency changes use a separate
credential-free build and image qualification before validation; their derived
image belongs only to that run.

Select separate saved credentials for the coding backend, issue reads and
repository delivery. Add explicit runner preflight grants for those selected
credentials. The native task receives only its selected model credential. GitHub
and queue tokens remain in the supervisor. Repository reads are verified during
preflight; actual push and PR creation establish write capability. A repository
role alone cannot establish a fine-grained token's write scopes.

Enable the project and the global processing schedule when ready. GitHub source
selection preserves the project's genuine enable cutoff and author/hold/
dependency rules. Existing issues are not silently backfilled. A source-eligible
project is checked before a live model authentication probe can run. Empty queues
make no paid probe calls; eligible projects share a short-lived proof for the
same selected account, version, model and runtime. Rotation, failure or changed
runtime invalidates that proof.

Run `python -m vibearr_runner --check` for a read-only queue authentication check.
Run `python -m vibearr_runner` to start the normal background controller. A missing
or unqualified runtime, selected credential or validation profile blocks claims.
The queue server advertises protocol availability separately from the
supervisor's actual host readiness.

## Follow a task

Run history shows its stage, native session, heartbeat, validation state, repair
count, wait reason and delivered PR. Context and plan pages retain the frozen
instructions, comments, repository ID, base commit, policy, selected account,
runtime and validation recipe. New discussion does not rewrite an already
running prompt. Closure, holds, changed material instructions, lost authority or
changed execution policy stop further task actions.

The agent can repair a failed independently checked candidate at most twice.
Repairs retain the frozen context, original wall-clock task and stage deadlines,
and require confirmed termination of previous work. A no-change result finishes
with an explanation and creates no empty commit or PR.

PR-only delivery ends after the exact pushed commit, observed open PR and server-
verified version2 evidence have been saved. The PR is ready for review. This
mode does not wait for CI or merge it; the delivery state remains an extension
point for automated merge policy.

## Pause, cancel and recover

Processing controls pause the same native invocation. Its original deadline
continues while paused; host enforcement stops an expired frozen container.
Dependency builds pause setup or wait at a known phase checkpoint. A copy,
assembly or trusted qualification already in flight can settle before the UI
reports it paused. See [runtime controls](native-runtime-controls.md).

Cancellation stops owned native and local Git processes and reconciles any push
or PR request already dispatched. Retry keeps the old claim and slot until that
work is confirmed terminal. A late retry of an older run cannot cancel a newer
attempt. An existing pushed branch or PR needs reconciliation before another
attempt can publish.

A supervisor restart uses the same journal, executor instance, operation IDs,
source, deadlines, committed candidate and publication identity. It never
replaces an unknown paid session, push or PR request. A lost response is resolved
by inspecting that original operation or provider identity. If the outcome
remains unknown, the claim stays owned and the run waits for reconciliation.
Replacing executor state is detected separately from ordinary service restart.

Actual controller/credential ownership loss stops locally owned execution even
if the queue API cannot accept another write. A graceful supervisor restart
preserves its durable invocation. A transient queue outage pauses work and
prevents new actions until current authorization can be checked again.

Transient GitHub GET and Git clone, fetch and remote-ref reads use at most three attempts with
backoff. Their attempt counts and next retry times survive supervisor restarts;
GitHub Retry-After and rate-reset headers extend the wait. Normal controller
cycles keep cancellation, pause, ownership and original deadline checks active
during that wait. Exhaustion fails ordinary work, while an unresolved publication
keeps its claim and reports reconciliation required. Push and PR mutations retain
their original identities and are never repeated as transport read retries.
Native push dispatch is recorded immediately before the push command; remote-ref
reads run separately before dispatch and after the command for reconciliation.
The tracked child waits behind its launch gate while the supervisor rechecks
controls and saves dispatch. A rejected prelaunch check reaps that blocked child
without recording a push; uncertainty after release keeps the publication fence.
Clone recovery records the exact owned destination before launch. A partial
checkout is removed only after tracked process termination is confirmed; a
durably completed clone resumes local setup without another clone. Unregistered
incomplete checkouts require operator reconciliation.

Completion acknowledgement permits existing server-directed retention. Native
source and logs expire independently, including executor artifacts, dependency
builds and run-owned derived images. Terminal identity/digest tombstones prevent
purged IDs from launching again. Unknown or running resources remain protected;
shared artifacts and operator images are preserved. Historical validation,
commit, PR and termination evidence stays readable after content expires.

## Real acceptance

Use a dedicated disposable queue database/project and a genuine new project
cutoff. Publish a bounded owner-authored issue after that cutoff, import it
normally, and configure the same qualified executor and selected credentials as
production. Keep other projects out of this acceptance configuration.

```sh
python -m vibearr_runner.queue_acceptance --execute \
  --state /private/acceptance-supervisor \
  --project-id 1 --issue-number 123 \
  --evidence /private/acceptance-delivery.json
```

This makes real model and GitHub requests. It runs normal controller cycles in
successive supervisor processes with the same journal. It records restart
checkpoints after agent completion, verified push, verified PR, and before
completion reporting, then reads the acknowledged server delivery back. It
refuses another project/issue and never clears an unknown invocation to continue.
The ordinary background service keeps one supervisor process; this driver adds
frequent restarts to exercise its durable recovery path.

The [issue #99 acceptance record](acceptance/feature04-issue99.md) contains actual
PR-only delivery receipts, supervisor restart checkpoints and run screenshots.
