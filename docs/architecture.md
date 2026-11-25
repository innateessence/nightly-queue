# Four-container runtime

Vibearr has four persistent services: `app`, `postgres`, `runner` and `worker`.
The app supervises Django and durable PostgreSQL issue-import jobs. PostgreSQL
owns application data. The trusted runner owns the controller journal, schedules,
publication credentials, subscription logins and scoped network mediation. One
worker sequences setup, coding/reviewer roles, validation and disposable test
database/application/browser processes.

Runtime services receive no Docker daemon, host namespaces or host checkout/home
mounts. Images are immutable, root filesystems are read-only, and writable state
uses private named volumes or bounded tmpfs. Production database and application
credentials are absent from runner/worker task environments.

The worker control API and mediation administration require distinct private
tokens. Candidate code runs with an unprivileged task UID, empty capabilities,
no-new-privileges and bounded CPU, memory, PID, workspace and log limits. Candidate
applications use a separate restricted UID. They cannot access runner journals,
persistent logins, production networks, trusted tools or worker control state.
Only scoped phase mediation is reachable; direct network and metadata access are
denied. Repository content and issue text cannot add permissions.

Account login is explicit operator activity in the runner. Known authentication
files persist in private per-provider/per-credential directories. Tasks receive
only a selected ephemeral copy. Subscription renewal is retained only after
confirmed task termination. OpenCode's external Ollama endpoint/model is explicit
configuration; redirects, metadata targets, arbitrary hosts and model-management
requests are denied.

Qualification binds actual source, immutable images, private execution instance,
current worker boot, registered profiles and runtime limits. Real probes cover
trusted-state and network denial, task UID/capabilities, descendant cleanup,
phase/database reset, interrupted-operation recovery, deadlines, pause/resume,
PID and memory pressure, bounded logs and authenticated control. Restarting a
worker invalidates its proof. Authentication files or a CLI version do not prove
a working selected account/model.

Trusted candidate validation uses fresh snapshots and image-owned commands.
Dependency changes produce run-bound artifacts without replacing trusted tools.
Visual review requires actual source-bound browser captures. Delivery checks
source/export digests, protected receipts, checks, reviews and repository identity
before publishing a PR. A human reviews and merges it.

All tests, migration, qualification, packaging, browser and recovery processes
also run in Docker. Maintenance jobs are temporary and do not add persistent
services. See [operations](docker-operations.md), [execution policy](runner-execution-policy.md),
[recovery](runner-recovery.md), [factory execution](software-factory-plan.md) and
[release acceptance](vibearr-mvp.md).
