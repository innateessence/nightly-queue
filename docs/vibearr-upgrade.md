# Names and upgrading to vibearr

The product is **vibearr**. The repository remains
[`innateessence/nightly-queue`](https://github.com/innateessence/nightly-queue);
this release does not rename it, rewrite issue identities, or create a new queue.

## Naming map

| Area | Canonical name | Compatibility |
| --- | --- | --- |
| CLI distribution | `vibearr-cli` | Replaces the local `nightly-queue-cli` distribution |
| CLI command/package | `vibearr`, `python -m vibearr` | `nq`, `python -m nq`, and `nq.cli` remain usable |
| Django configuration | `vibearr_app` | `nightly_queue` modules resolve to the same canonical modules |
| Runner distribution | `vibearr-runner` | Install from `vibearr_runner/` or the old `coding_runner/` source path |
| Runner command/package | `vibearr-runner`, `python -m vibearr_runner` | `coding-runner`, `python -m coding_runner`, and old module imports remain usable |
| App Compose service/image | `vibearr`, `vibearr-app` | Old `nightly-queue` DNS remains on the control network |
| Runner Compose service/image | `vibearr-runner` | Profile remains `runner`; old Python entry points remain usable |
| Ollama image | `vibearr-ollama` | Existing model volume is preserved |
| Environment | `VIBEARR_*` | Existing `NQ_*`, `POSTGRES_*`, `DJANGO_*`, and `CELERY_*` input names remain usable |
| Fresh DB/user | `vibearr` | Existing unspecified DB/user stay `nightly_queue` |
| Fresh Compose/data project | `vibearr` | Existing unspecified project/data prefix stay `nightly-queue` |
| Fresh runner state path | `/var/lib/vibearr` | Existing unspecified state path stays `/var/lib/coding-runner` |

The fresh-install names are selected explicitly in `.env.example`. An existing
minimal `.env` containing just `POSTGRES_PASSWORD`, `NQ_API_TOKEN`,
`NQ_CREDENTIAL_KEY`, and `NQ_RUNNER_API_TOKEN` continues selecting the old DB/user,
volume names, encryption key, and runner state path even if the checkout directory
is renamed. App labels, model tables, applied migrations, stored issue URLs, branch
identities, and runner journal format are unchanged.

All old Python import shims refer to the same module objects as the canonical
imports. They do not duplicate model, exception, or controller classes. Existing
migration imports, pickled references, and patch paths retain their identity.
Tests remain under the legacy package paths to run the suite once.

For a native Python installation, remove old distribution metadata before installing
the new distributions, since the compatibility packages share the old import names:

```sh
python -m pip uninstall nightly-queue-cli nightly-queue-runner
python -m pip install .
python -m pip install ./vibearr_runner
```

Run these inside the installation's virtual environment, after stopping the runner.
Docker images are rebuilt from source and need no package uninstall step.

## Environment transition

Replace `NQ_` with `VIBEARR_` for API URL/token, encryption key, runner token, state
directory, source/image revision, sandbox policy digest, and SQLite export path.
Prefix existing `POSTGRES_*`, `DJANGO_*`, and `CELERY_*` application inputs with
`VIBEARR_` to use their canonical forms. For example:

| Old | New |
| --- | --- |
| `NQ_API_TOKEN` | `VIBEARR_API_TOKEN` |
| `NQ_URL` | `VIBEARR_URL` |
| `NQ_CREDENTIAL_KEY` | `VIBEARR_CREDENTIAL_KEY` |
| `NQ_RUNNER_API_TOKEN` | `VIBEARR_RUNNER_API_TOKEN` |
| `POSTGRES_PASSWORD` | `VIBEARR_POSTGRES_PASSWORD` |
| `POSTGRES_DB` | `VIBEARR_POSTGRES_DB` |
| `POSTGRES_USER` | `VIBEARR_POSTGRES_USER` |

An explicitly set canonical value wins, including an empty value. A conflicting
legacy value produces a warning naming only the two variables; neither value is
printed. Matching aliases are quiet. Remove old names after verifying the new
configuration. Do not rotate keys or tokens merely to rename a variable.
PostgreSQL's own container still receives its standard `POSTGRES_*` variables.

Compatibility names will be retained throughout the first vibearr MVP release
series. Removing them requires a separately documented migration; it is not part
of this rename.

## Upgrade an existing installation

1. Pause work and stop the old supervisor and recorded children. Save a consistent
   database backup, the credential encryption key, and the stopped runner volume.
   Keep the old checkout, image tags/digests, and `.env` for rollback.
2. Record `docker compose config --volumes` and the actual volume names from
   `docker volume ls`. Keep your existing `.env`; **do not overwrite it with the
   fresh-install example**. Keeping old variable names is supported.
3. Preserve the old Compose project prefix. The standard installation uses
   `nightly-queue`. For a custom old project set `COMPOSE_PROJECT_NAME` to its
   existing value. If changing the service project to `vibearr`, also set
   `VIBEARR_DATA_PROJECT` to the old prefix explicitly. Named volumes are selected
   from that data prefix, independently of the checkout directory.
4. Preserve the old DB/user. If they were unspecified, leave them unspecified or
   set `VIBEARR_POSTGRES_DB=nightly_queue` and
   `VIBEARR_POSTGRES_USER=nightly_queue`. Renaming a database variable does not
   rename an existing PostgreSQL database or role.
5. Preserve `VIBEARR_RUNNER_STATE_DIR=/var/lib/coding-runner` for an old runner
   volume. An omitted state directory already selects this compatibility path.
   Do not relocate active state or copy it to start another controller. Saved
   absolute workspace paths and controller proof must continue to match.
6. Remove old service containers with the old Compose definition using
   `docker compose down` **without `--volumes`**. Start the new definition with
   `./run.sh --build`. Use `docker compose exec vibearr ...` for application
   commands and `docker compose --profile runner ... vibearr-runner` for runner
   operations. Service names changed; saved scripts using old service names must
   be updated.
7. Check projects, queue history, encrypted credential access, runner identity,
   and preserved workspace paths before enabling processing. New runtime code
   invalidates old code-bound release evidence; run the current deployment
   diagnostics/qualification instead of copying an old readiness report.

The rename does not automatically copy or rewrite database or runner data. A
future explicit path/database relocation must stop work, back up, migrate all
references consistently, and verify recovery before switching paths. Keeping
compatibility storage identities is the safe supported upgrade path.

## Backup, restore, and rollback

Database backups still use `pg_dump` inside the PostgreSQL service. Back up the
selected `postgres-data` and `runner-data` volume identities, not whichever names
happen to match a new checkout directory. Back up the credential encryption key
separately. See [runner operations](runner-operations.md) for consistent snapshots.

To roll back, pause and stop the new supervisor/children, stop the new services
without removing volumes, and restore the old checkout/images and saved `.env`.
Keep the same data project, DB/user, encryption key, and old runner state path.
The rename adds no database migration or journal schema change. If subsequent
features have changed schemas, use their documented compatible rollback or the
consistent backups; a naming rollback is not a schema downgrade.

The GitHub repository rename is a separate operation. When authorized, update
canonical repository links and bootstrap defaults while retaining repository IDs
and reconciling redirects before importing issues. No remote repository, issue,
branch, or PR identity changes in this feature.

## Rendered UI

[The projects page](screenshots/vibearr-rename-projects.png) was captured from a
real local instance with a disposable empty database. It contains no production
credentials or live project data.
