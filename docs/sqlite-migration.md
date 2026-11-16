# Move existing SQLite data

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
