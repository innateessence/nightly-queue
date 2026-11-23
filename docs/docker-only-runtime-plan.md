# Four-container Docker runtime

This contract replaces the preprovisioned worker-slot fleet and its separate
agent/setup/validation, database, proxy, gateway, and account services. It implements
[issue #152](https://github.com/innateessence/nightly-queue/issues/152), adapting
the existing work in [#142](https://github.com/innateessence/nightly-queue/issues/142)
to the [factory plan](https://github.com/innateessence/nightly-queue/pull/146).

## Deployment

The canonical Compose stack has exactly four long-running services:

| Service | Responsibility |
| --- | --- |
| `app` | Web UI/API, PostgreSQL-backed scheduling, durable manual/scheduled issue imports |
| `postgres` | Production application database |
| `runner` | Independent controller, private journal, persistent selected logins, scoped network mediation and Git/PR delivery |
| `worker` | Authenticated execution API; sequential setup, selected native CLI/reviewer and independent validation phases; disposable local PostgreSQL/browser services |

The worker image includes pinned Codex, Claude Code and OpenCode executables. The
lean runner carries their subscription-login executables but no task tools,
PostgreSQL, browser or task-network capabilities. One coding task owns the worker
at a time; its phases execute sequentially. The runner
starts with the application but processing remains disabled until configured and
qualified. A failed check or unavailable login must not enable another backend.
Humans review and merge every PR; keep delivery mode `pull_request` and automatic
merge disabled. A role's readiness result is not merge authorization.

Codex uses ChatGPT subscription login. Claude Code uses Claude subscription login.
The application stores a managed-login reference; OAuth login files remain in a
private runner volume, separate from source, logs and Git delivery credentials.
Only the selected account is copied into a clean task home, and refreshed login
material is collected after task descendants terminate. It is not isolated from
that agent's own tools. Model API-key fallback is forbidden in this runtime.

OpenCode uses an operator-provided external Ollama origin and installed model.
There is no bundled Ollama service, automatic discovery, server management or
model download. Only the selected model may be requested through the local
inference relay. It permits inference/model inspection, never model management.

## Boundaries

All application, agent, Git, test, browser, packaging, backup and recovery processes
run in Docker. Explicit operator Docker/Compose orchestration and deliberate
source/configuration edits remain the host boundary. Host `gh` is permitted when
explicitly authorized by the operator. Do not install host tools or services.

Use immutable images, private Docker networks, named data volumes and bounded
scratch tmpfs. No host checkout/home/credentials, engine socket/API credentials,
privileged mode, host namespaces or devices are allowed. Image IDs are supplied
by explicit operator image inspection; the runtime never controls Docker.

Neither runner nor worker has production DB network access, credentials or the
application encryption key. The worker mounts only its control state and private
worker-channel secrets. Persistent logins, controller journals and publication
credentials remain in the runner. The worker has no public egress network.
Root-owned execution control drops task code to UID 1000 with no capabilities or
privilege escalation; temporary PostgreSQL administration and candidate app
services use distinct identities. Task code cannot modify runtime commands,
image-owned validation tools or trusted control state.

Account, model/package and selected Ollama mediation are modules in the trusted
runner, with one lifecycle/journal rather than standalone daemons or sidecars.
The worker calls only operation-scoped endpoints/grants. Task-UID traffic cannot
reach management/login APIs or arbitrary public/LAN addresses. The operator's
selected external Ollama endpoint is an explicit exception, reached through the
inference-only relay. Setup has package access; validation has no external
network. Loopback is available for temporary test databases and test servers.

The worker's CPU, memory and PID limits cover its trusted execution control,
coding/check phase and local test services together. The runner has separate
declared control-plane limits; no per-process ceiling is implied. Phases share a
namespace and scratch storage that trusted reset must clean. This design is for
the operator's registered repositories, not hostile multi-tenant workloads.
Integrated-runner and retired fleet proofs cannot qualify this separate boundary.

## Jobs, validation and recovery

Manual imports are durable PostgreSQL jobs. Scheduled imports retain configured
intervals and source eligibility. Atomic claims, renewed leases, idempotent issue
imports and retry delays allow recovery without a message broker. The web server
and scheduler are supervised together; coding work happens in the runner and does
not block imports.

Each task phase gets a fresh snapshot/home. Confirm task descendants are gone
before reset or reuse. Export a sealed candidate, reset all writable state, and
import a verified fresh copy for validation or review. An uncertain termination fences the worker. The durable
controller journal and immutable operation IDs prevent restart from inventing a
new agent invocation, push or PR for an uncertain existing operation.

Trusted validation runs after the agent stops and checks the exact candidate
revision in fresh workspaces. Preserve the nine-check recipe and bounded,
run-scoped dependency artifacts. PostgreSQL tests start a temporary local server
with disposable storage; it never uses production PostgreSQL. Image-owned tools
and controller imports cannot be replaced by candidate dependencies.

Qualification checks the installed boot, resource ceilings, task UID/capabilities,
trusted-state denial, control/egress denial, descendant termination and cross-job
reset, cancellation, deadlines and interrupted-operation reconciliation. Every
restart requires a fresh interrupted witness and a new boot-bound proof. Live account/model and delivery
acceptance are separate and must report genuine observations, never fixtures.

## Operations and migration

Migrations run inside `app` before the web server and scheduler. Backups, restore,
developer/maintenance checks and qualification may use explicit short-lived
container operations, stopped after use. Normal task checks and browser reviews
run inside the worker without adding containers. Preserve database names, volume identities, original
keys, selected credentials, queue/history, journals and unresolved publications.

Drain and back up the old deployment before switching. Test restore into an empty
separate project. Keep processing disabled during migration and qualification.
Retire only explicitly selected old project services after acceptance, preserving
rollback data until checked. Never prune unrelated Docker resources or manage an
external Ollama service. Automatic production rebuild/deployment by the coding
runner is outside this contract.

See [Docker operations](docker-operations.md) for executable procedures and the
current verification evidence. Historical slot/host documents describe old
protocols, not the current deployment contract.
