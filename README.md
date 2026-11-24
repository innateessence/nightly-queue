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
3. **Scope the work.** The target runner performs intake, records risk and affected
   surfaces, and saves a specification with acceptance criteria.
4. **Build and review.** A builder implements the spec in an isolated workspace.
   Trusted checks run before separate code review and any required security, UX
   or UI reviews. Failures return to the builder within a shared repair budget.
5. **Deliver the change.** The supervisor verifies the checks and reviews, then
   publishes one pull request linked to the issue, with check and review evidence.
   A human reviews and merges it after the required GitHub checks pass.

This is the planned [Codex-first software factory](docs/software-factory-plan.md),
inspired by the ZazenCodes v2 workflow. Codex implements the full role pipeline
first; Claude Code and OpenCode follow the same contract. Intake, routed reviews
and round artifacts remain implementation work, and live execution stays disabled
until Docker qualification passes.

The release target is **four always-on containers**: app, PostgreSQL, runner and
one execution worker. The app handles issue-import scheduling; runner owns account
and network mediation; worker runs setup, coding, reviews, checks and disposable
test/browser processes sequentially. Celery/Beat, Valkey and the separate gateway,
proxy, account and test-database containers are being removed from the target.
This simplification is planned implementation work in
[S1–S4](docs/docker-only-runtime-plan.md#required-simplification-branches), not a
claim that the current Compose stack already has four containers.

Human merging is required through the MVP and factory rollout. Automatic merging
is deferred until sustained operation demonstrates maturity and a human explicitly
approves a separate rollout; agent review never grants merge permission.

Routine execution through PR delivery should finish without command or plan
approval prompts. Connecting a project sets
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
Issue → Intake → Spec → Build → Checks → Reviews → Decision → PR → Human merge
```

Each feature gets its own issue, branch, and PR. Dependencies keep later features
from starting before their prerequisites have merged. The first goal is to make
this loop reliable enough to build the rest of the app through the queue itself.
Automatic updates extend it from merged code to the running installation.

The [development plan](docs/autonomous-ai-queue-plan.md) lays out the implementation
milestones: the application rename, autonomous execution, an isolated runtime,
native agent integrations, issue selection, human-reviewed delivery, the factory role
pipeline, the operations UI, and self-updates. The
[factory branch sequence](docs/software-factory-plan.md#branch-sequence-and-acceptance)
defines F1–F8 and their acceptance gates.

The [MVP release contract](docs/vibearr-mvp.md) defines the first complete loop:
two dependent issues implemented and tested automatically, with a human merging
each PR and the second starting from the first verified merge. That is the
starting point for building vibearr
through its own queue.

For existing installations, follow the [vibearr upgrade guide](docs/vibearr-upgrade.md)
to preserve database, volume, and runner identities while adopting the new names.

## Run with Docker Compose

Docker Engine and Compose are the only runtime prerequisites. The web app uses
port **7777** by default. Every service, worker, test, browser and operational tool
runs in a container. The [Docker-only runtime plan](docs/docker-only-runtime-plan.md)
defines the complete worker stack and its qualification gates. Autonomous execution
stays disabled until that container boundary is implemented and verified.

The commands below describe the current Compose implementation. The compact
four-container deployment requires S1–S4; this documentation PR does not replace
Compose services or change an installation.

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

Use **Configuration → Coding Runners** to define named runners with their own
backends and credentials. Create reusable processing windows under
**Configuration → Schedules**, then assign one schedule to each coding runner.
Several runners can share a schedule. Assign each runner configuration to a provisioned
identity. See [runner configuration](docs/runner-settings.md) for setup;
existing identities continue using the preserved default configuration.

Use the Delete links on queue rows and configuration lists to remove work items,
projects, credentials, coding runners, and schedules. Confirmation pages explain
affected settings and retained history. See [UI deletion](docs/ui-deletion.md).

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

In the compact target, Django owns the queue, settings, credentials and run history.
One supervised import loop in the app discovers GitHub issues using existing
services and database leases. Production PostgreSQL stores application data. The
independent runner claims work over HTTP and owns private state, selected backend
accounts, scoped egress and Git/PR delivery. It calls one authenticated worker
directly; no gateway or Docker daemon access is needed.

The worker switches between setup, agent/reviewer and trusted validation phases,
with fresh source, credentials and local test databases for each phase. Browser
and test-service subprocesses run inside that worker only while needed. Images
contain pinned tools and released source; state stays in named volumes/bounded
tmpfs. Candidate code cannot access host paths, controller journals, publication
credentials or production data. GitHub issue text and repository content describe
the task; they cannot grant new permissions. The existing larger Compose topology
must be replaced and these boundaries requalified before release.

| Directory | Responsibility |
| --- | --- |
| `work_queue/` | Queue models, shared submission and claim services, UI, and API |
| `projects/` | Repository configuration |
| `credentials/` | Encrypted secrets and settings views |
| `runner_control/` | Runner APIs, configuration, run records, and operator views |
| `vibearr_runner/` | Independent supervisor, backends, workspaces, recovery, and delivery |
| `vibearr/` | Standalone command-line client |
| `vibearr_app/` | Django and current Celery configuration; S1 replaces background scheduling |

## Development

Use Python **3.12** in container images. Implement each feature on its own branch,
with tests and migrations where needed. The [development plan](docs/autonomous-ai-queue-plan.md)
and [Docker-only correction branches](docs/docker-only-runtime-plan.md) define
merge order, the runtime boundary and acceptance gates.

Run application checks in Docker:

```sh
bash test.sh
bash test.sh work_queue.test_api
docker compose --profile tools run --rm tools python manage.py makemigrations --check --dry-run
docker compose --profile tools run --rm tools python manage.py check
docker compose --profile tools run --rm tools black --check .
docker compose --profile tools run --rm tools ruff check .
docker compose --profile tools run --rm tools mypy . --config-file mypy.ini
```

The [Docker checks](docs/docker-operations.md#development-and-ci) use disposable
projects for Git acceptance, distribution builds, qualification, browser captures
and backup/restore. CI uses the same container images and profiles. Missing
container tooling does not permit host
Python, pip, Git task execution, browsers, services or package installation.

Native agent integrations use pinned CLI images and explicitly selected connections.
[Native Codex](docs/native-codex.md), [unattended execution](docs/native-queue.md),
[validation](docs/isolated-validation.md) describe the retained contracts.
[Automatic merge](docs/automatic-merge.md) is a deferred capability and must stay
disabled during this rollout. Their host-launcher implementation references
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
