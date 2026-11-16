# Isolated OCI executor

Feature 02 supplies a real Linux Docker executor and an independent standard-library
package, `vibearr_executor`. It supplies execution, not a coding model adapter.
Feature 03 owns native CLI/session/authentication diagnostics, 15 owns trusted
project validation recipes, and 04 owns queue dispatch and delivery. Native queue
readiness remains false until those integrations exist. Historical proposal
qualification never authorizes this runtime.

## Deployment

Run the executor on a dedicated Linux runner host/account with Docker engine
access. The web application, supervisor, and task processes receive no Docker
socket, engine credentials, production mounts, database access, encryption key,
or other run's state. The supervisor uses authenticated HTTP control only.
Do not mount the executor state directory into the supervisor or a task.

The intended host supports cgroup v2 memory, swap, CPU and PID controls, tmpfs,
container network namespaces, and a one-use NET_ADMIN firewall helper. Rootless
Docker is supported and tested. Qualification fails when these controls fail;
there is no nested bubblewrap or unsupported-check skip path.

Build from reviewed source, record the Dockerfile/base digest and resulting image
ID, and register that immutable ID. Images extending this base must retain its
protected Python package and Python 3.12; every distinct registered task image
undergoes the same actual boundary probes. Never adopt a candidate-controlled
Dockerfile/profile as an operator's trusted recipe. Dependency-image construction
belongs to feature 15's isolated build boundary.

```sh
python3.12 -m venv /opt/vibearr-executor
/opt/vibearr-executor/bin/pip install ./vibearr_executor
docker build -f vibearr_executor/Dockerfile -t vibearr-task:reviewed .
docker image inspect vibearr-task:reviewed --format '{{.Id}}'
docker pull postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f
docker image inspect postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f --format '{{.Id}}'
```

On hosts with a read-only Docker configuration home, pass a private writable
`docker --config /tmp/<build-config>` directory when building. The service never
builds or pulls a task-selected image.

Stop the executor control service before qualification; active or unknown
operations must first reconcile. Qualification itself starts an authenticated
service, exercises the real API and Docker, restarts that service during an active
task, and preserves a private journal/report. It does not claim a queue item,
consume approvals, contact a paid model, or require a successful production run.

```sh
python3.12 -m vibearr_runner.verify --boundary \
  --executor-image sha256:<installed-task-image-id> \
  --database-image sha256:<installed-postgresql-image-id> \
  --executor-state /var/lib/vibearr-executor \
  --profiles /etc/vibearr-executor/project-profiles.json \
  --evidence /tmp/executor-boundary.json
```

`coding_runner.verify --boundary` is the same compatibility entrypoint. Omitting
`--profiles` qualifies the fixed smoke profiles alone. Additional profiles are an
operator-owned JSON list, parsed by `Profile.parse`. Qualification writes the full
installed registry to `<state>/profiles.json` and a private random control token to
`<state>/token`; use these when starting the service afterward. A failed repeat
qualification revokes the previous report before probes begin. Preserve the
whole journal, rather than copying only a success JSON into another deployment.

```sh
python3.12 -I -m vibearr_executor.server \
  --state /var/lib/vibearr-executor \
  --profiles /var/lib/vibearr-executor/profiles.json \
  --token-file /var/lib/vibearr-executor/token
```

The state directory is mode 0700 and the token is mode 0600, readable only by its
service identity. [The systemd example](../deploy/vibearr-executor.service) uses
separate `/etc` profile/token paths; install the generated registry/token there
with the service identity's ownership if using it. It assumes that account's
rootless engine is already running with lingering configured. Configure
`DOCKER_HOST=unix:///run/user/<executor-uid>/docker.sock` in its private
`/etc/vibearr-executor/engine.env` for that host's actual socket. Neither the supervisor nor
the application joins the engine's permission group.

