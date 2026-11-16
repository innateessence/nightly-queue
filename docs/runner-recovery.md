# Runner recovery and cancellation

The supervisor now assembles `ContextPlanStore`, `WorkspaceManager`,
`ResearchStage`, `ImplementationStage`, and `DeliveryStage` into the owned
claim-to-finish lifecycle. Execution remains disabled by the server's
`execution_ready: false` release gate and the backend's unverified live readiness.
`--check` authenticates and reads state without binding a controller or claiming.
No production queue action or paid model call was used to validate this branch.

## Ownership

On its first normal connection the supervisor writes a random 256-bit
`controller_secret` into the private, fsynced journal, then binds its SHA-256
hash to `RunnerIdentity.controller_digest`. The centralized HTTP client sends
`X-NQ-Controller-Secret`; execution, secret delivery, metadata writes, token
recovery, and operation lookup require this proof as well as the runner Bearer.
Unbound identities cannot execute. Identity, configuration, active-run status,
and gate readbacks remain read-only probes. Generic queue and `nq` APIs retain
their existing authentication and behavior.

`POST /api/v1/runner/controller/` has an empty body. Repeating the same binding is
safe. Binding and mutation recheck current bearer and volume proof under the
controller lock, so revocation after HTTP authentication still fences the request.
Another volume cannot bind, recover a claim token, or mutate the run even
with the same runner Bearer. A first binding with existing active work is refused.
No automatic secret replacement, heartbeat expiry, lease takeover, or requeue is
provided. Missing local state with server-owned work requires operator recovery.

The journal lock excludes another controller on the same volume. Treat the
private volume as unique operational ownership: never start a second controller
from a copied backup with the same secret. Restoring or replacing it requires
stopping the previous container and every child process first. A copied secret
is a copied credential, not an independent controller identity. Do not clear the
server digest to make a replacement controller adopt unfinished work.

## Checkpoints and recovery

The journal retains the owned response and private claim token, original claim
window, stage sessions, full context and plan references, validation attestation,
PR delivery state, wait reason, active-time budget, and unfinished HTTP/process
operations. The original window counter is charged once, including when the claim
response is lost. An absent claim lookup retries the original UUID and request
only after current processing gates, including current and original account
reserves, pass again. A blocked retry retains its original window accounting.
Empty outcomes remain empty. Owned work is reconciled before
any new claim. Completed results retain claim proof locally until the completion
callback acknowledges them; branch 12 uses this to guard retention.

Before a trusted subprocess executes, a small launcher waits for the supervisor
to persist its PID, Linux start time, boot ID, and process group. Death before
that handshake cannot execute the command. Normal completion reaps and verifies
the group; restart kills and confirms any recorded matching groups before
resuming. Changed process identity or unconfirmed termination blocks execution.
These records cover model workers, approved sandbox commands, and supervisor Git.
An old heartbeat is never accepted as proof of process termination.

Every stage and every model/action checkpoint re-reads current global controls
and verifies remote ownership. Schedule, reserve, capacity, and approval waits
retain the claim and validation state. Waiting between steps consumes no active
budget. Approval polling happens before workspace preparation and does not charge
active time. Workspace and pending metadata preparation consume the total budget;
they do not require remaining implementation time after validation starts. Initial
context fetching and metadata recovery propagate fresh aggregate deadlines and
local stop checks without serializing callbacks or monotonic deadlines. Tracked
Git preparation stops and confirms child termination at its active deadline.
An interrupted active segment remains blocked for conservative operator
accounting; its elapsed budget is never silently reset, including across reboot.

Metadata uploads use durable operation UUIDs and exact request bodies.
`GET /api/v1/runner/runs/<run>/operations/<operation>/` returns the original
context, plan, handoff, approval, or evidence response. A confirmed absent
operation can replay its exact idempotent request. Reusing an operation UUID
with changed content is refused. Changed discussion and base revisions require
assessment and a saved replacement plan. The supervisor checks the current remote
base with `ls-remote` without resetting or overwriting local work. Private clones
and remote reads use the selected repository credential through the scoped
supervisor Git helper; credentials stay outside agent environments.

A journaled pending model request, started approved command, or uncertain patch
requires operator reconciliation rather than a second invocation. Stop and
confirm the original process first, inspect provider records and workspace
changes, then preserve the result or explicitly fail/requeue through the existing
operator procedure. This release supplies no endpoint that guesses an unknown
model result or erases these markers automatically.

Delivery uses branch 10's commit intent and remote PR reconciliation. Once the
verified PR reaches `delivered` or `reporting_pending`, recovery only reconciles
and reports finish; it does not require a new model, credentialed Git operation,
or PR creation. This reporting remains possible when a processing gate closes.
Known delivery failures preserve any recorded PR URL on the failed run.

## Cancellation

`request_cancellation(run_id, reason)` records an idempotent operator request.
Branch 12 supplies the browser control. The owned HTTP endpoint is
`POST /api/v1/runner/runs/<run>/cancellation/` with `claim_token`, `reason`, and
`termination_confirmed`. A request must be recorded before confirmation.
Subsequent stage writes and new secrets are fenced immediately. The runtime's
periodic checkpoint interrupts the backend/sandbox, verifies all tracked child
groups stopped, then persists confirmation and reports `failed` with a cancelled
explanation. Request and confirmation timestamps remain on the run. Existing
queue statuses are unchanged. Missing termination proof retains running ownership
and blocks new work; cancellation never auto-requeues.

## Validation and remaining release work

Portable recovery tests cover lost claims and uploads, exact operation identity,
missing journals, original counters, current gates, uncertain models/actions and
budgets, stale ownership, process-group termination, and cancellation. Git
acceptance tests run the real research/implementation/validation/delivery stages
against temporary repositories and fake provider/API responses, reload approval
and gate waits, and prove lost finish responses do not duplicate model calls,
pushes, or PRs. Existing delivery tests cover interruption around commit, push,
PR creation, artifacts, and finish. Django tests exercise proof fencing,
immutable operation lookup, cancellation ordering, and admin requeue.

The production image's sandbox namespace probe remains blocked by its deployment
security policy. Live backend authentication, real quota capability, and a
user-designated GitHub end-to-end release task remain unverified. They must be
qualified by branch 12 before release; deterministic test fakes are not release
proof.
