# Feature 04 runtime independent review, round 1

Final reviewed executable source: `8c6c6b43143e532f89e81ae5d8831d4e393425b6`.
Initial controls review source: `fe3baca6b1c54f9e0cf883b6051738adac87b589`.
Accepted comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Documentation and recorded evidence were also reviewed through the final source head.
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
A subsequent byte diff confirmed that updates through `9d8ece7` changed only
three documentation/evidence files. Those deltas were inspected, followed by the
final narrow receipt API addition described below.
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

The following broad controls checks were run against the initial `fe3baca`
source; they are historical proof for the unchanged controls/build implementation.

| Check at initial source | Independently observed result |
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

Initial-proof host: rootless Docker 29.9.3, cgroup v2. Independently built parent image:
`sha256:cd06fd09ec2e2ae391db84fe8569df1a2f09d247772a8cf9415cdda14e6b91b3`.
Pinned PostgreSQL image:
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
Initial installed executor source binding:
`89d8fa01ad2a87fd1af1a56baad7d85683539be451551504b96b89464a19232a`.
Initial executor wheel SHA-256:
`58f6cc45ca3799c9668069d2a2427ca981ba553be69ab0f08848e9542f31ab9e`.
Initial runner wheel SHA-256:
`351908823dafe49f82022327137a3cc12caf2c6f8d8ffe5bbeb5fc887f25a035`.

The final API delta from `9d8ece7` to `8c6c6b4` was independently assessed after
merging the stable topic into this report branch. It adds the SDK action and
method, authenticated empty-body POST route, and `manifest_digest` to the existing
protected receipt helper. The digest uses canonical bytes of the immutable
prepared manifest. Prepared presence remains distinct from missing identity;
termination truth still comes from reconciliation of the original operation.
The endpoint does not launch work. Authentication precedes routing, canonical
UUID validation precedes record access, and missing records raise HTTP 404.
Status reconciliation can still enforce expiry or finish existing cleanup.

Byte comparison confirmed that operation/build pause controls, build receipts,
retention, dependency construction and control qualification modules are unchanged.
The new manifest digest is additive to the setup receipt consumed by build receipts.
The earlier full actual dependency build matrix was not repeated at the final
source. Its source binding remains `89d8fa01...`, distinct from the new proof.
The final checked-in author evidence likewise labels the older build/edge proofs
separately from current-source qualification and HTTP presence evidence.

| Final API delta check at `8c6c6b4` | Independently observed result |
| --- | --- |
| Executor unit discovery | 164 passed |
| Repository-wide Black, Ruff and mypy | Passed; 341 Python/source files checked |
| Fresh installed-wheel actual OCI qualification with an ordinary profile registered | All 13 checks passed |
| Installed HTTP SDK presence lifecycle | Prepared binding/no launch, service restart, same-ID ordinary task, protected terminal result, selective/full purge and restart-stable receipt passed |
| API error semantics | Missing HTTP 404, unauthorized 401, malformed UUID and nonempty-body 400 passed |

Current independent validation image:
`sha256:4a20268fd67d74857b27bbb59e6c76cae115af57408a03a6c21380ad80f20231`.
Current installed source binding:
`553daee7b684a60969c845730dd6a9c780c2bc7f5a642c084731783c26c81e71`.
Current executor wheel SHA-256:
`54e2dcbea20ab89bd9f0e3628910787f705450e53472b2a8cfd0b85d140f5b3a`.
Current runner wheel SHA-256:
`1af3c7b41d8dd3b424f149439fe158dc9259240b69154500723b53001266afab`.
Current independent driver/evidence:
`/tmp/vibearr-review04-runtime-r1-api-proof.py`, `-api-proof-retry.log`,
`-api-boundary.json`, `-api-presence.json`, and the `-api-unit.log` and
`-api-{black,ruff,mypy}.log` check logs.
Final ordinary operation: `10804f40-aef2-40b4-a568-ec13d8432134`.
Prepared manifest digest:
`ee9338d13718108e6f49e9df2963d74ea15621e37398c5bb76f5daf46aedec7c`.
Protected receipt retained unchanged after both purge stages and restart:
`48c10baebda99e992058ebb5926abe2e9781b7b17dff00f318a2e5e436b40508`.

The initial ownership-filtered check found zero containers and zero networks across
all three reviewer executor instances. The final API proof used one additional
reviewer instance; its ownership-filtered check also found zero containers/networks. The reviewer test database was destroyed,
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
The first presence driver used the wrong SDK export return type after the actual
task completed; this fixture was corrected, then a fresh ordinary operation ran
the full HTTP lifecycle using the completed genuine current-source qualification.

This review performed ordinary functional/regression validation and the existing
required qualification, with no additional adversarial or host-network attack
probes. It did not use paid models, production application/database resources,
other reviewers' runtime resources, or external repository delivery. The complete
nine-check exact-candidate validation acceptance, real native server/controller
integration, live issue-to-PR acceptance and rootful deployment behavior remain
separate release/integration evidence. CI was still running when inspected; this
report does not substitute local results for the root's exact-head CI gate.
