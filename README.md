# nightly-queue

Queue work for your GitHub projects. Django stores tasks and imports issues.
A dedicated `coding-runner` Docker Compose service is planned to execute queued
work with Codex, Claude Code, or OpenCode and deliver pull requests.
It will read the full GitHub issue discussion, research a solution, and save an
implementation plan before handing the task to the code runner. Runner behavior
and credential selections will be configured in the web UI.
See the [coding runner architecture and implementation plan](docs/coding-runner.md).

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
3. Items are ready for a runner to claim through the API, oldest first. Priority
   does not change their order. Built-in task execution is planned; the current
   stack stores work and imports issues but does not execute coding tasks.

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
requests is part of the planned `coding-runner` service.

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

Stop any task runners and app writes first. Keep a backup of `db.sqlite3`.
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

The export opens SQLite read-only. Verify projects and queue items before resuming task runners.

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

## Coding runner architecture

The planned `coding-runner` service will:

1. Claim a task and read the current GitHub issue and all comments, including every
   page of the discussion. Manual tasks use their title and instructions.
2. Research the issue against the repository and relevant sources, then save an
   implementation plan with acceptance criteria and validation steps.
3. Hand the plan and source context to the code runner, which creates or reuses the
   task branch and implements the solution.
4. Test the branch, inspect the diff, and record validation results.
5. After required checks pass, push the branch and create or reuse a PR linking the
   original issue, then report the actual PR URL to the queue.

Django owns queue state, saved plans, run visibility, and all runner settings in
the web UI. Settings will include processing days/time ranges and timezone, a
global minimum remaining-usage percentage, execution limits, research/planning
and implementation backends/models, credential selections, and retention. Project
pages will provide overrides plus setup and validation commands. Celery
Beat and its worker continue to own scheduled issue discovery. Issue sync and
coding execution have separate schedules. The Celery greeting task remains a demo.

Processing will be gated by both the allowed schedule and the global usage
reserve. For example, **40% minimum remaining usage** allows queue work only while
more than 40% of each participating backend account's allowance remains. At 40% or
less, processing stops at a safe checkpoint across all projects, with no new
claims or model work. The UI will show remaining usage and the blocking reason;
missing or unsupported usage readings block processing when enforcement is enabled.
An in-flight call may cross the threshold before it can pause. Processing resumes
when usage is above the reserve and the schedule permits it. See the
[schedule and usage policy](docs/coding-runner.md#processing-schedule-and-global-usage-reserve)
for account/window handling and recovery.

The runner will use the existing HTTP API at `http://nightly-queue:8000` on the
Compose network, with its own workspace and secret configuration. It will not
connect directly to PostgreSQL or mount the live application's source directory.
Runner credentials will be managed through the web UI and selected separately
from the issue-import token. The existing API does not expose stored project
credentials; scoped runner credential delivery must be implemented. Queue
connectivity/authentication, encryption keys, and container/volume wiring remain
deployment configuration. Commands arriving as untrusted issue or research data
require user confirmation before execution, surfaced through the web UI.

The service, comment retrieval, research/planning handoff, runner settings UI,
credential delivery, and recovery behavior are not implemented yet. The current
Settings page only configures API credentials and the issue-import interval.
See [the implementation plan](docs/coding-runner.md) for scope and acceptance criteria.

## Issue discovery operations

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

## Runner API

All API paths below start with `/api/v1/` and require the bearer token.
POST bodies must be JSON with `Content-Type: application/json`.

| Method and path | Body | Response |
| --- | --- | --- |
| `GET /projects/` | — | `{"projects": [...]}` |
| `POST /items/` | `{"project_id": 12, "title": "Add export", "instructions": "CSV", "priority": "normal"}` | HTTP 201, `{"item": {...}}`; instructions and priority are optional |
| `POST /items/claim/` | `{"execution_ref": "runner:<run-id>"}` or `{}` | `{"item": {...}}` or `{"item": null}` |
| `POST /items/123/finish/` | `{"claim_token": "<UUID from claim>", "status": "succeeded", "result_url": "https://github.com/owner/repo/pull/1"}` | Updated item |

Claims select the oldest eligible item across manual work and imported issues.
No queue selection is needed. Item responses include `source` (`manual` or
`github`), `issue_number`, `issue_repository`, and `issue_state` (empty/null for
manual work). Both sources use the same claim tokens, finish endpoint, and
recovery rules. Closed issues and imports from projects with Autonomous disabled
remain visible but cannot be claimed. Existing `/auto/` bookmarks redirect to the
main queue. The merge migration preserves item IDs, issue metadata, and execution
history.

The planned runner will:

1. Run only when enabled, within the configured processing schedule, and above the
   global remaining-usage reserve when that control is enabled.
2. Claim an item with a unique runner execution ID. Wait for the polling interval
   when `item` is `null`.
3. Prepare the workspace using the returned **top-level** `repository_url` and
   `base_branch`, and retain `branch_name` (`nq/<item-id>`) for implementation.
   A blank base branch means the remote default. These checkout settings stay
   fixed across retries; the nested project reflects current settings.
4. Fetch the full issue discussion for GitHub tasks, research the problem, and save
   an implementation plan. Pass inputs as data, without interpolating them into
   shell commands. Claim responses do not contain issue comments or a plan.
5. Hand the plan to the code runner to create or reuse the task branch and implement
   the solution. Validate the branch; after required checks pass, commit, push, and
   create or reuse a pull request linking the original issue when applicable.
6. Finish with the returned `claim_token` and status `succeeded` plus the actual PR
   URL in `result_url`, or `failed` with an `error_summary`.
7. Repeat while the schedule, global usage reserve, and configured task limit allow
   it. Recheck gates before new stages or model work and preserve paused progress.

Claims are atomic. Only the current token can finish a running item. Invalid
input returns 400, wrong tokens 401, missing items 404, wrong methods 405, and
stale claims or repeated completion 409.

Interrupted items currently stay running. Stop the old runner execution and agent, then select
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
