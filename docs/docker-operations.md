# Docker operations

Use Docker Engine and Compose already installed on the machine. Every Python,
Git, test, browser, backup and recovery process runs in an image. Do not mount a
checkout, home directory or engine socket into a service. D4 CI and final release
acceptance remain in progress; keep autonomous processing disabled.

## Initialize and bootstrap

Build images from the reviewed checkout and start the application with Compose.
`initialize` creates private credentials in named volumes, and `migrate` applies
migrations before the web service starts. Source and static files are in images.
Use the `tools` profile for management commands:

```sh
docker compose build
docker compose up -d --wait
docker compose --profile tools run --rm tools python manage.py createsuperuser
docker compose --profile tools run --rm tools python manage.py docker_bootstrap_runner docker-runner
```

Bootstrap generates a dedicated runner API token atomically inside the runner
secret volume. Repeating the command preserves both identity and token. Existing
identities with missing, different or disabled authority require explicit
reconciliation. Add `--credential-id <id>` for each credential whose preflight
access you explicitly grant. Bootstrap does not select connections, acquire a
controller, enable processing or run a model. Configure connections through the
UI or the existing explicit application bootstrap command; never import host
agent logins or scan the operator's home.

Existing PostgreSQL data requires the original database password, queue API
token, credential encryption key and Django key. Supply their original values
through the deployment configuration before the first initialization. A saved
volume takes precedence only when values agree; conflicts fail. Missing original
credentials stop initialization before replacement credentials can be generated.
Preserve original database/user names and `VIBEARR_DATA_PROJECT` on upgrade.

## Worker readiness

The runner profile uses the actual immutable worker and PostgreSQL image IDs in
`VIBEARR_AGENT_IMAGE_ID`, `VIBEARR_WORKER_IMAGE_ID` and
`VIBEARR_DATABASE_IMAGE_ID`. Obtain these using explicit operator
`docker image inspect` commands after building the selected images. The runtime
cannot access the engine and cannot discover or create containers itself.

Before qualification, ensure no real task owns a slot and stop the supervisor.
Qualification interrupts a fixed synthetic witness and requires a fresh worker
boot. It makes no paid model calls:

```sh
docker compose --profile runner up -d --wait agent-worker setup-worker validation-worker worker-gateway
docker compose --profile checks run --rm --no-deps qualification prepare
docker compose --profile runner kill -s SIGKILL agent-worker setup-worker validation-worker
docker compose --profile runner up -d --wait agent-worker setup-worker validation-worker worker-gateway
docker compose --profile checks run --rm --no-deps qualification finish
docker compose --profile checks run --rm --no-deps resource-check
```

Every worker restart invalidates its previous boot proof. Successful fleet
qualification creates the private validation profile. Model readiness and
delivery authority require separate checks. The supervisor is started explicitly
only after those checks and release acceptance; qualification does not start it.
New native receipts use schema 2 with the protected slot instance, boot and
binding digest. They identify reusable workers without inventing per-task
container IDs. Historical container receipts remain readable; they cannot supply
the new worker restart acceptance evidence.

The disposable validation job exercises all nine checks from the installed
trusted recipe. `--dependency` adds a synthetic dependency change and verifies
the setup-to-validation artifact flow:

```sh
docker compose --profile checks run --rm --no-deps --entrypoint python qualification -m deploy.docker.validate
docker compose --profile checks run --rm --no-deps --entrypoint python qualification -m deploy.docker.validate --dependency
docker compose --profile checks run --rm --no-deps browser
```

Browser PNGs and its JSON report stay in `browser-artifacts`. Export only selected
artifacts using an explicit operator Docker copy; no browser runs on the host.

## Development and CI

`bash test.sh` builds a disposable `vibearr-check-*` Compose project and runs the
application suite, formatting/type checks, offline distributions, worker boot and
resource qualification, all nine candidate checks, dependency-change validation,
browser captures and a fenced backup/restore. It never reuses deployment volumes
or starts a paid agent. It removes its own containers and volumes on exit and
prints the directory containing exported logs and public evidence.

For a focused application suite, use `bash test.sh work_queue.test_api`. Run
`bash deploy/docker/checks.sh boundary` or `operations` for those standalone
checks. `VIBEARR_CHECK_OUTPUT` selects a new evidence directory; the command
refuses an existing directory or disposable project with existing state.

