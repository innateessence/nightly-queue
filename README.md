# nightly-queue

Capture project-linked work in Django, then let n8n claim and execute it at night.
Each queue item references an existing `Project`, which identifies a GitHub
repository and an optional base branch.

## Application structure

- `projects/` owns project configuration, GitHub URL/branch validation, admin, and
  the project-list API.
- `work_queue/` owns queue items, submission UI, queue services/API, execution
  tracking, recovery, templates, and static assets. It depends
  on `projects`; `projects` does not depend on `work_queue`.
- `nightly_queue/` contains Django/Celery configuration and shared HTTP API helpers.
- `nq/` is the independent, standard-library CLI.

Tests live alongside their owning app in `test_*.py` (and `work_queue/tests.py`).
Add new schema migrations to the app that owns the model.

The apps use fresh initial migrations and the `projects_project` and
`work_queue_queueitem` tables. Only fresh database builds are supported.

## Run locally

Use Python 3.12 for the Django application:

```sh
python -m pip install -r requirements.txt
export NQ_API_TOKEN='your-random-api-token'
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Create projects at http://127.0.0.1:8000/admin/projects/project/. Each project has:

- A numeric ID, displayed in admin and used by `nq`.
- A name and an HTTPS GitHub repository URL, normalized to
  `https://github.com/owner/repo.git`. Credentials, query strings, and links to
  repository subpages are rejected.
- An optional base branch. A blank value tells n8n to use the repository's default
  branch; it does not assume `main`.

Open http://127.0.0.1:8000/ to select a project and add a title, optional
instructions, and priority. Search by title/instructions and filter by project or
priority. Items remain oldest first regardless of priority. The page shows execution
status and results; its queue count includes only queued items. Projects referenced
by any items cannot be deleted, and running items cannot be removed from the queue.

The browser queue remains a shared, unauthenticated local interface with CSRF
protection. The machine API requires `Authorization: Bearer <NQ_API_TOKEN>`;
browser sessions do not grant API access. An unset token disables the API with
HTTP 503. Keep this prototype local/private, and use HTTPS when accessing the API
over a network.

Alternatively, export the token and run `./run.sh --build` to use Docker Compose.
Compose also reads `NQ_API_TOKEN` from an untracked `.env` file. SQLite lives at
`/app/db.sqlite3` in the source bind mount, so data persists in the host checkout
when containers are recreated. Back up that file before schema changes. The app
supports `NQ_DB_PATH` to select a different database file; its parent directory
must already exist and be writable.

## Install and use nq

The CLI uses the Python standard library and needs no Django installation or direct
database access. Install it from this checkout (Python 3.10+):

```sh
python -m pip install .
export NQ_URL='http://127.0.0.1:8000'
export NQ_API_TOKEN='the-same-token-as-the-server'
nq projects
nq add --project 12 "Add CSV export" \
  --instructions "Include active filters in exported results." \
  --priority normal
```

`NQ_URL` defaults to `http://127.0.0.1:8000`. `nq projects` lists existing IDs;
`nq add` requires one and never creates a project implicitly. Successful submissions
print the new item ID. Validation, authentication, and connection failures exit
nonzero. The client uses a 30-second timeout and does not follow redirects or
automatically retry submissions, which could otherwise add duplicate work. During
development, `python -m nq` runs the same CLI without installation.

## n8n API contract

All endpoints live under `/api/v1/`, require the bearer token, and return JSON.
POST requests require `Content-Type: application/json`. Unknown fields and invalid
values return HTTP 400 with an `errors` object. Wrong tokens return 401, and wrong
HTTP methods return 405.

| Method and path | Request | Result |
| --- | --- | --- |
| `GET /projects/` | — | `{"projects": [...]}` with ID, name, repository URL, and base branch |
| `POST /items/` | `{"project_id": 12, "title": "Add export", "instructions": "CSV", "priority": "normal"}` | HTTP 201 with `{"item": {...}}`; instructions and priority are optional |
| `POST /items/claim/` | `{"execution_ref": "n8n-execution-id"}` or `{}` | HTTP 200 with one reserved item, or `{"item": null}` when nothing is queued |
| `POST /items/123/finish/` | `{"claim_token": "<returned UUID>", "status": "succeeded", "result_url": "https://github.com/owner/repo/pull/1"}` | HTTP 200 with the updated item |

