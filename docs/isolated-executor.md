# Docker worker target and historical OCI protocol

The existing `vibearr_executor` package implements the earlier host-launched OCI
runtime. The supported target is now the Docker-only worker contract below; the
earlier implementation is reference material, not a compliant deployment.
Feature 03 owns native CLI/session/authentication diagnostics, 15 owns trusted
project validation recipes, and 04 owns queue dispatch and delivery. Native queue
readiness remains false until those integrations exist. Historical proposal
qualification never authorizes this runtime.

[Native runtime controls](native-runtime-controls.md) documents pause/resume,
deadline reconciliation, terminal artifact retention and state identity checks.

## Docker-only deployment requirement

The [Docker-only runtime plan](docker-only-runtime-plan.md) replaces the host
launcher described by the existing implementation. Do not install this package,
Python, service files, coding tools or qualification utilities on the host.
There is no systemd, lingering, host socket, privileged container or host-path
mount option. Old host qualification is historical evidence only.

Correction D2 replaces dynamic daemon dispatch with Compose-provisioned worker
slots and authenticated control APIs. Trusted worker PID 1 runs one job at a time
under a separate UID, enforces the slot's actual limits and network rules, seals
results and resets all task state before reuse. A reused slot is not a fresh
container. No application component receives Docker daemon access.

Correction D3 supplies credential-free validation/setup, sealed dependency
artifacts and resettable test databases. D4 supplies containerized qualification,
CLI, packaging, browser capture and backup/recovery jobs. Keep claims disabled
until the actual Docker-only boundary qualifies; missing tools are not a reason
to run host commands.

The sections below describe existing protocol concepts and pre-replacement
implementation evidence. Reuse valid identity/artifact/fencing contracts while
versioning the changed worker/boot/reset evidence. They are not installation
instructions for a supported host service, nor proof the Docker-only worker
implementation is complete. All diagnostic Python examples execute inside a
qualification/tools container once that path is implemented.

## Historical protocol reference: profiles and manifests

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
before launch, then the worker erases its private input file before opening the
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

Qualification version 3 binds executor source digest, engine ID/version/API,
kernel, security options, cgroup/storage driver, active-run limit, denied host
addresses, exact registry digests and installed immutable image contracts. Every
distinct task image must have a complete actual host probe report. Changes or
missing images revoke readiness; a boolean setting or historical proof cannot
restore it. Unqualified execution is restricted to the exact installed fixed smoke
recipes needed for automated qualification. They do not emulate a model session.

The binding's `denied_ips` contains every configured deny address and currently
assigned global host IPv4 address. Non-global destinations are categorically denied
by the proxy, so discovering/removing private bridge gateways does not change
authorization. Each proxy still receives the full current host/configured deny set;
direct task traffic remains restricted to its own proxy/database by the firewall.
Configured deny changes and public host-address additions/removals revoke readiness.
This source change invalidates prior proofs: rebuild/pin worker images, install the
matching executor and rerun host qualification before ordinary work resumes.

The PR `Executor boundary / Qualify OCI executor` check runs actual Docker probes
on Linux and uploads their report. The replacement deployment workflow runs the verifier inside a qualification
container against the Docker-only worker slots, without mounting a host socket.
CI also registers a credential-free ordinary validation recipe before qualification,
then launches it with a disposable database. This recipe has no smoke exemption.
`python -m vibearr_executor.network_probe` checks readiness and the exact binding
while its network/database exist, after restarting the control service with the same
task container, and after cancellation/export. Its separate evidence records host
IPv4 snapshots to distinguish rootful bridge topology changes from task failures.
The initial diagnostic-only PR87 CI run passed all eleven smoke checks, then failed
this ordinary-operation assertion: creating the private network added only gateway
`172.18.0.1` to discovered host addresses and revoked the previous binding. The
binding now records the stable authorization inputs described above; the same strict
ordinary lifecycle check remains mandatory on rootful CI and rootless hosts.
The checks exercise hostile source/imports and forged receipts, selected-secret
isolation, pause/resume and deadline reconciliation, terminal retention,
credential-free package installation, model HTTPS reachability without
auth, offline validation, real ENOSPC/PID/OOM/CPU enforcement, restart-stable launch,
detached descendant stop/export, and fresh PostgreSQL/cross-operation isolation.
The workspace/PID/CPU probe must exit successfully and export its verified source
before memory exhaustion runs in a separate, exact `smoke-oom-v1` operation. Its
unchanged 256 MiB memory/no-swap limit can kill the allocating child, task process,
or trusted init. Protected results must contain actual kernel OOM/limit counters.
If init dies, only this fixed memory probe can use matching engine container/image/
operation identity, memory/PID limits, `OOMKilled=true`, and exit 137 as boundary
evidence. The runtime remains unknown until explicit stop confirms every descendant
gone; the report keeps a null task exit and marks its workspace export unavailable.
Ordinary unknowns, arbitrary signals, foreign identities and missing receipts never
become successful tasks or validations. Protocol/schema stay at 1; earlier
qualification reports must be replaced by all thirteen version 3 checks.

Qualification prints safe profile/operation progress and preserves unknown-state
diagnostics, including engine exit/OOM metadata, before cleanup. It omits source,
credentials, Docker argv and private engine error strings. A local four-iteration
reproduction against accepted executor source `2b2a67a` produced three protected
OOM results, then an unknown init with engine exit 137 and `OOMKilled=true`; the
previous combined probe could not reliably preserve its own successful checks.
The original CI failure had no phase/engine diagnostic, so its exact cause cannot
be established retrospectively from that log alone.

Before first dispatch and every unopened gate, the prepared profile must still
match the current authorized profile's complete digest/data. A removed/replaced
recipe cannot inherit another registry's successful qualification. Existing opened
operations retain frozen readback/cancel access after revocation. Validation profile
providers can extend `Runtime.registered_profile(manifest)` with current run-scoped
certificate checks; they must not insert stale profiles into the static registry.

Run-owned workspace, temporary files and HOME explicitly permit execution, with
`nosuid,nodev` retained. Protected state/secrets remain `noexec`. Real qualification
executes a shebang script from each run-owned mount, as well as verifying exported
executable modes. Runner-only installations without the optional SDK skip only the
SDK-dependent positive fixture and still test actionable missing-SDK diagnostics;
source/native-extra environments run the full capability tests.
Unit tests cover malformed archives, contracts, API auth/conflicts, DNS rebinding,
and material qualification invalidation. No report substitutes these probes for
feature 03's genuine authenticated native editing session or the later two-issue
delivery/merge release acceptance.