Loopback control defaults to `127.0.0.1:8791`. Remote control requires
`--tls-cert`, `--tls-key`, and an explicit `--bind` address; clients verify TLS
normally and reject redirects. Make only the supervisor able to reach that
control origin. Add `--deny-ip` for additional executor-host public/NAT addresses;
local hostname addresses and all non-public destinations are already denied by
the egress proxy. A changed deny list invalidates qualification. The current
supported operating mode is one active run; unknown runs retain that fence.
An active/unknown operation also fences a replacement operation UUID within the
same run. Only the exact pair of fixed database-only qualification probes can
overlap to test cross-operation network isolation.

Install the executor client package beside a separately installed supervisor and
set `VIBEARR_EXECUTOR_URL` and `VIBEARR_EXECUTOR_TOKEN_FILE` there. The runner
Dockerfiles include the package without installing Docker or mounting its socket.
`vibearr_runner.native_runtime.runtime_diagnostics(policy)` reads the actual
installed capability report. It reports runtime readiness separately from native
adapter readiness, and never reads a legacy proposal or production-release proof.
Both canonical and legacy runner distributions declare the client through the
`native-runtime` extra. Install reviewed local sources together with
`pip install ./vibearr_executor './vibearr_runner[native-runtime]'`, or use their
reviewed wheels. Without that extra, normal standalone CLI imports still work and
runtime diagnostics explain the missing client. With the client installed, missing
control configuration names the two required environment variables explicitly.
The same-object `coding_runner.native_runtime` compatibility import is retained.

Distribution tests build the executor from its sdist, install it with both runner
sdists in clean environments, invoke diagnostics outside the checkout, and verify
packaged helper/image-recipe resources. CI additionally installs the executor wheel
and runs `python -I -m vibearr_executor.artifact_smoke --image sha256:<id> --state <private-directory>`
outside the checkout. This starts its authenticated service and executes/exports a
real offline OCI validation job using only installed Python modules.

## Profiles and manifests

Profiles live outside candidate checkouts and have this exact schema:

```json
{
  "profile_id": "example-agent-v1",
  "version": 1,
  "image": "sha256:<64-lowercase-hex-image-id>",
  "phase": "agent",
  "argv": ["/absolute/image-owned/agent", "fixed", "arguments"],
  "limits": {"cpu": 2, "memory_mb": 4096, "pids": 128, "workspace_mb": 2048},
  "network_hosts": ["api.openai.com"],
  "backend_secret_env": "OPENAI_API_KEY",
  "database_image": ""
}
```

`Profile.digest` is SHA-256 of its canonical JSON (sorted keys, compact separators).
Image references must be installed content-addressed `sha256:` IDs, not tags.
`setup`, `agent`, and `validation` are separate phases. Setup cannot receive a
backend secret. Validation cannot receive one or have external egress. A profile
may select an immutable PostgreSQL image; the executor creates a fresh sidecar,
random throwaway credentials and private network, with no published port or host
data volume. The task receives only its own `DATABASE_URL`. The executor fixes
the worker entrypoint and protected UID regardless of image defaults. The task's
fixed argv runs as UID 1000, with no effective capabilities or new privileges.
Select trusted Python/tool recipes with isolated Python and absolute image-owned
paths so candidate modules cannot replace the recipe itself.

Every prepare request binds the following exact manifest:

```json
{
  "schema_version": 1,
  "run_id": "<canonical-uuid>",
  "operation_id": "<canonical-uuid>",
  "profile_id": "example-agent-v1",
  "profile_digest": "<64-lowercase-hex>",
  "source_digest": "<uploaded-source-sha256>",
  "context_digest": "<uploaded-context-sha256>",
  "phase": "agent",
  "limits": {"cpu": 2, "memory_mb": 4096, "pids": 128, "workspace_mb": 2048},
  "deadline": "2026-11-16T12:30:00+00:00",
  "network_hosts": ["api.openai.com"]
}
```

Limits may reduce the registered ceiling. Memory must cover the workspace plus
128 MiB, and PID limits must be at least 16. The deadline must initially be within
the next hour; the worker converts the remaining time to a monotonic deadline.
Network hosts may only reduce profile permissions and must also respect the
supervisor's frozen execution policy. The supervisor selects the hosts needed by
each phase; the policy's union of package/model hosts need not all appear in a
credentialed agent manifest. Commands, mounts, paths, images, shell/Docker argv,
and credential selectors are not manifest fields and are rejected.

