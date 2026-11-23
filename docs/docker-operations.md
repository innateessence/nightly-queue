# Docker operations

Vibearr ships three long-running containers: `app`, `postgres`, and `runner`.
Codex and Claude Code use subscriptions; OpenCode uses external Ollama that the
operator provides. The stack never starts Ollama or downloads models.

All Python, Git, agent, test, browser and recovery processes run in Docker. Host
activity is explicit Docker/Compose orchestration and source/configuration edits.
No host checkout/home/credential mounts, Docker socket, privileged containers,
host namespaces, or automatic production deployment are supported.

## Fresh installation

```sh
cp .env.example .env
bash run.sh --build
docker compose run --rm app python manage.py createsuperuser
```

`run.sh` builds application/runner images, inspects the actual runner image ID,
and starts the complete stack. Override image names through deployment
configuration. Direct Compose startup requires `VIBEARR_RUNNER_IMAGE_ID` to match
the installed runner image; no runtime component can inspect or control Docker.

App startup initializes private named credential volumes without replacing saved
values, waits for PostgreSQL, migrates, and provisions the configured dedicated
runner identity. It then supervises the web server and PostgreSQL import
scheduler. The runner starts idle. Processing is disabled by default; migrations
from the old fleet disable existing runner configurations for review.

The UI is available at `http://127.0.0.1:7777`. Add projects, select a separate
GitHub issue-import credential, and configure runner repository/issue credentials
and schedules. Manual issue sync queues a durable job; its latest result appears
on the queue page. The scheduler uses configured intervals, leases, idempotent
imports and retry delays, independently of coding execution.

## Subscription login

Create a credential with provider **Codex** or **Claude Code** and type **Runner
subscription login**, leaving the secret blank. Save it, open its edit page, and
use the displayed command with that credential's actual ID:

```sh
docker compose exec runner python -I -m vibearr_executor.managed_login login codex --credential-id 12
docker compose exec runner python -I -m vibearr_executor.managed_login login claude --credential-id 13
```

Complete the displayed vendor authorization link on your own device. Codex uses
its device-code flow; enable that login method in the ChatGPT account/workspace
when required. Claude Code uses its subscription browser/code flow. No host
login files are scanned, mounted or copied. No model API key is required.

The same command accepts `status` or `logout` instead of `login`. Stop or drain
active coding work before changing accounts; an execution lock prevents overlap.
Only known authentication files are persisted in `runner-logins`, with separate
provider/credential directories. Tasks receive a selected copy in an ephemeral
home. After confirmed termination, refreshed login material is retained for the
next task. Credential files never become source or Git delivery artifacts.

Select the saved login and model in the coding-runner configuration. Add explicit
preflight grants for each selected credential through Django admin, or provision
those exact IDs with the existing command:

```sh
docker compose run --rm app python manage.py docker_bootstrap_runner docker-runner --credential-id 12 --credential-id 13
```

GitHub issue/read and repository/delivery grants remain separate. Saving login
metadata is not live authentication proof. Login expiry and subscription limits
must block work rather than fall back to model APIs. Unsupported remaining quota
telemetry stays unsupported; enabling reserve requires fresh supported readings.

## External Ollama for OpenCode

Create an **OpenCode connection** credential using **External Ollama** and the
origin of your server, for example `http://192.168.1.20:11434` or
`https://ollama.example`. Select the installed model in runner settings, such as
`qwen3-coder:30b`. The endpoint must be reachable from the runner's Docker network;
container loopback is not an external server. No host-discovery alias is supplied.

The endpoint is explicit operator configuration, never issue/repository input.
URL credentials, query strings, metadata addresses, internal service names and
arbitrary paths are rejected. A local relay permits inference/model inspection
for the selected model, blocks redirects and model-management requests, and
prevents OpenCode from selecting a cloud fallback. A missing server/model must be
reported without starting an Ollama service or downloading anything.

## Qualification and readiness

Drain active work before qualification. Start a fixed non-model witness, interrupt
the runner container, then qualify its new boot without making model calls:

```sh
docker compose exec -T runner python -I -m vibearr_executor.integrated restart-witness
docker compose kill -s SIGKILL runner
docker compose up -d --wait runner
docker compose exec -T runner python -I -m vibearr_executor.integrated qualify
```

The actual container must match its registered read-only image and CPU, memory,
PID and workspace ceilings. Probes check task UID/capabilities, denial of trusted
state and control access, direct egress denial, detached-descendant termination,
cleanup between jobs, pause/resume, deadlines, cancellation and replay. The
interrupted witness must reconcile without a new execution or fabricated result.
Every runner restart invalidates its proof and needs a fresh interrupted witness.

Native account/model probes run separately, only for eligible work with selected
credentials/grants. Fixture responses, `--version`, stored auth files, and old
fleet proofs are not live coding/delivery acceptance. Keep processing disabled
until the selected real account/model and designated repository are accepted.
Never interpret unverified quota as unlimited capacity.

The controller, execution control, CLIs, package/model mediation and temporary
test PostgreSQL share one container with different OS identities. This provides
less isolation than separate containers. Limits apply to the entire runner,
including trusted processes and the test server. The agent can access its own
selected subscription credentials; do not claim otherwise. See the
[runtime contract](docker-only-runtime-plan.md).

## Development and CI

```sh
bash test.sh
bash test.sh work_queue.test_api
bash deploy/docker/checks.sh boundary
bash deploy/docker/checks.sh operations
```

