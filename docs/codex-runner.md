# Codex coding runner

The optional `coding-runner` Compose profile processes one task at a time using
Codex CLI 0.169.0. It polls the queue, checks out the task branch, runs Codex,
validates changes, commits and pushes the branch, creates or reuses an open PR,
and reports success with its actual URL. It does not merge PRs. Claude Code and
OpenCode are deferred.

## Configure

Create an operator-reviewed repository policy:

```sh
mkdir -p .cache
cp coding_runner/projects.example.json .cache/runner-projects.json
```

Edit the file to list each allowed repository as a lowercase `owner/repository`
key. Each policy needs at least one validation command. Commands are arrays of
arguments, executed without a shell. Optional setup commands run before Codex.
For example:

```json
{
  "example/project": {
    "setup": [["python", "-m", "pip", "install", "-r", "requirements.txt"]],
    "validation": [["python", "-m", "unittest", "discover"]]
  }
}
```

Review repository setup and validation commands before allowing execution. The
base image includes Python 3.12, Node.js 22, Git, and Codex. Extend the runner image
for projects needing compilers, browsers, databases, or other build dependencies.
The application database and issue-import credentials are not passed to the runner.

Set these values in the untracked `.env`:

| Variable | Purpose |
| --- | --- |
| `NQ_API_TOKEN` | Existing queue API bearer token |
| `NQ_RUNNER_GITHUB_TOKEN` | GitHub personal token with repository contents read/write and pull-request read/write access |
| `OPENAI_API_KEY` | Codex API authentication; account-based login is not implemented in this version |
| `NQ_RUNNER_PROJECTS_FILE` | Policy path on the host; defaults to `./.cache/runner-projects.json` |
| `NQ_RUNNER_MODEL` | Optional model override; blank uses Codex's default |
| `NQ_RUNNER_TIMEOUT_SECONDS` | Command execution budget per task; default 3600 seconds |
| `NQ_RUNNER_POLL_SECONDS` | Empty-queue/window polling interval; default 60 seconds |
| `NQ_RUNNER_TIMEZONE` | Execution window timezone; default UTC |
| `NQ_RUNNER_WINDOW_START` | Optional local start, such as `22:00` |
| `NQ_RUNNER_WINDOW_END` | Optional local end, such as `06:00` |

Set both window boundaries or neither. Start is inclusive and end is exclusive;
overnight windows are supported. Blank boundaries allow continuous execution.
The window controls new claims; an existing task may finish after it closes.
GitHub and queue HTTP calls have separate 30-second timeouts. Per-window task
budgets and execution settings in the web UI are follow-up work.

## Build and check

Build the image and check the queue, GitHub authentication, and installed tools
without claiming work:

```sh
docker compose build coding-runner
docker compose run --rm --no-deps coding-runner --check
```

The check also verifies that the run reconciliation endpoint is deployed and that
Codex's sandbox can execute a harmless command, without claiming any tasks. It
does not make a model request, prove the OpenAI key works, or verify every
repository's write permission.
Ensure the app is running and contains the runner reconciliation endpoint before
starting execution. The runner uses `http://nightly-queue:8000` inside Compose.

## Execute

After a controlled test repository is configured, process at most one task:

```sh
docker compose run --rm --no-deps coding-runner --once
```

Start continuous polling explicitly:

```sh
docker compose --profile runner up -d --no-deps coding-runner
docker compose logs -f coding-runner
```

The runner profile is excluded from ordinary `docker compose up`. Source is copied
into its image; rebuild and recreate the runner after implementation changes.
Environment and policy configuration are read at startup. Recreate the container
after changing environment variables; restart it after changing policy contents.
No automatic restart policy is configured: an interrupted run stops for review.

## Sandbox and credentials

The container runs as UID 10001, drops Linux capabilities, and uses
`no-new-privileges`. Codex runs with `workspace-write`, no network access for its
shell tools, and approval policy `never`. Required approvals therefore block the
task instead of silently enabling broader access. A structured `blocked` result
is reported as failure with the private log retained.

Docker's default seccomp policy prevents the user namespaces needed by Codex's
bubblewrap sandbox. This service uses `seccomp:unconfined` so that sandbox can run;
it does not use privileged mode or mount the host Docker socket. A smoke test on
the development host verified workspace writes and rejection of a write outside
the workspace. Hosts must permit unprivileged user namespaces. Do not disable the
Codex sandbox to work around a host that cannot provide it.

