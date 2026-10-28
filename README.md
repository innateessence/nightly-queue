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

## Tests

```sh
python manage.py test
```

Use `bash test.sh` for the Docker-based lint and test suite.

## Continuous integration

The `Tests` GitHub Actions workflow runs `bash test.sh` on pull requests and pushes
to `main`. It also supports manual runs. The job builds the
Compose services, checks Django readiness, then runs formatting, lint, type,
migration, and application checks through the same script used locally.

CI combines `docker-compose.yml` with `docker-compose.ci.yml`. Application
containers use the runner's UID/GID so SQLite and Celery can write to the checkout.
Service logs appear in the workflow output on failure; containers and volumes are
removed afterward. Runs time out after 20 minutes, and newer runs cancel older runs
for the same pull request or branch.

To reproduce the CI configuration on Linux:

```sh
export NQ_CI_UID="$(id -u)"
export NQ_CI_GID="$(id -g)"
export COMPOSE_PROJECT_NAME='nightly-queue-ci'
export COMPOSE_FILE='docker-compose.yml:docker-compose.ci.yml'
bash test.sh
docker compose down --volumes --remove-orphans
```

The workflow reports failures from the checked-out branch. Existing formatting,
invalid-form, and escaping-test defects on `main` are addressed by the separate
project/UI PR.
