# vibearr

For repeatable self-development setup and finite issue-to-PR acceptance, see
[the bootstrap guide](docs/self-development-bootstrap.md).

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

You'll need Docker with Compose. The web app uses port **7777** by default.

Copy the environment file and build the images:

```sh
cp .env.example .env
docker compose build
```

Generate a credential encryption key:

```sh
docker compose run --rm --no-deps vibearr python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set these values in `.env` before starting:

| Variable | Purpose |
| --- | --- |
| `VIBEARR_API_TOKEN` | A random token for the queue API and `vibearr` CLI |
| `VIBEARR_POSTGRES_PASSWORD` | The application database password |
| `VIBEARR_CREDENTIAL_KEY` | The generated key for encrypting saved credentials |

Start the app:

```sh
./run.sh --build
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
docker compose exec vibearr python manage.py createsuperuser
```

This is a single-user app for a private network. The browser UI has no login
requirement; admin access and bearer-authenticated APIs have separate credentials.
Keep the UI on your LAN or behind an authenticated proxy. Use HTTPS when sending
credentials over a network.

## Submit work from the terminal

`vibearr` is a small HTTP client with no Django dependency. It needs Python 3.10 or newer.

```sh
python -m pip install .
export VIBEARR_URL='http://127.0.0.1:7777'
export VIBEARR_API_TOKEN='your-queue-api-token'

vibearr projects
vibearr add --project 12 "Add CSV export" \
  --instructions "Include the active filters and column headings." \
  --priority normal
```

Use a project ID from `vibearr projects`. Successful submissions print the new item ID;
errors exit nonzero. You can also run it with `python -m vibearr` from the repository.

## How it fits together

Django owns the queue, settings, credentials, and run history. Celery and Beat
discover GitHub issues. The independent runner claims work over HTTP and owns
agent processes, task workspaces, validation, and Git delivery. PostgreSQL stores
application data; Valkey supports the background task services.

The runner has its own image and persistent state volume. It does not need direct
database access or a mount of the running application's source. Agents work in
task environments, with supervisor credentials and production data kept outside
their reach. GitHub issue text and repository content describe the task; they
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

Use Python **3.12** for the application. Implement each feature on its own branch
and include tests and migrations with the code that needs them. The
[development plan](docs/autonomous-ai-queue-plan.md) defines branch dependencies
and acceptance criteria for the autonomous workflow.

Install dependencies for native development:

```sh
python -m pip install -r requirements.txt
```

Configure a reachable PostgreSQL server with `VIBEARR_POSTGRES_DB`, `VIBEARR_POSTGRES_USER`,
`VIBEARR_POSTGRES_PASSWORD`, `VIBEARR_POSTGRES_HOST`, and `VIBEARR_POSTGRES_PORT`, then run:

```sh
python manage.py migrate
python manage.py runserver
```

Run checks locally, or prefix application commands with
`docker compose exec vibearr` to use the app container:

```sh
python manage.py test
python -m unittest nq.test_cli
python manage.py makemigrations --check --dry-run
python manage.py check
black --check .
ruff check .
mypy . --config-file mypy.ini
```

`bash test.sh` builds the Compose services and runs the Docker checks used by CI.
Native Codex sessions use a pinned CLI image and scoped per-task authentication.
See [native Codex setup and diagnostics](docs/native-codex.md) for agent profiles
and authentication. [Unattended queue execution](docs/native-queue.md) covers
dispatch, validation, publication, cancellation and recovery.

The independent runner has its own verification entry point:

```sh
python -m vibearr_runner.verify
python -m vibearr_runner.verify --boundary --executor-image sha256:<id> --database-image sha256:<id>
```

Boundary checks must pass on the intended execution host. Portable tests alone
do not establish that an agent environment works in deployment. The independent
[OCI executor](docs/isolated-executor.md) owns engine access, registered immutable
profiles, and automated host qualification; the supervisor uses its authenticated
control API. Runtime readiness is separate from native coding-agent readiness.

The [isolated validation profile](docs/isolated-validation.md) tests exact committed
trees in fresh containers and disposable databases. It keeps required commands
outside candidate control and builds dependency changes without touching the live
installation.

## Keeping your data

PostgreSQL data lives in the `postgres-data` volume. `docker compose down` keeps
it; adding `--volumes` removes it. Runner state lives in `runner-data` and must be
preserved to recover unfinished work.

Back up the database:

```sh
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > backup.sql
```

Keep `.env` and tokens out of Git. Back up `VIBEARR_CREDENTIAL_KEY` separately from the
database: losing the key makes saved credentials unreadable. Stop the runner
before backing up its state, and keep database and runner backups consistent.
See the [operations guide](docs/runner-operations.md) for recovery and retention,
or the [SQLite migration procedure](docs/sqlite-migration.md) for older installs.
