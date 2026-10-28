# nightly-queue

A small Django prototype for capturing work to be executed later by n8n.

## Run locally

```sh
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ to add work items with a title, optional instructions,
and priority. Items are saved in SQLite, displayed oldest first, and can be removed.
Search by title or instructions, and filter by priority to find queued work.
Priority is metadata only; it does not change the display order. The queue is shared
and has no authentication yet, so use it locally with non-sensitive data.

n8n is not connected yet: adding an item does not execute it or schedule a run.

Alternatively, run `./run.sh --build` to use Docker. The current Docker setup keeps
SQLite inside the app container; recreating that container loses its saved queue.

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

This branch adds the client only. The Django server does not expose its endpoints
yet; the project model and submission API follow in separate branches.

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
```

Use `bash test.sh` for the Docker-based lint and test suite.

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
