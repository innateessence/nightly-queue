# nightly-queue

Queue work for your GitHub projects. Django stores it; n8n claims items and runs
an agent on a nightly schedule.

## Run locally

Copy `.env.example` to an untracked `.env` and set `NQ_API_TOKEN` for API access:

```sh
cp .env.example .env
# Edit .env, then start the containers:
./run.sh --build
docker compose exec nightly-queue python manage.py createsuperuser
```

1. Create a project on the [Projects page](http://127.0.0.1:7777/projects/)
   with an HTTPS GitHub repository URL and optional base branch. Leave the branch
   blank to use the repository default.
2. Open [the queue](http://127.0.0.1:7777/) and add work with a title, optional
   instructions, and priority.
3. Use n8n to process items oldest first. Priority does not change their order.

The **Autonomous** toggle on project creation and editing records whether a
project should automatically find open issues and create a pull request for each.
It defaults to off, including for existing projects. The project list shows the
selected mode, and the API exposes it as the boolean `autonomous` on each project,
including projects nested in queue item responses.

Celery Beat imports GitHub issues for every autonomous project at the interval
selected in Settings: **1h**, **4h**, **8h**, **12h**, or **24h**. The default is **1h**,
including for existing installations. The interval applies to all autonomous projects.
Choose an existing **GitHub Token** on the [Settings page](http://127.0.0.1:7777/settings/)
to enable imports. The token needs Issues read access to each repository.
The scheduler checks once a minute. The first sync runs on the next check; later
syncs are measured from the start of the last scheduled attempt. Interval changes
apply on the next check without restarting workers or Beat. Shortening the interval
can make a sync due immediately; lengthening it postpones the next sync. The last
scheduled attempt is stored in the database so restarts preserve the interval.
Click **Sync issues now** on the queue page to import issues from every autonomous
project immediately, without changing the scheduled timer. This uses the same task
as scheduled imports, so repeated clicks do not duplicate existing items or requeue
completed work. In development,
the sync runs immediately and reports how many items were added. With asynchronous
workers enabled, it starts the sync in the background; refresh the queue shortly
afterward to see the imported items.
All issues, including closed issues, appear alongside manual work in
[the queue](http://127.0.0.1:7777/); pull requests are excluded.
Imports fetch every page and refresh titles, bodies, and issue state without
creating duplicates. Titles longer than 200 characters are shortened for display.
Manual work and imported issues share one oldest-first queue. Each item shows
its origin as Manual or GitHub issue.

Only open issues on projects that still have Autonomous enabled can be claimed
from the queue. Closed issues remain visible, and previously completed work
is not automatically requeued if an issue reopens. Imports preserve running and
completed items' instructions and execution history. Issue identity includes the
project, repository, and issue number, so changing a project's repository does
not overwrite another repository's issues. Imported issues are managed on GitHub;
imported items cannot be removed from the queue page.

Missing credentials or an unavailable encryption key skip imports. API failures
are logged by project ID and retried on the next scheduled run; other projects
continue importing. Clearing the GitHub selection pauses imports. The GitLab API
credential selector accepts existing GitLab tokens for future integrations;
repository URLs and issue imports currently support GitHub only. Creating pull
requests still requires a separately configured runner.

The queue, project, credential, and settings pages have no login requirement.
This is a single-user app for a private LAN.
API access requires
`Authorization: Bearer <NQ_API_TOKEN>`; an unset token disables the API. Use HTTPS
for API access over a network.

PostgreSQL 17 stores data in the `postgres-data` Docker volume. Its port stays
internal to Docker. `docker compose down` keeps the data; `down --volumes` deletes it.
The database, user, and password default to `nightly_queue`. Override them with
`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` in `.env`.
Database credentials initialize an empty volume; changing `.env` does not change an existing database password.

Back up PostgreSQL with:

```sh
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > backup.sql
```

## Repository credentials

GitHub and GitLab tokens, SSH private keys, and other secrets are stored encrypted
in PostgreSQL. Generate
an encryption key once after building the app image:

```sh
docker compose run --rm --no-deps nightly-queue python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set `NQ_CREDENTIAL_KEY` to that value in your untracked `.env`, then recreate the
app and Celery worker containers with `./run.sh --build`. For a native setup,
export the variable and run `python manage.py migrate`. There is no default encryption key; credential
creation and decryption fail if it is missing or invalid. Existing projects keep
working without one until you add credentials.

Open the [Credentials page](http://127.0.0.1:7777/credentials/) from the main
navigation to add credentials and see saved names and types. No account or login
is required. You can also manage credentials in
[Django admin](http://127.0.0.1:7777/admin/).
Enter a **Name**, select a **Provider** (**GitHub** or **GitLab**), then choose a
**Type** (**Token**, **SSH**, or **Secret (other)**) and paste the secret. SSH keys can
use OpenSSH or PEM format; supply the passphrase if the key is encrypted. To
associate it with a repository, select it in the optional **Credential** field
when creating or editing a project on the Projects page. You can also assign it
in admin.
Multiple projects can share a credential. A credential cannot be deleted while
projects or API settings reference it.

Saved secrets are never filled back into forms. Use admin to edit credentials;
leave the secret blank to keep it or paste a new value to replace it. Replacing an SSH key also
requires its passphrase again, if encrypted. The credential type cannot be changed
after creation. Project forms show saved credential names only, and existing APIs
do not expose credentials.

Back up the encryption key separately from PostgreSQL and keep it out of Git.
Losing or replacing it makes saved credentials unreadable. Database exports
contain ciphertext, but names, providers, and credential types remain readable. Secrets and
SSH passphrases use [Fernet authenticated encryption](https://cryptography.io/en/stable/fernet/).
Use HTTPS when submitting credentials over a network.

The scheduled issue importer uses the GitHub token selected in Settings. The project
credential remains a separate repository association. Runners do not retrieve
these credentials through the API; there is no credential-decryption HTTP endpoint.
Trusted server code can explicitly call `project.credential.get_secret()` to obtain a dictionary
with `secret` and `passphrase`. Do not log or serialize that result into queue
responses.

Existing credentials are migrated to the GitHub provider: GitHub tokens become
Token credentials and SSH private keys become SSH credentials. The migration
preserves encrypted values and project associations without needing the
encryption key. GitLab is available for credential storage; project repository
URLs still support GitHub only.

## Move existing SQLite data

Stop n8n executions and app writes first. Keep a backup of `db.sqlite3`.
After setting up `.env`, export the current schema's data and import into an empty PostgreSQL database:

```sh
docker compose stop nightly-queue celery-worker celery-beat
mkdir -p .cache
docker compose run --rm --no-deps -T nightly-queue python manage.py dumpdata \
  --settings nightly_queue.sqlite_settings --natural-foreign --natural-primary \
  --exclude contenttypes --exclude auth.permission --exclude sessions > .cache/sqlite-export.json
./run.sh --build
docker compose exec nightly-queue python manage.py loaddata .cache/sqlite-export.json
```

The export opens SQLite read-only. Verify projects and queue items before resuming n8n.

## Use the CLI

The CLI needs Python 3.10+ and no Django installation:

```sh
python -m pip install .
export NQ_URL='http://127.0.0.1:7777'
export NQ_API_TOKEN='the-same-token-as-the-server'
nq projects
nq add --project 12 "Add CSV export" \
  --instructions "Include active filters." --priority normal
```

Use an existing project ID from `nq projects`. Successful submissions print the
item ID; errors exit nonzero. `NQ_URL` defaults to `http://127.0.0.1:8000`.
Use `python -m nq` to run without installing. Submissions are never retried automatically.

## Connect n8n

Celery Beat owns scheduled issue discovery. n8n owns work execution schedules,
cloning, and agent execution. Configure its GitHub authentication separately;
this repo does not install a workflow. The Celery greeting task remains a demo.
Beat stores its schedule in `/tmp/celerybeat-schedule` inside its container;
recreating the container resets Beat's minute check timer. The import interval and
last scheduled attempt remain in the database.
Run one Beat instance. After updating an existing installation, apply migrations
and restart the worker and Beat so they load the new task and schedule:

```sh
docker compose exec nightly-queue python manage.py migrate
docker compose up -d --force-recreate celery-worker celery-beat
```

The worker receives `NQ_CREDENTIAL_KEY` through Compose and must use the same key
as the web app. Beat runs with `DJANGO_DEBUG=False` so it sends tasks to the worker
instead of executing them eagerly without the credential key.
No API token is passed as a Celery task argument or returned in
its result.

All API paths below start with `/api/v1/` and require the bearer token.
POST bodies must be JSON with `Content-Type: application/json`.

| Method and path | Body | Response |
| --- | --- | --- |
| `GET /projects/` | — | `{"projects": [...]}` |
| `POST /items/` | `{"project_id": 12, "title": "Add export", "instructions": "CSV", "priority": "normal"}` | HTTP 201, `{"item": {...}}`; instructions and priority are optional |
| `POST /items/claim/` | `{"execution_ref": "n8n-execution-id"}` or `{}` | `{"item": {...}}` or `{"item": null}` |
| `POST /items/123/finish/` | `{"claim_token": "<UUID from claim>", "status": "succeeded", "result_url": "https://github.com/owner/repo/pull/1"}` | Updated item |

Claims select the oldest eligible item across manual work and imported issues.
No queue selection is needed. Item responses include `source` (`manual` or
`github`), `issue_number`, `issue_repository`, and `issue_state` (empty/null for
manual work). Both sources use the same claim tokens, finish endpoint, and
recovery rules. Closed issues and imports from projects with Autonomous disabled
remain visible but cannot be claimed. Existing `/auto/` bookmarks redirect to the
main queue. The merge migration preserves item IDs, issue metadata, and execution
history.

Build a workflow that:

1. Runs on your nightly schedule.
2. Claims an item with the n8n execution ID. Stops when `item` is `null`.
3. Checks out the returned **top-level** `repository_url` and `base_branch`, then
   creates or reuses `branch_name` (`nq/<item-id>`). A blank base branch means the
   remote default. These checkout settings stay fixed across retries; the nested
   project reflects current settings.
4. Runs the agent with the title and instructions. Pass inputs as data, without
   interpolating them into shell commands.
5. Finishes with the returned `claim_token` and status `succeeded` or `failed`.
   Optionally include an HTTP(S) `result_url` or an `error_summary`.
6. Repeats while the execution window remains open.

Claims are atomic. Only the current token can finish a running item. Invalid
input returns 400, wrong tokens 401, missing items 404, wrong methods 405, and
stale claims or repeated completion 409.

Interrupted items stay running. Stop the old n8n execution and agent, then select
**Requeue failed/interrupted work** in admin. This clears the old token and
attempt results while preserving checkout settings and the branch. There is no
automatic recovery. If a claim or finish request times out, inspect the item in
admin before retrying.

## Development

- `projects/`: project models, validation, admin, and API.
- `credentials/`: encrypted repository credentials and admin forms.
- `work_queue/`: queue models, services, UI, API, and recovery.
- `nightly_queue/`: Django/Celery configuration and shared API helpers.
- `nq/`: standalone standard-library CLI.

Add migrations and tests to the app that owns the model. Implement each new
feature in its own branch.

UI previews with sample data: [Queue](docs/screenshots/queue-desktop.png),
[Settings](docs/screenshots/settings-desktop.png),
[mobile queue](docs/screenshots/queue-mobile.png), and
[mobile Settings](docs/screenshots/settings-mobile.png).

The app uses Python 3.12. Run checks inside the app container with
`docker compose exec nightly-queue <command>`. For a native Python setup, install
`requirements.txt` and set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_HOST`, and `POSTGRES_PORT` for a reachable PostgreSQL server.

```sh
python manage.py test
python -m unittest nq.test_cli  # CLI tests without Django
python manage.py makemigrations --check --dry-run
python manage.py check
black --check .
ruff check .
mypy . --config-file mypy.ini
```

`bash test.sh` runs the Docker-based checks. CI runs the same script for non-draft
pull requests; see [the workflow](.github/workflows/test.yml).