Use this initial version only for repositories and policies you have reviewed.
Setup and validation run as the runner user outside Codex's sandbox. All stages
share a container and UID, so this is not a hardened boundary for hostile code.
Separate task containers and a credential proxy are follow-up hardening work.

The runner removes queue and GitHub credentials from setup, agent, and validation
environments. The OpenAI key is passed only to Codex as `CODEX_API_KEY`, excluded
from its shell-tool environment. GitHub authentication is supplied only to remote
Git and GitHub API operations and is not written to clone URLs. Known credential
values are redacted from command logs. Logs may still contain repository or task
content; keep the data volume private.

The integration follows the [Codex automation and authentication guide](https://developers.openai.com/codex/noninteractive).

## Logs and recovery

Open **Runner logs** on a queue item to view live execution stages and redacted
command output. The page checks for new output every three seconds while the
selected run is active. Previous attempts remain selectable after a retry.
Log text is displayed as plain text, including any HTML emitted by commands.
These pages have the same private-LAN visibility as the queue; they do not add a
login requirement.

The runner uploads batches about every two seconds through
`POST /api/v1/items/<id>/logs/`, using the current claim token and run UUID.
Sequence numbers make retries of the same chunk idempotent. Stale claims and
conflicting sequences are rejected. Each run can upload 256 chunks of at most
4096 characters. Larger logs stop uploading with a truncation notice; full output
remains in the private runner log. Known queue/GitHub/OpenAI/claim credentials are
redacted before transmission, including values spanning output chunks. The server
also removes its API/claim tokens and terminal control characters before storage.

Uploads run independently of coding commands and fail without failing the task.
During an outage, web logs may be incomplete; they are not automatically backfilled
from the private log. The bounded uploader drains briefly before completion
reporting. No shared filesystem mount is needed for web access.

Deploy the log schema with `python manage.py migrate` in the app container and
rebuild the runner image before starting a new execution. Existing private run
logs are not imported into the web app. Deleting a queue item also deletes its
uploaded logs.

`runner-data` stores `/data/active.json` and `/data/runs/<run-uuid>/`. Runs retain a
private command log, Codex completion result, workspace, home directory, and
archived state. The active journal includes the claim token and is written
atomically with mode 0600. One process holds a volume lock to prevent simultaneous
use of the same journal. Separate volumes can host independent workers through
the queue's atomic claims.

The runner never automatically replays an interrupted agent. On restart it looks
up the run's exact execution reference. A persisted completion outcome can be
confirmed as already applied, or reported to the same running claim. Other
interrupted phases and ambiguous claim/PR responses stop for operator review.
A changed claim token prevents reporting an old outcome.

To recover interrupted implementation work:

1. Stop the original container and agent. Inspect the private journal/log and any
   pushed branch or PR before deciding whether to retry.
2. Use **Requeue failed/interrupted work** in Django admin when retrying is appropriate.
3. Archive the stopped run's active journal:

   ```sh
   docker compose run --rm --no-deps coding-runner --discard-interrupted
   ```

   This checks the API and refuses to discard a run still marked running.
4. Start the runner again. A new claim uses the existing task branch and preserves
   earlier commits; an existing open PR is reused. Closed PRs or a mismatched PR
   base stop for review. No changes are reported as failure rather than fabricated
   successful delivery.

Do not discard an ambiguous claim until the original request has settled and its
queue state has been inspected. A missing lookup is not proof a timed-out claim
was never processed. After reporting errors, retain the PR URL in the journal and
reconcile it before rerunning implementation. There is no automatic requeue or
lease expiry. Archived workspaces/logs have no automatic retention cleanup yet;
remove selected archived run directories only while the runner is stopped, and
keep active or unresolved runs.

## Verification

Runner tests use temporary local Git repositories, a fake coding backend, and
stubbed GitHub responses. They do not call a paid model, create real PRs, or claim
production work:

```sh
python -m unittest discover -s coding_runner -t .
docker run --rm --network none --entrypoint python nightly-queue-coding-runner \
  -m unittest discover -s coding_runner -t .
```

`bash test.sh` builds the runner and runs its tests in that image. Django's test
discovery also finds the standard-library tests; Git-dependent tests are skipped
in the web image, which does not install Git, and exercised in the runner image.

A real Codex/GitHub end-to-end acceptance run on a designated test repository is
still required before enabling unattended production work. Start with `--once`
and verify the pushed diff, validation evidence, PR, and queue completion.
