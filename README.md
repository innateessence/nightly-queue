# nightly-queue

Capture project-linked work in Django for a future nightly n8n workflow.

## Run locally

Use Python 3.12 for the Django application:

```sh
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Create projects at http://127.0.0.1:8000/admin/hello/project/. Each project has:

- A numeric ID, displayed in admin and used by `nq`.
- A name and an HTTPS GitHub repository URL, normalized to
  `https://github.com/owner/repo.git`. Credentials, query strings, and links to
  repository subpages are rejected.
- An optional base branch. A blank value tells n8n to use the repository's default
  branch; it does not assume `main`.

Open http://127.0.0.1:8000/ to select a project and add a title, optional
instructions, and priority. Search by title/instructions and filter by project or
priority. Items remain oldest first regardless of priority. Projects referenced by any items cannot be deleted.

The browser queue remains a shared, unauthenticated local interface with CSRF
protection. Use it locally with non-sensitive data. n8n execution is not connected.

Alternatively, run `./run.sh --build` to use Docker Compose.
SQLite lives at
`/app/db.sqlite3` in the source bind mount, so data persists in the host checkout
when containers are recreated. Back up that file before schema changes. The app
supports `NQ_DB_PATH` to select a different database file; its parent directory
must already exist and be writable.

## Upgrade an existing queue

If existing items have no project, migration `0003` stops with instructions rather
than guessing their repositories. Back up SQLite and stop submissions/workflows
while performing the upgrade:

```sh
python manage.py migrate hello 0002
python manage.py shell
```

In the Django shell, create the projects those items actually belong to:

```python
from hello.models import Project, QueueItem

project = Project.objects.create(
    name="My project",
    repository_url="https://github.com/owner/repo",
    base_branch="",  # or an explicit branch such as "develop"
)
print(project.pk)
print(list(QueueItem.objects.filter(project__isnull=True).values("id", "title")))
```

Exit the shell, then assign specific item IDs. Repeat for each project:

```sh
python manage.py backfill_projects --project 1 --items 3 4 8
python manage.py migrate
```

Use `--all` instead of `--items ...` only when every remaining unassigned item
belongs to that project. The command leaves already assigned items alone. Fresh
databases and empty queues can run `python manage.py migrate` directly.
For Docker, prefix these commands with `docker compose run --rm nightly-queue`
while the app is stopped; the explicit command overrides its startup migration.

## nq CLI

Install the standalone HTTP client with Python 3.10+:

```sh
python -m pip install .
export NQ_URL='http://127.0.0.1:8000'
export NQ_API_TOKEN='the-same-token-as-the-server'
nq projects
nq add --project 12 "Add CSV export" \
  --instructions "Include active filters in exported results." \
  --priority normal
```

Projects can now be managed in Django admin. The CLI endpoints arrive in the
submission API branch; this stage still submits work through the browser.

`nq projects` calls `GET /api/v1/projects/` and lists existing project IDs.
`nq add` calls `POST /api/v1/items/` with a required numeric `project_id`, title,
optional instructions, and priority (`low`, `normal`, or `high`). It prints the
created item ID. The expected responses are `{"projects": [...]}` and
`{"item": {"id": 42, "project": {"id": 12}, "title": "Add CSV export"}}`.

`NQ_URL` defaults to `http://127.0.0.1:8000`. The client sends a bearer token from
`NQ_API_TOKEN`, uses a 30-second timeout, and exits nonzero on validation,
authentication, or connection failures. It does not follow redirects or retry
submissions automatically. Keep credentials out of URLs and use HTTPS remotely.
The CLI needs neither Django nor direct database access.

Run the client without installing it with `python -m nq`. Run its tests with
`python -m unittest hello.test_cli`.

## Tests

```sh
python manage.py test
python manage.py makemigrations --check --dry-run
python manage.py check
black --check .
ruff check .
mypy . --config-file mypy.ini
```

Use `bash test.sh` for the Docker-based lint and test suite. Migration tests cover
explicit backfill and the required project relationship.

## Continuous integration

The `Tests` GitHub Actions workflow runs `bash test.sh` when a pull request is
opened as ready for review or changed from draft to ready. Drafts are skipped.
Commits pushed afterward do not trigger another run. To request another run after
further changes, mark the PR as draft and then ready again.

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
