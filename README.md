# vibearr

For Docker setup, qualification, backup and recovery, see
[Docker operations](docs/docker-operations.md). D4 CI and final release acceptance
are still in progress; keep autonomous execution disabled.

A self-hosted *arr app for vibe coding with Codex, Claude Code, and OpenCode.

The idea is simple: put the work in GitHub issues and let a coding agent get on
with it. Describe a feature, a bug, or a chore. vibearr handles when it runs,
which agent does it, and where the result goes.

Think Sonarr or Radarr for your development backlog. Repositories are the
projects, issues are the work, and coding agents are the workers. Open the web
app to add something, see what's running, or find out why a task failed. Leave
it alone when everything is working.

## The workflow

1. **Connect a repository.** Choose a base branch, an agent, and the commands that
   must pass before a change can be delivered.
2. **Add work.** Write a GitHub issue or submit a task from the queue. Give it a
   clear outcome and any acceptance criteria the agent will need.
3. **Let the queue run.** The worker picks up eligible work during its processing
   window, prepares an isolated checkout, and gives the agent the issue context.
4. **Check the result.** The agent implements the change and runs validation.
   Failed checks get a bounded repair attempt; unresolved failures stay visible.
5. **Deliver the change.** Passing work becomes a pull request linked to the
   original issue. Projects configured for automatic merge can finish the job
   once the required GitHub checks pass.

Routine work should finish without approval prompts. Connecting a project sets
the boundaries for its agents: which issues they can work on, what they can
access, how long they can run, and how changes are delivered. Setup, investigation,
editing, and tests happen inside those boundaries. Work that cannot proceed
gets a useful failure reason instead of waiting for someone to approve a command.

## Built for background work

The queue is the center of the app. It should be easy to tell what's ready,
what's running, and what's waiting on something else.

- **Projects:** repository settings, backend and model selection, setup commands,
  validation commands, and delivery policy.
- **Queue:** incoming issues and manual tasks, with priorities, holds, and
  dependencies so useful work runs first.
- **Activity:** the current stage, progress, logs, and controls to pause or cancel.
- **History:** previous attempts, validation results, failures, and links to the
  commits and pull requests they produced.
- **System:** connection health, schedules, backend limits, storage, and updates.

Use processing windows to run overnight or keep it working all day. Limit active
tasks, execution time, and retry attempts. Where a backend supplies reliable
allowance information, a minimum remaining-usage setting can reserve capacity
for your other work.

A restart should preserve a task's progress. A lost response should not create a
second agent session or a duplicate PR. The runner keeps durable execution state
and reconciles external operations before continuing.

## A queue that works on itself

vibearr's own GitHub issue tracker is its development backlog. The intended
loop is the same one used for any other project:

```text
Issue → Queue → Agent → Tests → Pull request → Merge
```

Each feature gets its own issue, branch, and PR. Dependencies keep later features
from starting before their prerequisites have merged. The first goal is to make
this loop reliable enough to build the rest of the app through the queue itself.
Automatic updates extend it from merged code to the running installation.

The [development plan](docs/autonomous-ai-queue-plan.md) lays out the implementation
milestones: the application rename, autonomous execution, an isolated runtime,
native agent integrations, issue selection, automatic merging, the operations UI,
and self-updates.

The [MVP release contract](docs/vibearr-mvp.md) defines the first complete loop:
two dependent issues implemented, tested, and merged automatically, with the second
starting from the first change. That is the starting point for building vibearr
through its own queue.

For existing installations, follow the [vibearr upgrade guide](docs/vibearr-upgrade.md)
to preserve database, volume, and runner identities while adopting the new names.

## Run with Docker Compose

Docker Engine and Compose are the only runtime prerequisites. The web app uses
port **7777** by default. Every service, worker, test, browser and operational tool
runs in a container. The [Docker-only runtime plan](docs/docker-only-runtime-plan.md)
defines the complete worker stack and its qualification gates. Autonomous execution
stays disabled until that container boundary is implemented and verified.

Copy the environment file and build the images:

```sh
cp .env.example .env
docker compose build
```

Fresh installations leave secret values empty: initialization generates them
once in private named volumes. Existing PostgreSQL installations must supply
their original values before initialization; missing credentials or conflicts
stop startup. Preserve the original database/user and volume settings on upgrade.

| Variable | Purpose |
| --- | --- |
| `VIBEARR_API_TOKEN` | Original queue API token on upgrade; generated for a fresh install |
| `VIBEARR_POSTGRES_PASSWORD` | Original database password on upgrade; generated for a fresh install |
| `VIBEARR_CREDENTIAL_KEY` | Original encryption key on upgrade; generated for a fresh install |
| `DJANGO_SECRET_KEY` | Original Django key on upgrade; generated for a fresh install |

Start the app:

```sh
docker compose up -d --wait
```

