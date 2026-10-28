# nightly-queue

Queue work for your GitHub projects. Django stores it; n8n claims items and runs
an agent on a nightly schedule.

## Run locally

Use Python 3.12:

```sh
python -m pip install -r requirements.txt
export NQ_API_TOKEN='your-random-api-token'
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

1. Create a project in [Django admin](http://127.0.0.1:8000/admin/projects/project/)
   with an HTTPS GitHub repository URL and optional base branch. Leave the branch
   blank to use the repository default.
2. Open [the queue](http://127.0.0.1:8000/) and add work with a title, optional
   instructions, and priority.
3. Use n8n to process items oldest first. Priority does not change their order.

The queue page has no login requirement: keep it local/private. API access requires
`Authorization: Bearer <NQ_API_TOKEN>`; an unset token disables the API. Use HTTPS
for API access over a network.

For Docker, set the token in your shell or an untracked `.env`, then run
`./run.sh --build`. SQLite persists at `db.sqlite3` in your checkout. Back it up
before schema changes. Set `NQ_DB_PATH` to use another file in an existing,
writable directory. Only fresh database builds are supported by the current migrations.

## Use the CLI

The CLI needs Python 3.10+ and no Django installation:

```sh
python -m pip install .
export NQ_URL='http://127.0.0.1:8000'
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
