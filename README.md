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

The queue and project pages have no login requirement: keep them local/private.
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

n8n owns scheduling, GitHub credentials, cloning, and agent execution. This repo
does not install a workflow. The Celery greeting task is only a demo.
Beat stores its schedule in `/tmp/celerybeat-schedule` inside its container;
recreating the container resets this demo schedule.

All API paths below start with `/api/v1/` and require the bearer token.
POST bodies must be JSON with `Content-Type: application/json`.

| Method and path | Body | Response |
| --- | --- | --- |
| `GET /projects/` | — | `{"projects": [...]}` |
| `POST /items/` | `{"project_id": 12, "title": "Add export", "instructions": "CSV", "priority": "normal"}` | HTTP 201, `{"item": {...}}`; instructions and priority are optional |
| `POST /items/claim/` | `{"execution_ref": "n8n-execution-id"}` or `{}` | `{"item": {...}}` or `{"item": null}` |
| `POST /items/123/finish/` | `{"claim_token": "<UUID from claim>", "status": "succeeded", "result_url": "https://github.com/owner/repo/pull/1"}` | Updated item |

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
- `work_queue/`: queue models, services, UI, API, and recovery.
- `nightly_queue/`: Django/Celery configuration and shared API helpers.
- `nq/`: standalone standard-library CLI.

Add migrations and tests to the app that owns the model. Implement each new
feature in its own branch.

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
