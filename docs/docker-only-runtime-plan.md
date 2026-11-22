# Docker-only MVP runtime

This contract supersedes host-executor and host-service deployment instructions.
It describes the implementation and release requirements. See
`docker-only-progress.json` and `acceptance/docker-runtime.md` for completed
engineering checks and outstanding deployment acceptance. Keep autonomous
execution disabled until the actual target stack is qualified.

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

## Architecture: preprovisioned worker slots

Compose starts fixed worker containers for the MVP's one active task. The runtime
does not create containers dynamically and needs no Docker daemon access.

| Container | Responsibility and boundary |
| --- | --- |
| Web, PostgreSQL, Valkey, import worker, Beat | Queue, encrypted connections, durable application data and discovery; preserve existing database/broker volumes |
| `vibearr-runner` | Claims, Git checkout, context, delivery, CI, merge and recovery; private runner volume and scoped API grants; no DB or engine access |
| Agent worker | Pinned Codex, then separate Claude/OpenCode profiles; only the selected model connection |
| Setup worker | Credential-free dependency resolution and artifact construction; package egress only |
| Validation worker | Trusted nine-check profile and fresh job snapshots; no model/GitHub credentials or public egress |
| Test PostgreSQL slots | Fresh job databases/roles, bounded separate storage, no production volume or published port |
| Model/package proxies | The job's registered public endpoint allowlist; no host/LAN/metadata access |
| Tools, acceptance, browser, backup jobs | Explicit one-shot container commands with narrowly declared volumes and inputs |

These are the required service boundaries. Missing tools must be implemented in
Docker; do not replace them with host commands.

A trusted worker PID 1 exposes authenticated prepare/start/status/pause/stop/export
operations. It validates registered recipes and artifacts and starts task code
under a separate unprivileged UID. The private control API is not published on the
host. Task data cannot select executables, mounts, images, permissions, credentials,
or Docker arguments. The supervisor communicates over authenticated private
container networking and keeps its checkout and journal outside worker access.

### Fresh jobs in reusable containers

A worker slot is a long-lived Compose container, not a fresh container per command.
Version the changed runtime protocol and report this distinction accurately.
Before every job, trusted initialization must confirm all previous task-UID
descendants are gone and clear source, Git metadata, HOME, caches, temporary output,
and backend material. Unknown termination keeps the slot fenced. Test that task A
cannot read or affect task B's data, processes, or credentials.

Use a read-only image, bounded writable job storage, deadlines, no task capabilities
or privilege escalation, and enforced egress. Any trusted network initialization
is limited to that container's own namespace, with minimal explicit capabilities
unavailable to task code; no host namespace or `SYS_ADMIN` is allowed.

Resource profiles must match limits the Compose slot actually enforces. A smaller
requested memory/CPU/workspace ceiling requires a matching slot or an explicit
rejection. Never report an unenforced per-job limit. Seal actual exit, descendant
termination and export evidence before releasing a slot. Worker restart reconciles
the durable operation and boot generation before accepting more work; lost output
is a failure, not an invented successful receipt.

## Validation and dependency changes

Retain Black, Ruff, mypy, migrations, system checks, the full Django suite, CLI,
portable runner tests and Git acceptance. Packaging checks run in tools containers.
Commands and tool configuration belong to pinned validation images, not candidate
scripts. Each check gets a fresh workspace and, when needed, a fresh test database
and role on a preprovisioned test PostgreSQL slot. Trusted reset code alone has DB
administration credentials. Confirm session/data cleanup before reuse.

Dependency changes produce a bounded, digest-bound wheelhouse or environment
artifact in the credential-free setup worker. Verify it before use and keep it
from replacing image-owned validation tools or worker-server imports. No runtime
component builds/commits Docker images or changes production dependencies in place.
Operator/CI Docker builds produce updated base images for separate qualification.

## Required correction branches

Original feature IDs remain stable. Each correction has a new branch, issue and
PR from updated main. Dependencies below describe code integration order; final
release acceptance of the original features follows D1–D4, avoiding a circular
requirement to qualify production before implementing its runtime. Old host-based
signoffs do not satisfy Docker-only acceptance.

| Order | Branch | Depends on | Deliverable |
| --- | --- | --- | --- |
| D1 | `feat/docker-only-packaging` | — | Immutable app/controller/tool images, complete Compose services, health/restart policies, private networks, named volumes and secret initialization; no production source bind |
| D2 | `feat/socketless-worker-slots` | D1, policy 01 | Worker APIs, UID separation, reset/stop/export, boot reconciliation, egress and enforced slot limits; replace host Docker dispatch |
| D3 | `feat/docker-only-validation` | D2, validation 15 | Nine checks, fresh test databases/roles, sealed dependency artifacts and exact-tree evidence through worker APIs |
| D4 | `feat/docker-only-operations` | D1–D3, bootstrap 06, human-merge reconciliation 05/06 | Containerized setup, migrations, CLI, CI, qualification, browser screenshots, backup/restore, delivery recovery and real dependent two-issue acceptance with human merges |

D1–D4 are P0 MVP prerequisites. Requalify features 02, 03, 04, 05, 06 and 15 against
this boundary before enabling claims. Boundary qualification needs no paid agent.

## Operations and transition

The supported interface is Docker/Compose build, up, logs, exec, and one-shot run
commands. All Python/Git/browser/test examples in technical references are commands
inside the appropriate container, never host commands. Configure connections in
the UI or an explicit Docker bootstrap job; never scan/import the operator's home
or coding-agent login. There is no host `.runtime` directory in the deployment.

Preserve the production database/broker volumes, encryption key, connections,
queue and history. Initialize fresh Docker worker state and runner identity where
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