Open [localhost:7777](http://127.0.0.1:7777/), add a project with its HTTPS GitHub
repository URL, and save a GitHub token under Credentials. Select the issue-import
token in Settings and enable **Autonomous** on the project to discover its issues.
You can also add tasks directly from the queue.

The web app and issue importer start with the default Compose services. Execution
runs in a separate `vibearr-runner` service. Follow the [runner setup and operations
guide](docs/runner-operations.md) for its identity, credentials, and deployment
requirements. Starting the web app alone does not start a coding agent.

For Django admin access, create an account:

```sh
docker compose --profile tools run --rm tools python manage.py createsuperuser
```

This is a single-user app for a private network. The browser UI has no login
requirement; admin access and bearer-authenticated APIs have separate credentials.
Keep the UI on your LAN or behind an authenticated proxy. Use HTTPS when sending
credentials over a network.

## Submit work from the terminal

Run the HTTP CLI in the tools container, whose entrypoint loads the private queue
token. No host Python or CLI installation is required.

```sh
docker compose --profile tools run --rm -e VIBEARR_URL=http://vibearr:8000 tools python -m vibearr projects
docker compose --profile tools run --rm -e VIBEARR_URL=http://vibearr:8000 tools python -m vibearr add \
  --project 12 "Add CSV export" \
  --instructions "Include the active filters and column headings." --priority normal
```

Use a project ID from `projects`. Successful submissions print the item ID;
errors exit nonzero. The compatibility `nq` module is also available in the image.

## How it fits together

Django owns the queue, settings, credentials, and run history. Celery and Beat
discover GitHub issues. The independent runner claims work over HTTP and owns
agent processes, task workspaces, validation, and Git delivery. PostgreSQL stores
application data; Valkey supports the background task services.

The Docker-only target gives the runner its own image and state volume. It calls
authenticated preprovisioned worker slots, with no Docker daemon access. Released
images contain source; workspaces and artifacts stay in named volumes and bounded
tmpfs. Agents cannot access host paths, controller journals or production data. GitHub issue text and repository content describe the task; they
cannot grant new permissions.

| Directory | Responsibility |
| --- | --- |
| `work_queue/` | Queue models, shared submission and claim services, UI, and API |
| `projects/` | Repository configuration |
| `credentials/` | Encrypted secrets and settings views |
| `runner_control/` | Runner APIs, configuration, run records, and operator views |
| `vibearr_runner/` | Independent supervisor, backends, workspaces, recovery, and delivery |
| `vibearr/` | Standalone command-line client |
| `vibearr_app/` | Django and Celery configuration |

## Development

Use Python **3.12** in container images. Implement each feature on its own branch,
with tests and migrations where needed. The [development plan](docs/autonomous-ai-queue-plan.md)
and [Docker-only correction branches](docs/docker-only-runtime-plan.md) define
merge order, the runtime boundary and acceptance gates.

Run application checks in Docker:

```sh
docker compose exec vibearr python manage.py test
docker compose exec vibearr python -m unittest nq.test_cli
docker compose exec vibearr python manage.py makemigrations --check --dry-run
docker compose exec vibearr python manage.py check
docker compose exec vibearr black --check .
docker compose exec vibearr ruff check .
docker compose exec vibearr mypy . --config-file mypy.ini
```

The Docker-only operations branch supplies tools containers for Git acceptance,
distribution builds, qualification and browser screenshots. CI must use the same
container images and profiles. Missing container tooling does not permit host
Python, pip, Git task execution, browsers, services or package installation.

Native agent integrations use pinned CLI images and explicitly selected connections.
[Native Codex](docs/native-codex.md), [unattended execution](docs/native-queue.md),
[validation](docs/isolated-validation.md) and [automatic merge](docs/automatic-merge.md)
describe the retained contracts. Their host-launcher implementation references
cannot qualify Docker-only execution. D1–D4 must pass the actual worker boundary
and dependent two-issue scenario before enabling claims.

## Keeping your data

PostgreSQL data lives in the `postgres-data` volume. `docker compose down` keeps
it; adding `--volumes` removes it. Runner state lives in `runner-data` and must be
preserved to recover unfinished work.

The Docker-only backup job writes database and runner backups into a dedicated
backup volume and tests restoration in a disposable Compose project. Host export
is an explicit operator-selected Docker copy, not an application background write.

Keep `.env` and tokens out of Git. Back up `VIBEARR_CREDENTIAL_KEY` separately from the
database: losing the key makes saved credentials unreadable. Stop the runner
before backing up its state, and keep database and runner backups consistent.
See the [operations guide](docs/runner-operations.md) for recovery and retention,
or the [SQLite migration procedure](docs/sqlite-migration.md) for older installs.
The [MVP deployment guide](docs/mvp-deployment.md) covers persistent background
services, qualification, backups and release updates.