## Authenticated control protocol v1

All endpoints require `Authorization: Bearer <private-token>`, including reads.
Requests/responses are bounded JSON. No endpoint accepts Docker options or host
paths. The standard-library `Client` supplies these methods:

| Method | Endpoint / result |
| --- | --- |
| `capabilities()` | `GET /v1/capabilities` |
| `upload(kind, bytes)` | `POST /v1/artifacts`, `{kind, sha256, content: base64}`; returns digest handle, digest, size |
| `prepare(manifest)` | `POST /v1/prepare`; binds identity, profile and artifacts |
| `supply_secret(operation_id, secret)` | `POST /v1/operations/<id>/secret`, private body; selected agent credential only |
| `start(operation_id)` | `POST /v1/operations/<id>/start`, empty object |
| `status(operation_id)` | `POST /v1/operations/<id>/status`, empty object |
| `logs(operation_id, offset, limit)` | `POST /v1/operations/<id>/logs`; maximum 65536 bytes per read |
| `stop(operation_id)` | `POST /v1/operations/<id>/stop`, empty object |
| `export(operation_id, original_source)` | `POST /v1/operations/<id>/result`; client verifies full digest inventory |

`ExecutorRejected.http_status` distinguishes invalid/auth/conflicting requests
(400/401/409). `ExecutorUnavailable` covers transport/503 uncertainty: look up the
same operation before retrying. Uploads are content-addressed, prepare rejects a
changed manifest under the same operation ID, credential replacement conflicts,
start reconciles the deterministic container identity, and stop is idempotent.
No missing heartbeat or lost response authorizes a second task process.

Status is `not-started`, `running`, `exited`, or `unknown`. It includes the journaled
container ID once known. An exited result includes the actual child exit code,
timeout/cancel flags, timestamps, sanitized log size/digest/truncation, and either
a verified source manifest or an explicit export error. `descendants_terminated`
is true only after engine removal/absence of all operation containers has been
confirmed. A missing/dead container without a protected result stays unknown;
explicit stop can establish termination but cannot invent its exit code/source.
PID exhaustion may temporarily prevent protected engine-exec readback. A known
running container retains its identity/fence and exposes a wait reason until
readback recovers. A confirmed launch gate is never opened again on a start retry.
Result integrity failures instead become unknown and require recovery.
Qualification also holds all 32 configured PID slots occupied, checks occupancy
through engine-side statistics, retries the same operation, then cancels without
raising its PID limit. It requires confirmed descendant/container termination and
a verified export containing the pre-cancellation workspace marker.

The trusted UID 0 init alone retains minimal setup/termination capabilities. Tasks
cannot read/write its private `/state`, signal it, forge receipts by printing JSON,
or shadow its Python imports. Init terminates and reaps every task-UID process,
including detached descendants, before snapshotting. It stays alive to preserve
tmpfs until the executor has durably copied and verified results. Restart
reconciliation always uses the original container/operation, then completes that
copy before removing resources. Terminating the control service does not destroy
the task or its recovery identity.

## Files, credentials, network, and capacity

Source is an uncompressed tar of at most 32 MiB and 10000 entries; context is at
most 1 MiB. Traversal, absolute/noncanonical paths, duplicate entries, file/parent
collisions, `.git` metadata, symlinks, hardlinks, devices, FIFOs, and special modes
are rejected. Extraction writes individually validated files, never `extractall`.
The task creates fresh disposable Git metadata. Its Git configuration, hooks,
refs, and metadata never reach the supervisor's publishing checkout.

Results contain a full regular-file snapshot and exact per-file SHA-256/size/Git
mode (0644/0755), source/archive digests, and a deletion list. The supervisor verifies
the entire inventory before importing changes into its separate, fenced trusted
checkout. `import_result` rejects a changed base, existing linked path, untracked
overwrite, or a conflicting directory; it preserves additions, deletions,
executable bits, and file/directory transitions. Publishing stays supervisor-owned.
Missing exports, digest failures, unknown exits, or truncated events cannot be
treated as a successful native delivery.

