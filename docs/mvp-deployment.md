# Deploy the MVP with Docker Compose

Every vibearr process runs in Docker: web, database, importer, supervisor, coding
workers, validation, tools, qualification, browser capture, backup and recovery.
Existing Docker Engine and Compose are the only runtime prerequisites. Installation
and operation never install host packages, services, virtual environments, coding
CLIs or browsers, or change host security, shell or lingering configuration.

The [Docker-only runtime plan](docker-only-runtime-plan.md) defines the architecture,
correction branches and release gates. It replaces the withdrawn host-service
procedure. The current Compose file does not yet provide the complete replacement;
keep autonomous execution disabled until D1–D4 are implemented and qualified.

## Runtime layout

Compose owns services, health checks, restart policies, private networks, named
volumes and bounded tmpfs. Released images contain source and pinned tools.
Production services do not mount the host checkout, home, credentials, `.git`,
`.runtime`, Docker socket, devices or arbitrary host paths. No privileged container,
host namespace or Docker-engine reconfiguration is part of this deployment.

The supervisor uses authenticated APIs on fixed worker slots provisioned by Compose.
Trusted worker PID 1 separates task UIDs from control state, runs registered recipes,
confirms descendant termination and resets all job state before reuse. Agent,
setup and validation workers have separate credentials and network rules. Test
PostgreSQL slots never share production data. No runtime component controls a daemon.

## Setup and upgrade sequence

1. Build pinned images and inspect the rendered Compose configuration. Reject
   forbidden mounts, privileges and exposed control/test ports.
2. Start production database, broker, web and import containers. Apply migrations
   through the application container, preserving existing data and encryption keys.
3. Run a Docker bootstrap job to initialize private named volumes, choose saved
   connections and worker profiles, and provision a runner identity. Keep claims
   disabled. Never discover credentials by reading the operator's home or login.
4. Start the controller and worker slots on private Docker networks. The normal
   stack publishes only the intended application port.
5. Run containerized qualification against actual images, limits, networks, journals,
   reset/stop behavior and worker boot identities. Old host-based proofs are historical.
6. Verify no-bypass GitHub policy and required CI. Run the bounded dependent two-issue
   scenario, browser-container screenshots, recovery and backup/restore acceptance.
   Enable routine processing only after these gates pass.

These are implementation requirements, not instructions to start nonexistent tool
services. A missing Docker tool must not be replaced with a host command.

## State, credentials and recovery

Preserve `postgres-data`, `valkey-data`, the encryption key, saved connections,
queue and history. Use separate Docker-managed runner, worker, artifact, secret
and backup volumes with minimal access. No model credential enters validation;
no control token, production database connection or engine access enters a task.

The old host `.runtime` directory is not part of the deployment. Initialize fresh
Docker identities where old host state was deleted and explicitly retire unused
host identities. Reconcile unfinished GitHub publications without duplicating work,
copying controller proofs or manufacturing successful completion.

Before upgrades, stop claims, drain/checkpoint owned work and produce consistent
container-generated backups. Verify restore in a disposable Compose project.
Update images, migrate in a container, check health, then enable claims. Rollback
uses compatible images and data. Host export is only an explicit operator-selected
Docker copy; startup never deletes backups, credentials or rollback data.

Use Docker logs and container health for operations. There are no host service
files, schedulers, package installers or global configuration edits to maintain.
