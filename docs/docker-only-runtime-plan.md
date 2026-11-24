# Docker-only MVP runtime

This contract supersedes host-executor and host-service deployment instructions.
It describes the implementation and release requirements. See
`docker-only-progress.json` and `acceptance/docker-runtime.md` for completed
engineering checks and outstanding deployment acceptance. Keep autonomous
execution disabled until the actual target stack is qualified.

Footprint revision, 2026-11-22: the release target is **four always-on containers**:
app, production PostgreSQL, trusted runner and one phase-switching worker. This
supersedes the separate agent/setup/validation, test-DB, gateway, account and proxy
container topology. S1–S4 below are required implementation work before release;
the current Compose files and old engineering evidence do not implement or
qualify this smaller stack.

Delivery policy revision, 2026-11-22: humans review and merge every PR throughout
the MVP and factory rollout. Use `delivery_mode: "pull_request"`; automatic merge
is deferred behind the [factory maturity gate](software-factory-plan.md#later-automatic-merge-maturity-gate).
Existing automatic-merge implementation and old acceptance reports do not authorize
its use. This revision changes the planned acceptance contract, not runtime settings.

## Non-negotiable boundary

- Existing Docker Engine and Compose are the only runtime prerequisites. Setup
  must not install or upgrade them or change host configuration.
- Every web, database, broker, importer, supervisor, coding CLI, setup, validation,
  migration, CLI, test, qualification, browser, backup, and recovery process runs
  in a container, including development and CI checks.
- No host Python, virtual environment, pip installation, coding CLI, task Git
  subprocess, browser, background process, systemd unit, cron job, or lingering.
  Use Compose health checks and restart policies.
- No host Docker socket/API credentials, privileged containers, host PID/network
  namespaces, devices, or host security/sysctl changes. Privileged Docker-in-Docker
  is not a fallback.
- Runtime services do not bind-mount the host checkout, home, credentials, `.git`,
  `.runtime`, or arbitrary host paths. Released images contain source. Persistent
  data uses dedicated Docker named volumes; temporary job data uses bounded tmpfs.
- Host activity is limited to explicit operator Docker/Compose commands and
  deliberate repository/configuration file operations. Applications have no
  host-management interface. No automatic installer changes the machine.

## Architecture: four preprovisioned containers

Compose starts exactly four long-running containers for one active task. The
runtime does not create containers dynamically and needs no Docker daemon access.
Initialization/migrations run as ordered startup steps within these services.

| Container | Responsibility and boundary |
| --- | --- |
| `app` | Django UI/API and one supervised issue-import scheduling loop using shared import services and DB leases; no Celery/Beat or Valkey runtime dependency |
| `postgres` | Production application data only; preserve the existing database identity, credentials and volume |
| `runner` | Independent supervisor, claims, private Git/journal, PR delivery and human-merge reconciliation; direct worker client, selected account lifecycle and scoped egress mediation; no DB or engine access |
| `worker` | One trusted authenticated slot for sequential setup, agent/reviewer and independent validation phases; local disposable PostgreSQL/browser/app subprocesses only while needed |

The [factory footprint contract](software-factory-plan.md#required-footprint-four-always-on-containers)
defines the removals and phase boundaries. Gateway dispatch, account renewal and
model/package/account mediation live in the runner; they do not require separate
containers. Browser and test databases are phase-local processes in the worker's
immutable image, without production mounts or permanent sidecars. Later providers
select registered profiles in the same slot rather than add always-on fleets.

Explicit tools, packaging, qualification and backup/restore jobs may use temporary
containers. They stop after use and are counted separately. Normal task execution,
tests and browser review must work with just the four runtime containers. No hidden
required helper services or externally hosted substitutes for removed services.

A trusted worker PID 1 exposes authenticated prepare/start/status/pause/stop/export
operations. It validates registered recipes and artifacts and starts task code
under a separate unprivileged UID. The private control API is not published on the
host. Task data cannot select executables, mounts, images, permissions, credentials,
or Docker arguments. The supervisor communicates over authenticated private
container networking and keeps its checkout and journal outside worker access.

### Fresh jobs in reusable containers

A worker slot is a long-lived Compose container, not a fresh container per command
or role. Setup, agent/reviewer and validation phases execute sequentially.
Version the changed runtime protocol and report this distinction accurately.
Before every job, trusted initialization must confirm all previous task-UID
descendants are gone and clear source, Git metadata, HOME, caches, temporary output,
and backend material, including local test databases/services. Unknown termination
keeps the slot fenced. Seal/export source between phases and import a verified
fresh snapshot after reset; validation never inherits the builder's environment,
credentials or DB. Test cross-phase and cross-task source/process/secret isolation.

Use a read-only image, bounded writable job storage, deadlines, no task capabilities
or privilege escalation, and enforced egress. Any trusted network initialization
is limited to that container's own namespace, with minimal explicit capabilities
unavailable to task code; no host namespace or `SYS_ADMIN` is allowed.

Separate trusted PID 1/control paths and local PostgreSQL administration from
task UIDs. Only operation-scoped egress mediation and approved local test services
are reachable by task code. Deny access to runner account/control APIs, worker
management ports and production networks. Validation has no public egress. Private
network membership alone is not enforcement. Account secrets stay in the runner's
protected volume except the explicitly documented, short-lived selected CLI access.

Resource profiles must match limits the Compose slot actually enforces. A smaller
requested memory/CPU/workspace ceiling requires a matching slot or an explicit
rejection. Never report an unenforced per-job limit. Seal actual exit, descendant
termination and export evidence before releasing a slot. Worker restart reconciles
the durable operation and boot generation before accepting more work; lost output
is a failure, not an invented successful receipt.

## Validation and dependency changes

Retain Black, Ruff, mypy, migrations, system checks, the full Django suite, CLI,
portable runner tests and Git acceptance. Task packaging checks run in the worker;
developer/CI distribution jobs may use explicit disposable tools containers.
Commands and tool configuration belong to the immutable worker image, not candidate
scripts. Each check gets a fresh workspace and, when needed, a fresh local test
PostgreSQL cluster/database/role started by trusted worker code. Pin its binaries
in the worker image; no test database container or production DB connection is
required. Trusted reset code alone has administration credentials. Stop all test
services and discard their bounded storage before phase/slot reuse.

Dependency changes produce a bounded, digest-bound wheelhouse or environment
artifact in the credential-free setup phase. Verify it before use and keep it
from replacing image-owned validation tools or worker-server imports. No runtime
component builds/commits Docker images or changes production dependencies in place.
Operator/CI Docker builds produce updated base images for separate qualification.

## Required simplification branches

Original feature IDs remain stable. Each correction has a new branch, issue and
PR from updated main. D1–D4 remain packaging, execution-boundary, validation and
operations acceptance categories; the old topology-specific branch sequence is
superseded by S1–S4. Earlier reports remain historical. Each S branch must include
implementation, migrations/configuration as needed and its named checks.

| Order | Branch | Depends on | Deliverable |
| --- | --- | --- | --- |
| S1 | `feat/compact-app-scheduler` | Existing app/queue | Extract import services from Celery wrappers; one supervised DB-leased scheduling loop in app, no Celery/Beat/Valkey runtime dependency, ordered idempotent initialization/migrations. Verify duplicate starts, backoff, pause-independent import, child failure/signals, and upgrade without data loss. |
| S2 | `feat/compact-runner-control` | Existing supervisor | Direct authenticated worker client, account lifecycle and operation-scoped egress mediation as shared runner modules with one journal/lifecycle; remove gateway/account/proxy sidecars and obsolete daemon entrypoints. Test endpoint/phase isolation, selected auth/renewal and restart/lost-operation reconciliation without host credentials. |
| S3 | `feat/compact-phase-worker` | S2, existing validation contracts | One immutable phase-switching worker with pinned CLI/browser/PostgreSQL tools, local disposable services, fresh independent checks, UID/control separation, reset/stop/export and real resource limits. Test sequential phases, stale grants, cross-phase leaks, DB/admin isolation, OOM/cancel/restart and browser receipts. |
| S4 | `test/compact-stack-acceptance` | S1–S3 | Replace the old Compose topology with exactly four runtime services and in-service startup steps; preserve production/account/runner state. Qualify D1–D4 engineering gates, CI, CLI, packaging, drain/backup/restore, service count, aggregate limits and failure recovery on that stack. No paid model calls required. |

S1–S4 are P0 prerequisites. Requalify features 02, 03, 04, 05, 06 and 15 against
the smaller stack. F6 supplies the real Codex two-issue MVP/factory proof with human
merges after S4 and role integration; do not require that live proof to implement
or run the engineering checks. A larger stack cannot satisfy compact acceptance.

## Operations and transition

The supported interface is Docker/Compose build, up, logs, exec, and one-shot run
commands. All Python/Git/browser/test examples in technical references are commands
inside the appropriate container, never host commands. Configure connections in
the UI or an explicit Docker bootstrap job; never scan/import the operator's home
or coding-agent login. There is no host `.runtime` directory in the deployment.

Preserve the production database volume, encryption key, connections, queue and
history. Drain/stop old controllers and importers before transition, reconcile
pending Celery work into durable state, and retain the old Valkey volume for an
explicit rollback/retention decision. Removal from Compose must not delete it.
Preserve registered account identities/renewal state when moving account storage
into runner ownership; verify permissions, restart reuse and rollback compatibility.
Initialize fresh Docker worker state and runner identity where
deleted host state cannot be recovered safely. Explicitly retire unused host
identities. Never copy controller proofs, backdate eligibility, or relabel old
qualification as current readiness. Reconcile unfinished GitHub publications
before claims; do not duplicate an agent, branch or PR. Observe human merges
through read-only reconciliation; do not dispatch merges during this rollout.

Containers write backups to a designated backup volume. Verify restore in a
disposable Compose project. Export to a host path only through an explicit
operator-selected Docker copy. Cleanup of backups, credentials, volumes or
rollback data is separate from startup. Upgrades drain work, back up state, update
images, migrate in a container and check health; rollback uses compatible images
and data. No host scripts, schedulers, packages or global configuration changes.

Automatic self-updates are outside the MVP. Feature 12 must keep these same
boundaries and cannot introduce a socket mount, privileged updater or host service.
Image publication and update notification can precede a separately designed,
appropriately scoped automatic deployment path.

## Release evidence

Qualification and acceptance code runs in containers on the actual target stack.
Explicit operator Docker/Compose inspection can supply selected configuration
facts; no qualification container receives a host engine socket.

- A clean machine with existing Docker/Compose starts the complete stack without
  package, service, shell/profile, home/auth, or security-setting changes.
- Rendered production Compose has no host-path/socket/device mounts, privileged
  services, host namespaces or daemon credentials. Only declared volumes/tmpfs
  are writable; images contain released source.
- Default runtime plus the runner profile declares exactly four long-running
  containers, including the selected account connection. No init/migration,
  broker, proxy, gateway, account or test-DB sidecar is required. Temporary operator
  jobs stop after use; normal build/check/browser phases add no containers.
- App web/import and runner control/mediation children fail and restart visibly;
  scheduling leases prevent duplicate imports. Startup is retry-safe after partial
  initialization/migrations. Restarts cannot duplicate paid sessions or PRs.
- Local test PostgreSQL/browser/app subprocesses share the worker's enforced CPU,
  memory, PID, disk and time limits. Cross-phase checks prove no surviving process,
  source, credential or DB state; failed cleanup fences the slot. Record measured
  idle and peak use rather than assume a smaller count guarantees lower memory.
- Real tasks cannot read host sentinels, control the engine, reach LAN/metadata or
  production services, forge receipts, or survive confirmed cancellation. Verify
  resource exhaustion, worker restart and cross-job source/secret/DB isolation.
- All tests, packaging, migrations, backups/restores and actual browser PNGs run
  in containers. CI uses the same images/profiles. Old host-based reports cannot
  qualify this release.
- Two owner issues, B depending on A, are discovered, coded, independently checked
  and published automatically. A human reviews and merges each PR; B uses A's
  verified human merge. Delivery releases claim/worker capacity while review waits,
  and independent work can proceed. Record final trees, CI identities, human merge
  actors/timestamps, restart/termination/ACK evidence, ancestry, zero runner merge
  calls and zero command/plan approvals, including failed/recovered generations.

Failure keeps execution disabled. There is no host-runner, socket, privileged-mode
or per-command/plan approval fallback. Human PR review and merge remain required.