CI takes the check recipe from the immutable PR base and imports candidate Git
objects as data without configuration, hooks or credentials. The validation job
checks the exact candidate commit through qualified workers; packaging runs with
no network or credentials under the separate task UID. A pinned introduction
commit supplies the recipe when the PR base predates Docker checks. Local runs
without `--candidate` validate a disposable snapshot and do not claim an exact
committed revision. Use `--candidate <checkout> --commit <sha>` for exact input.
Run `candidate`, `boundary` and `operations` separately for that input. Boundary
and operations build the candidate implementation with the protected Dockerfiles
and Compose recipe in their own disposable projects. Candidate validation keeps
its worker and checking tools from the protected recipe, so candidate runtime
changes cannot replace their own validation controls. These CI observations do
not replace qualification on the actual deployment after review.

## Drain and upgrade

Request a drain while the current supervisor is running, wait for that request's
acknowledgement, then stop it. Owned work and recovery continue while new claims
are fenced. A timeout leaves the drain request in place and requires inspection;
it does not establish that work has stopped.

```sh
docker compose --profile tools run --rm --no-deps tools python -m vibearr_runner.drain on
docker compose --profile tools run --rm --no-deps tools python -m vibearr_runner.drain wait --timeout 3600
docker compose --profile runner stop vibearr-runner
```

Retain the current immutable image IDs and take the quiescent backup below before
changing images. Build or select reviewed replacement images, preserve the same
data project, and run migrations through Compose. Check application health and
qualify every updated worker boot before restarting a previously active
supervisor. The persistent drain still prevents claims during those checks.
Only after recovery, connection readiness and release acceptance are satisfied,
clear the drain explicitly and start that supervisor:

```sh
docker compose --profile tools run --rm --no-deps tools python -m vibearr_runner.drain off
docker compose --profile runner up -d --no-deps vibearr-runner
```

Rollback requires compatible images and schema or an isolated restore of the
matching snapshot. Do not promise automatic rollback across irreversible
migrations. Keep processing disabled until the full Docker release acceptance
has succeeded.

## Backup

Drain and stop the supervisor using the commands above before backup. The backup
job refuses a database with active runs.
Stop all writers before declaring the snapshot quiescent; leave PostgreSQL
running for its transaction-consistent logical dump:

```sh
docker compose --profile runner stop vibearr celery-worker celery-beat agent-worker setup-worker validation-worker worker-gateway valkey
docker compose --profile operations build backup
docker compose --profile operations run --rm --no-deps backup backup --quiesced
```

The job prints a backup UUID after publishing a complete snapshot. It preserves
PostgreSQL data, encrypted connections and original keys, broker files, worker
journals and runner/Git/recovery state. It hashes every stored file, records
ownership and modes, checks database table fingerprints before and after the
dump, and requires space for the snapshot plus a 1 GiB reserve. Links and setuid
files are rejected. Broker sticky directory permissions are preserved.

Backups contain credentials: protect the named backup volume and any explicit
export. A failed attempt remains under `.incomplete-<uuid>` and is never treated
as a complete backup. Cleanup is a separate operator action. Startup never
deletes backups, credential volumes, PostgreSQL data or rollback state.

Resume only the services that were running before backup. Restarted workers need
new qualification before real work. Do not start a previously stopped supervisor
as a side effect of taking a backup.

## Restore into a separate project

Select a new Compose project and set `VIBEARR_DATA_PROJECT` to that same new
name. Set `VIBEARR_RESTORE_BACKUP_VOLUME` to the original backup volume name.
The source backup is mounted read-only and declared external, so target-project
cleanup cannot delete it. All target state and PostgreSQL volumes must be empty;
restoration never overwrites an existing deployment.

Use the original database/user names from the snapshot. With those variables set
for the new project, run:

```sh
docker compose --profile operations run --rm --no-deps restore-files restore-files <backup-uuid>
docker compose up --no-deps -d --wait postgres
docker compose --profile operations run --rm --no-deps restore-database restore-database <backup-uuid>
docker compose run --rm --no-deps initialize
docker compose --profile tools run --rm --no-deps tools python manage.py docker_bootstrap_runner restored-runner --restored-backup <backup-uuid>
docker compose up -d --wait
```

File restoration verifies the entire archive before extraction. Database restore
uses one transaction and verifies table fingerprints. It then disables restored
runner identities, clears their controller authority and disables processing;
business data fingerprints are checked again after that deliberate fence.
The restored runner volume remains marked as requiring a new identity.

The explicit restored bootstrap requires a new runner name. It archives the old
controller journal under `runner-data/restore-history/<backup-uuid>` and retires
the old runner token/profile/proof files before generating a new token. It never
copies the old controller proof into the new active journal. Grant selected
credential IDs explicitly again and requalify fresh worker boots.

Inspect and reconcile archived GitHub publications before claims. Keep the old
deployment fenced: a restore test must never start a second publishing
supervisor. The disposable round-trip fixture checks encryption, queue state and
run history; it is not real delivery or dependent-issue release acceptance.
