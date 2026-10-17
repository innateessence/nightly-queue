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