The root filesystem and generated input mount are read-only. Workspace, temporary
HOME, `/tmp`, protected result state and shared memory use separately sized tmpfs
mounts charged to the task memory limit. This enforces workspace capacity on
rootless overlay2/extfs without claiming an unavailable overlay disk quota. CPU,
memory/swap, PIDs and file descriptors are also constrained. Docker logs are
rotated and bounded for every task/helper/proxy/database container. Artifact storage
is capped at 512 MiB, operation count at 1000 and journal admission at 2 GiB; less
than 128 MiB free blocks new work. Active/unknown data is retained for recovery.
Archive completed journals and unreferenced artifacts during offline maintenance;
never remove uncertain active state to free capacity. General retention remains
separate from task workspace enforcement.

Only the selected backend credential is uploaded privately. Trusted init loads it
before launch, then the executor erases its host input file before opening the
agent gate. Init exposes a private per-run file and the profile's selected variable
only to the agent phase; Docker's recorded environment contains no backend key.
The file is deleted when the agent stops. Known values are redacted from bounded
streaming/final logs and result strings; source containing the raw selected key is
withheld. Backend keys remain available to the CLI's own tools, which can encode
or exfiltrate them through an allowed provider endpoint. Use a dedicated account
and provider-side spending limits; this is not a credential broker or dollar cap.

Each operation has an internal network, with no general route. Before its launch
gate opens, a separate fixed helper installs IPv4 OUTPUT allow rules only for that
operation's proxy and optional DB; IPv6 is denied. The task never has NET_ADMIN,
NET_RAW or the Docker socket. Direct DNS, gateway/host, LAN, metadata, production
DB/broker/control origins and direct public traffic are denied. The proxy only
permits CONNECT to exact registered public DNS hosts on 443, validates all IPv4
answers against private/reserved/multicast/host addresses, and connects to the
validated numeric address without a second DNS lookup. IPv6 destinations and
literal addresses are rejected. Validation has no proxy or external network.

## Capability and qualification evidence

`GET /v1/capabilities` returns schema/protocol version 1, `runtime: docker-oci-v1`,
`qualified`, `accepting_work`, storage measurements, `binding_digest`, and installed
profile IDs/digests/images/phases/versions/limits/hosts/credential-variable/database
image. `native_agent_ready: false` means this executor does not attest native
backend availability; feature 03 supplies that separate diagnostic. The runtime
diagnostic can succeed before any production claim or model call.

Qualification version 1 binds executor source digest, engine ID/version/API,
kernel, security options, cgroup/storage driver, active-run limit, denied host
addresses, exact registry digests and installed immutable image contracts. Every
distinct task image must have a complete actual host probe report. Changes or
missing images revoke readiness; a boolean setting or historical proof cannot
restore it. Unqualified execution is restricted to the exact installed fixed smoke
recipes needed for automated qualification. They do not emulate a model session.

The PR `Executor boundary / Qualify OCI executor` check runs actual Docker probes
on Linux and uploads their report. The manually dispatched deployment workflow runs
the same verifier directly on the executor host, never nested in the supervisor.
The checks exercise hostile source/imports and forged receipts, selected-secret
isolation, credential-free package installation, model HTTPS reachability without
auth, offline validation, real ENOSPC/PID/OOM/CPU enforcement, restart-stable launch,
detached descendant stop/export, and fresh PostgreSQL/cross-operation isolation.
The deliberate OOM probe can end with the child or the probe process killed by
the kernel. It passes only after the workspace/PID/CPU checks completed and the
protected result records actual OOM/PID/throttling counters. The real nonzero exit
is preserved in the report; this exception applies only to that fixed resource
qualification recipe and cannot authorize a native delivery or passing validation.
Unit tests cover malformed archives, contracts, API auth/conflicts, DNS rebinding,
and material qualification invalidation. No report substitutes these probes for
feature 03's genuine authenticated native editing session or the later two-issue
delivery/merge release acceptance.
