# Feature 04 runtime independent review, round 2

Reviewed executable and documentation head: `0a2fc3fba47a44ea9fa6a32c61f5ada342e647a2`.
Accepted comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Topic: [PR 93](https://github.com/innateessence/nightly-queue/pull/93),
`feat/native-runtime-controls` into `feat/unattended-issue-to-pr`.
Review date: 2026-11-16. This report changes documentation only.

**Outcome: no P0, P1 or P2 finding identified in this fresh review.**
The historical R1 finding at `8c6c6b4` is corrected at the reviewed head.
This conclusion covers the runtime topic, not the separate server/controller
integration or complete feature 04 issue-to-PR release acceptance.

## Scope and source assessment

This was a new full clone and independent review of the complete accepted-base
diff and surrounding runtime, engine, worker, API/SDK, qualification, dependency
construction/qualification, artifact, provenance, retention and native diagnostic
code. It was not limited to the R1 correction. The review read AGENTS.md,
feature 04's plan and MVP requirements, native runtime controls, native Codex,
isolated executor/validation documentation, both feature preparation notes, and
the historical [R1 report](https://github.com/innateessence/nightly-queue/pull/96).
Author evidence was inspected as topic content and was not counted as independent
execution proof.

Operation controls retain the original UUID/container, launch binding, profile
authorization and immutable UTC plus same-boot monotonic deadline. Paused status
avoids engine-exec readback. Host expiry and cancellation remove the original
paused resources without inventing an export. Prepared expiry erases selected
secret input without launching work. Unknown identities and uncertain engine
outcomes preserve the execution fence; stop remains available after revocation.

Build pause intent persists independently of the runtime lock. Phase gates allow
an existing fixed assembly or qualification to settle, distinguish settling from
a completed checkpoint, and prevent paused ready profiles from becoming usable.
Resume authorization is serialized against newer pause generations. Protected
termination receipts require original setup termination and assembly/qualification
cleanup. A terminal state label alone is insufficient.

Retention validates canonical identities, the expected owning run, strict resource
selections and confirmed termination before deleting payloads. Pending removal
intent survives interruption. Immutable manifests, original receipt digests,
compact outcomes/provenance and tombstones survive removal. Shared source and
derived-image ownership/reference checks remain conservative. Full tombstones
release live admission slots without bypassing retained-byte capacity. HTTP 410,
never-prepared HTTP 404 and uncertain transport remain distinct. Protected
presence binds executor instance, operation, run and canonical manifest digest;
it does not dispatch a prepared operation.

## R1 correction and new observer

R1's counter comparison included legitimate unpaused RPC time. The new probe
establishes its baseline after pause acknowledgement, compares another sample
after service restart while still paused, and requires exact equality. Only then
does it resume and separately require counter advancement with the original
container and PID. Slow-RPC/order and changed-frozen-counter regressions exercise
these measurement boundaries. Increasing a tolerance is no longer the criterion.

The new observer is reachable only from the fixed control qualification recipe,
not task profiles or the public API. Its fixed isolated-Python command reads the
bounded regular integer file at `/proc/1/root/work/pause-count.txt`, using a final
no-follow open. It joins only the exact original owned PID namespace and has no
host PID namespace, mounts, network or delivered credentials. Its separate cgroup
limits are 64 MiB, no swap, 0.5 CPU and 16 PIDs; a five-second alarm bounds the read.
The two read capabilities belong only to this helper; task capabilities are
unchanged. Images with declared volumes are rejected before helper creation.

The helper intent is durable before create and binds instance, operation, target,
image, name and recipe digest. Readback checks original ID, labels, command,
entrypoint, user, namespace, mounts, capabilities, resource limits and security
options. A captured helper ID cannot be replaced. Finally/retry cleanup reconciles
lost create/start/remove responses by exact owned identity; uncertainty preserves
the fence. Runtime cleanup removes the observer before its original task and
confirms absence. The independent unit suite exercised lost-create reconciliation
and rejection of changed ID, namespace and mounts.

## Independent validation

Executor, runner and CLI wheels were built and installed into a new Python 3.12
environment outside the checkout before test discovery. A separate current-source
validation image, private Docker configuration and unique executor state were
used. Only reviewer-owned disposable resources were created or removed.

| Check at the reviewed source | Independently observed result |
| --- | --- |
| Executor unit discovery | 171 passed |
| Full Django discovery with a separate PostgreSQL test database | 1,622 passed; four expected skips |
| Distribution artifact tests with setuptools/wheel installed | Four passed, none skipped |
| Repository-wide Black and Ruff | Passed; 343 Python files |
| Repository-wide mypy | Passed; 343 source files |
| Migration drift and Django system checks | No changes; no system issues |
| Installed-wheel actual rootless OCI qualification with validation/setup profiles registered | All 13 checks passed |
| Installed HTTP presence and selective/full operation retention | Passed |
| Actual dependency build, pause/restart, protected receipt and selective image retention | Passed |

The actual OCI controls proof independently observed frozen counter **3**, counter
**3** after executor restart while still paused, and **6** after resume. Both
samples used different observer containers, retained the same original task
container/PID, and confirmed observer removal. Qualification also exercised
paused cancellation, original-deadline expiry after downtime, never-started secret
erasure, shared-source retention, known removed-artifact readback and terminal
prepare/start replay without another invocation.

Control operation: `d119203a-4e72-4989-ad42-3e8ff8687c64`.
Original container:
`8ed08c45303d8b121e5106d226df3dcc6e746a9971c2eb2740e752ebf5b60fb8`.
Original PID: `2815470`.
Observer containers:
`d060349cb5727d098e1c420feb3a5168f5c1b82f751d2335dfcf021f75170c55`
and `760f8e063369addd4181cbd1ffce25bc8fd9681b8dc6dc8c56e6d296a95daff8`.

The installed HTTP fixture verified prepared presence without launch, exact
manifest/run/instance binding, successful same-ID task completion, protected
terminal receipt, source-only purge preserving logs, full purge, stable receipt
after removal, removed logs HTTP 410, missing presence HTTP 404 and unauthorized
presence HTTP 401.
After another service restart its retained receipt was unchanged; malformed UUID
and nonempty receipt requests returned HTTP 400, and never-prepared stop returned
HTTP 404.

An actual `idna==3.10` dependency build paused before setup without launching,
then froze its original running setup container and preserved it through service
restart. It later paused while an existing derived-image qualification was
running, survived another service restart, and settled to ready while still
paused and unavailable. Explicit resume exposed the ready profile. Its protected
receipt bound the original setup and confirmed assembly/qualification cleanup.
Source-only purge removed the exact derived image and preserved the registered
parent image; subsequent log purge retained that receipt and a non-relaunchable
full tombstone.

Build UUID: `8a52e459-06fc-461b-bcb5-054a86f0e74e`.
Original setup container:
`ef3f5e74b824b3acf675db4f6b1a283774b262b63c921707ab9e53240acf6429`.
Removed derived image:
`sha256:47bda8d53dfd1fe2b5c8250db53127d86ef75b661e31375fb623eb60f2dd6070`.
Stable build receipt digest:
`d5d77eaa041d15f0c82e428e965d24be72a5aed7963d834c14c2dc4c4c7c5c72`.

Host: rootless Docker 29.9.3, cgroup v2.
Independent current-source validation image:
`sha256:c1c617f24d4829de8e69ded59465582bb95260222545c90873c921a9e67d7525`.
Pinned PostgreSQL image:
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
Installed executor source binding:
`bb447de81c9a7956daf800981033c0325949e316126c81ac8d975bf7c4c74b6f`.
Independent executor wheel SHA-256:
`8e10861bef4289648da064dbdde70841fa8b17702328caafe8138db750447956`.
Independent runner wheel SHA-256:
`f1931e43c86ce51de67a1a7b95a76e8b03743ea97c34bb95c39b7c1e5b9567e5`.

Local independent driver and proof files are retained at
`/tmp/vibearr-review04-runtime-r2-proof.py`,
`/tmp/vibearr-review04-runtime-r2-proof/qualification.json`,
`/tmp/vibearr-review04-runtime-r2-proof/functional.json`, and the matching
`-proof.log`, `-functional-pass.log`, `-django.log` and `-artifacts-real.log` files.
The first presence fixture omitted the fixed smoke recipe's `delete.txt`; it
produced an honest exit 1 and was corrected. A build fixture initially paused at
the pre-qualification checkpoint, which correctly prevented qualification from
starting. That fixture was interrupted, explicitly stopped/reconciled and purged;
the corrected fixture waits for the existing qualification intent. A subsequent
ordinary launch was correctly fenced until that interrupted build was reconciled.
These were reviewer fixture corrections, not implementation changes.

Ownership-filtered checks confirmed zero containers and zero networks across both
reviewer executor instances, including the derived-image qualification instance.
Every retained observer intent confirmed removal and engine absence. The separate
test database was destroyed and the empty reviewer database dropped.
No image prune or cleanup of unrelated resources was performed. This review used
ordinary functional/regression checks and the existing required qualification,
with no additional adversarial or host-network attack probes. It used no paid
model, production application/database, other reviewers' runtime resources, or
external repository delivery. Complete exact-candidate nine-check acceptance,
native server/controller integration and real issue-to-PR acceptance remain
separate integration/release requirements. Root retains sole merge authority.