Checks create uniquely named disposable Compose projects, refuse existing state,
clear deployment credential environment, use random loopback web ports, and
remove only their own containers/volumes on exit. They do not start paid agents
or modify the live stack. Logs and public evidence are exported to a fresh
`/tmp/vibearr-check-*-reports` directory; `VIBEARR_CHECK_OUTPUT` overrides it.

The full run covers application/portable tests, formatting/types, migration drift,
actual boundary/restart qualification, all nine trusted candidate checks, browser
PNGs, and quiescent backup/restore with fresh authority. Candidate validation
uses fresh snapshots, trusted image tools, and local disposable PostgreSQL inside
the runner. Dependency changes use run-bound artifacts without replacing trusted
tools or building runtime images.

Use `--candidate <checkout> --commit <sha>` for exact Git object input. The
operator copies only objects/ref metadata into an input image, never host Git
configuration, hooks or credentials. Git reads and source freezing happen in a
container. Local runs without exact input are labeled disposable snapshots and
must not be presented as proof of a committed revision.

The consolidated GitHub required check is **Application, runtime and recovery**
in **Three-container checks**. Before switching branch protection or a configured
merge profile, require this check and remove superseded worker-fleet check names.
Existing required-check settings are not changed automatically. The separate
manual deployment workflow inspects an already qualified installation and does
not start agents or enable processing.

## Drain, upgrade and migration from the old fleet

Retain the old image IDs, Compose recipe, database/user names, data-volume project,
credential encryption key, application/API keys, runner identity and journals.
Drain using the currently running old controller and its documented controls;
wait for acknowledgement and reconcile unfinished publications before stopping
it. Do not drop journals to make an upgrade appear idle.

Take and verify a quiescent backup using that version's backup job. Version-1
fleet backups must be restored with the corresponding old image/recipe before
migration. Version-2 integrated backups retain their original six-volume recipe.
The four-service runtime writes version-3 `compact-worker-v1` backups with an exact
mapping of application-secrets, database-secrets, runner-secrets, runner-data,
runner-logins, worker-secrets and worker-control. Old backups and volumes remain
available for rollback and are never relabeled as compact snapshots.

For an already integrated deployment, drain while the controller is running:

```sh
docker compose exec --user 10001:10001 runner python -I -m vibearr_runner.drain on
docker compose exec --user 10001:10001 runner python -I -m vibearr_runner.drain wait --timeout 3600
docker compose stop runner worker app
bash deploy/docker/operations.sh backup --quiesced
```

The backup job requires the explicit `--quiesced` flag, no running app/runner/worker
services and no active database runs. PostgreSQL remains running for its logical
dump. It preserves encrypted connections, original keys, journals, execution
receipts and subscription login state with hashes, ownership and permissions.
Treat backups as credentials. Taking a backup never restarts processing.

Keep original `.env` names/values on migration. Missing original credentials
stop initialization before replacement secrets can be generated. Existing
`postgres-data` and `runner-data` names remain stable through
`VIBEARR_DATA_PROJECT`. The runner mounts its state volume at both canonical and
legacy paths so retained absolute workspace paths remain resolvable.

Build/start the new stack, inspect migration results, reconnect selected native
logins, qualify the new boot and validate delivery. Runtime changes invalidate
old execution proofs. Preserve uncertain old operations for explicit recovery;
never replay them as new tasks. Only after acceptance should the operator remove
specifically identified old worker/proxy/broker and development containers.
Retain their volumes and old images until rollback has been verified. Never use
blanket Docker pruning or affect unrelated/external Ollama services.

For routine upgrades, rebuild reviewed images with `bash run.sh --build`, qualify
the new boot, check recovery and account readiness, then explicitly clear the
drain and enable the intended schedules. Rollback across migrations may require
restoring the matching snapshot; automatic schema rollback is not promised.

## Restore into a separate project

Use a new `COMPOSE_PROJECT_NAME` and the same new `VIBEARR_DATA_PROJECT`, retaining
the database/user names from the snapshot. Set `VIBEARR_RESTORE_BACKUP_VOLUME` to
the original source backup volume and `VIBEARR_RUNNER_NAME` to a new identity name.
All target volumes must be empty; source backups are mounted read-only.

With those values exported in the operator shell or deployment configuration:

```sh
bash deploy/docker/operations.sh restore-files BACKUP_UUID
bash deploy/docker/operations.sh restore-database BACKUP_UUID
bash deploy/docker/operations.sh restore-bootstrap BACKUP_UUID
bash run.sh --build
```

File restoration validates the complete archive before extracting. Database
restore uses one transaction and verifies table fingerprints, then disables old
identities and processing. Explicit restored bootstrap archives old controller
state, preserves history, and creates fresh runner authority. It also archives the
restored worker's control state and rotates both control and mediation tokens
before the worker can start. Retired tokens and old qualification reports stay in
private restore history. The new worker needs a forced-restart witness, boundary
and role/browser qualification; the runner needs account/delivery acceptance
before it can process work. Keep
source backups and rollback state until this separate restoration is verified.

## Browser and artifacts

```sh
bash deploy/docker/operations.sh browser
```

This is a one-shot operator browser container with no credentials or host checkout. Its
PNGs/report stay in `<project>_browser-artifacts`. Exact-candidate CI supplies
source/image binding; unbound local smoke captures are not native merge evidence.
Export selected captures with explicit operator Docker copy and retain them with
release evidence. The worker's model-free `qualify-roles` step separately runs the
registered browser profile after boundary qualification and binds its captured
archive to the current worker boot and immutable source/image identities. Missing
or stale worker browser proof blocks visual reviewer roles. Browser artifacts
are separate from private-state backups.