The claim response includes the item ID, nested project, title, instructions,
priority, status, timestamps, `claim_token`, `execution_ref`, `branch_name`, and
top-level `repository_url` and `base_branch`. Use the **top-level checkout fields**:
these are captured at the first claim and preserved on retries even if the project
is edited later. The nested project contains current project settings. A blank
captured base branch still means the repository's default branch at checkout time.
The stable feature branch is `nq/<item-id>` and is also reused on retries.

Configure an n8n workflow to:

1. Trigger on your chosen nightly schedule and timezone.
2. Claim an item, passing the n8n execution ID for traceability. Stop on `item: null`.
3. Clone the returned repository using credentials stored in n8n, check out the
   base branch (or discover the remote default), and create the returned feature
   branch. On retries, inspect/reuse any existing feature branch instead of blindly
   creating another. Pass repository URLs, branch names, and instructions as data
   to the execution tool rather than interpolating them into shell commands.
4. Run the agent with the title and instructions in that checkout.
5. Report `succeeded` with an optional HTTP(S) result URL, or `failed` with an
   optional `error_summary`, using the claim token returned in step 2.
6. Claim the next item while the nightly execution window remains open.

Claims atomically change `queued` to `running`; overlapping callers cannot claim
the same queued item. Completion requires both `running` status and the current
claim token. A stale token or a repeated completion returns 409; a missing item
returns 404. Neither operation retries or schedules agent work itself.

Interrupted work stays `running` until an operator recovers it. First stop the old
n8n execution and agent, then select the item in Django admin and choose
**Requeue failed/interrupted work**. This invalidates its old token and clears
attempt timestamps/results while preserving the checkout settings and branch.
The next claim receives a new token. There is no automatic lease expiry or separate
attempt history yet. If a claim or finish request times out after reaching Django,
inspect its status/execution reference in admin before retrying the operation.

n8n owns the nightly schedule, GitHub credentials, cloning, and agent execution.
No n8n workflow is installed by this repository. The existing Celery greeting is
only a demo and does not consume queue items.

## Tests

```sh
python manage.py test
python manage.py test projects work_queue  # Focus on Django apps
python -m unittest nq.test_cli            # Run CLI tests without Django
python manage.py makemigrations --check --dry-run
python manage.py check
black --check .
ruff check .
mypy . --config-file mypy.ini
```

Use `bash test.sh` for the Docker-based lint and test suite. Django creates a fresh
test database from the initial migrations. Model tests cover required project
relationships and deletion protection.
API and service tests cover authentication, validation, competing claims, completion, and
recovery; CLI tests cover requests, output, and failure exit codes.

## Continuous integration

The `Tests` GitHub Actions workflow runs `bash test.sh` when a pull request is
opened as ready for review, changed from draft to ready, or updated with new
commits while non-draft. Drafts are skipped, including commits pushed to them.

The job builds the Compose services, checks Django readiness, then runs formatting,
lint, type, migration, and application checks through the same script used locally.

CI combines `docker-compose.yml` with `docker-compose.ci.yml`. Application
containers use the runner's UID/GID so SQLite and Celery can write to the checkout.
Service logs appear in the workflow output on failure; containers and volumes are
removed afterward. Runs time out after 20 minutes, and newer runs cancel older runs
for the same pull request.

To reproduce the CI configuration on Linux:

```sh
export NQ_CI_UID="$(id -u)"
export NQ_CI_GID="$(id -g)"
export COMPOSE_PROJECT_NAME='nightly-queue-ci'
export COMPOSE_FILE='docker-compose.yml:docker-compose.ci.yml'
bash test.sh
docker compose down --volumes --remove-orphans
```
