# Feature 04 runtime independent review, round 1

Reviewed executable source: `fe3baca6b1c54f9e0cf883b6051738adac87b589`.
Accepted comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Reviewed final documentation/evidence head: `9d8ece7dd6f860b209aa1d74ca0d556cc3447108`.
Topic: [PR 93](https://github.com/innateessence/nightly-queue/pull/93),
`feat/native-runtime-controls` into `feat/unattended-issue-to-pr`.
Review date: 2026-11-16. This report changes documentation only.

No P0, P1, or P2 findings were identified in the reviewed runtime topic. Integration
remains subject to root review and successful CI on the exact topic and report
heads. This report does not clear later executable changes or the separate native
server/controller integration.

The full accepted-base-to-source diff and surrounding runtime, engine, API/SDK,
qualification, dependency assembly/qualification, provenance, retention, artifact,
validation, and native diagnostic code were inspected. The review read AGENTS.md,
feature 04's plan/MVP requirements, native runtime controls, native Codex,
isolated executor/validation documentation, and both feature preparation notes.
A subsequent byte diff confirmed that commits after the reviewed executable SHA
change only three documentation/evidence files. Those deltas were inspected.
The stale version-2/eleven-check wording in `docs/isolated-executor.md` was noted
and corrected by the author in the final documentation-only commit.

The implementation preserves the original operation UUID, recorded container ID,
launch binding and immutable deadline through pause/resume. Paused status avoids
protected engine-exec reads; host expiry removes the original container without
inventing an export. Stop/reconciliation remains available after authorization
changes. Prepared expiry erases selected secret input without creating a container.

Build controls persist pause intent independently of the runtime lock, distinguish
an in-flight phase from a settled checkpoint, and serialize resume authorization
against newer pause generations. An existing fixed assembly or qualification can
settle; the gate prevents entering the next phase and prevents exposing a paused
ready profile. Protected build receipts require original setup termination,
assembly cleanup and qualification cleanup rather than trusting a terminal label.

Retention checks canonical identities, the owning run, strict resource selections
and confirmed terminal resources. Payload removal preserves the immutable manifest,
original receipt digest, compact result/provenance and non-relaunchable tombstone.
Shared artifacts and images retain reference/ownership checks. HTTP 410 remains
distinct from never-prepared HTTP 404 and transport uncertainty. Fully removed
records release live admission slots while retained metadata remains subject to
byte capacity. Qualification version 3 retains the previous eleven checks and
adds two actual control/retention checks without changing wire protocol 1.

Independent verification used a new full clone, independently built executor and
runner wheels, a new Python 3.12 environment outside the checkout, a separately
built validation image, private Docker configuration, separate executor journals,
and only reviewer-owned disposable resources. Wheels were built and installed
before test discovery. Author evidence was inspected as topic content; it was not
counted as this reviewer's execution proof.

| Check | Independently observed result |
| --- | --- |
| Executor unit discovery | 161 passed |
| Portable runner unit discovery | 581 passed |
| Full Django discovery with an isolated PostgreSQL test database | 1,612 passed; four expected skips |
| Repository-wide Black and Ruff | Passed, 341 Python files checked |
| Repository-wide mypy | Passed, 341 source files |
| Migration drift and Django system checks | No changes; no system issues |
| Installed-wheel actual rootless OCI qualification | All 13 required checks passed |
| Second actual qualification with parent validation/setup profiles registered | All 13 required checks passed |
| Actual installed-wheel dependency build, pause/restart, provenance and selective retention | Passed; exact derived image removed, parent preserved |
| Actual paused never-launched build expiry and never-prepared stop | Passed; protected terminal receipt and same-instance HTTP 404 |

The actual control probes exercised frozen writes, same PID/container identity,
service restart, pause/cancel, original-deadline expiry after downtime,
never-launched secret erasure, source-only retention preserving logs, shared source
references, HTTP 410, and terminal prepare/start replay without another launch.
Mocked regression cases independently run in the unit suite also cover lost
pause/resume responses, changed binding, unknown engine, generation ordering,
image references, and admission at the 1,000-operation and 32-build caps.

An actual `idna==3.10` build paused before setup, then froze its original setup
container and retained that ID and monotonic deadline after service restart. It
later reported `pausing-at-checkpoint` during actual derived-image qualification,
survived another service restart, settled to ready while still unavailable, and
exposed profiles only after resume. Its provenance parsed against the original
parent and run. A prepared ordinary validation operation protected the derived
image from source purge with HTTP 409. After that reference was stopped and fully
purged, logs-only then source removal retained the original protected build
receipt, removed the exact derived image, preserved the operator parent, returned
HTTP 410 for removed setup payloads, and replayed prepare/start as a tombstone.

Build UUID: `aafbaec5-7c2f-413d-b2ce-d0d3834cce82`.
Original setup container:
`f4fbbbda19b02f35196c6865c401db0e4ef4fc68706c231da1ba7e99f3349213`.
Removed derived image:
`sha256:5e83707bf6f43512a2d211de5374fa3ec21b01e25003e36dbdffb7dfb0cd7ff0`.
Stable protected build receipt digest:
`6df8accf79fd86bc2a6d6aaededa4e85450d5230e2ba9dc28748de5efdb8cadb`.

Host: rootless Docker 29.9.3, cgroup v2. Independently built parent image:
`sha256:cd06fd09ec2e2ae391db84fe8569df1a2f09d247772a8cf9415cdda14e6b91b3`.
Pinned PostgreSQL image:
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
Installed executor source binding:
`89d8fa01ad2a87fd1af1a56baad7d85683539be451551504b96b89464a19232a`.
Executor wheel SHA-256:
`58f6cc45ca3799c9668069d2a2427ca981ba553be69ab0f08848e9542f31ab9e`.
Runner wheel SHA-256:
`351908823dafe49f82022327137a3cc12caf2c6f8d8ffe5bbeb5fc887f25a035`.

The final ownership-filtered check found zero containers and zero networks across
all three reviewer executor instances. The reviewer test database was destroyed,
and the separate empty migration-check database was dropped. No image prune or
cleanup of unrelated resources was performed.

Local independent evidence and drivers are retained under:

- `/tmp/vibearr-review04-runtime-r1-boundary.json` and `-boundary.log`.
- `/tmp/vibearr-review04-runtime-r1-build-state/qualification.json`.
- `/tmp/vibearr-review04-runtime-r1-build.py`, `-build-retry.log`, and `-build.json`.
- `/tmp/vibearr-review04-runtime-r1-expiry.py`, `-expiry-retry.log`, and `-expiry.json`.
- `/tmp/vibearr-review04-runtime-r1-unit.log`, `-runner.log`, and `-django.log`.
- `/tmp/vibearr-review04-runtime-r1-black-all.log`, `-ruff-all.log`, `-mypy.log`,
  `-migrations.log`, and `-system.log`.

The first ordinary build driver incorrectly expected immediate setup freeze while
the response correctly reported `pausing-at-checkpoint`. Its build was explicitly
stopped and cleanup attested. The driver was corrected to wait for the documented
checkpoint before asserting pause; this was a reviewer fixture correction, not an
implementation change. The expiry fixture likewise uses bounded polling for the
monitor acknowledgement rather than assuming it completes at a fixed sleep boundary.

This review performed ordinary functional/regression validation and the existing
required qualification, with no additional adversarial or host-network attack
probes. It did not use paid models, production application/database resources,
other reviewers' runtime resources, or external repository delivery. The complete
nine-check exact-candidate validation acceptance, real native server/controller
integration, live issue-to-PR acceptance and rootful deployment behavior remain
separate release/integration evidence. CI was still running when inspected; this
report does not substitute local results for the root's exact-head CI gate.
