# Standalone runner supervisor

The `runner` Compose profile adds a read-only HTTP supervisor. Model execution,
claims, repository commands, and delivery remain disabled regardless of web
settings. This branch does not establish live backend or release acceptance.
Startup authenticates `/api/v1/runner/identity/` and reads the versioned
`/api/v1/runner/configuration/`, then looks up its owned active run even when the
local journal is absent. `--check` exits after these reads (and read-only
reconciliation if a journal operation is pending). The normal loop refreshes
configuration without consuming work.

Provision the separate runner identity with `manage.py provision_runner`, following
its `--token-env` help. Put the same secret in the supervisor's `NQ_RUNNER_API_TOKEN`;
`NQ_API_TOKEN` remains the independent general CLI credential. Configure bootstrap
values in an untracked environment file. Do not pass the database password,
credential encryption key, issue-import secrets, or backend credentials to the
supervisor.

```sh
docker compose --profile runner build coding-runner
docker compose --profile runner run --rm --no-deps coding-runner --check
docker compose --profile runner up -d coding-runner
```

The queue application must be running and its migrations applied first. The
runner has no Compose dependency that starts PostgreSQL or Valkey. Authentication
rejection stops the process; unavailable queue reads retry with interruptible
1, 2, 4, 8, 16, 32, then 60-second waits. `--check` fails immediately when a probe
fails. SIGINT/SIGTERM interrupt waits, release ownership, and exit; an in-flight
HTTP request is bounded by its request deadline/socket timeout. Authentication
errors are logged without request/response bodies, URLs, or bearer values.
The controller's deadline includes DNS and response headers. At most one
transport worker exists per client; a stalled transport blocks further requests
until it returns, rather than accumulating connections. A timed-out mutation
remains ambiguous even if its transport completes later. Shutdown is checked
between probe and reconciliation reads, so only the current request may finish
before controller ownership is released.

## State and recovery contract

`runner-data` is the dedicated persistent volume at `/var/lib/coding-runner`.
The directory is mode 0700, and journal files are 0600. Journal writes flush the
new file, replace atomically, then sync the directory. A process-held `flock`
prevents a second controller from sharing that volume, including another
container. The lock is released automatically on process death; never delete a
lock file to recover ownership. Journals bind to the authenticated runner ID.
Corrupt, insecure, or foreign journals fail closed instead of resetting state.

`journaled_request` records a pending operation before sending its POST and
records the returned response before proceeding. Network failures, malformed
responses, and server errors after mutations are ambiguous. A conflict also
requires reconciliation. The HTTP client never retries writes. A pending
operation survives restart and blocks another mutation. Read-only reconciliation
stores owned-run lookup results, including a not-found result, but does not clear
the operation, repeat the write, requeue an item, or infer successful delivery.
Finishing recovery and advancing verified checkpoints belong to branch 11.
Claim tokens belong only in protected journal state, never display artifacts.
Retain and back up the volume as sensitive material; volume deletion is not a
routine recovery action.

## Deployment boundary

The separately built Python 3.12 image copies only `coding_runner/`, uses UID/GID
10001, pins its base digest and Git/GitHub CLI package versions, and does not
install Django or the application requirements. `coding_runner/pyproject.toml`
is its independent dependency manifest; runtime dependencies are standard library
only. `nq` retains its existing independent packaging and imports.

Compose mounts only the named runner volume and an ephemeral `/tmp`. The
filesystem is read-only, all Linux capabilities are dropped, privilege escalation
is disabled, and the process count is limited. No source, home directory, Docker
socket, or app secret mount is shared. Only the Django app joins the internal
`runner-api` bridge with the runner; PostgreSQL and Valkey stay on the default
network. The runner's separate egress bridge permits future provider connections.
This is a deployment separation, not an agent sandbox or protection against a
host/network administrator. No agent code runs in this image until the dedicated
workspace, approval, backend, and release controls are implemented and verified.

## Validation

```sh
python3.12 -m unittest coding_runner.test_runtime
python3.12 -m unittest nq.test_cli
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python nightly-queue-coding-runner \
  -m unittest coding_runner.test_runtime
```

Tests cover request bounds/errors, ambiguous mutations, journal restart and
replacement failures, conflicting controllers, ownership changes, no-claim
startup/retry behavior, and graceful termination. Image validation must also
check non-root UID, absent Django, named-volume persistence, and resolved Compose
mount/environment/network boundaries. A fixture HTTP server can prove startup
connectivity without changing real queue items. Controlled end-to-end execution,
paid backends, and real PR delivery remain unverified and disabled.
