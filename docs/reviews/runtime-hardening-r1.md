# PR 83 runtime hardening independent review, round 1

Reviewed source: `9686417b097b7298256b26dc8db32bfcb30168ae` on
`fix/executor-qualification-readback`.
Accepted base: `a679ecb19dd18ab4b2f1c765c4bbf6c7de816351`.
Review date: 2026-11-16. This report changes documentation only.

## Findings

No P0, P1, or P2 findings in the reviewed diff. The four fixes are suitable to
integrate at this exact source revision. This is runtime review evidence, not
native adapter or MVP release acceptance.

## Scope and reasoning

Read the repository instructions, feature 02 in
`docs/autonomous-ai-queue-plan.md`, the MVP contract, executor documentation, and
the entire accepted-base-to-source diff. Inspected surrounding launch, ownership,
harvest, termination, export, qualification binding, and installed package paths.

- Qualification version 2 requires eleven checks. Resource enforcement must now
  finish successfully and export a verified workspace independently of deliberate
  memory exhaustion. The OOM exception is confined to the exact fixed profile,
  digest, and limits. Protected receipts require actual kernel OOM/limit counters;
  the lost-init branch additionally requires matching engine identity/image and
  operation label, stopped state, OOMKilled, exit 137, memory/swap/PID limits, and
  confirmed removal. It preserves null task exit and unavailable export. Ordinary
  task status, validation, and native delivery do not gain this exception.
- Prepared recipes must match the current full registered profile before first
  dispatch and before an unopened gate. An already opened operation retains its
  original identity and readback/cancel access after revocation. The extension hook
  does not itself trust a run-derived profile; its provider remains responsible
  for current authorization, as documented.
- Only run-owned workspace, temporary files, and HOME gain execution. The real
  mount options retain nosuid/nodev; protected state and secrets retain noexec.
  Qualification actually executes shebang scripts on all three permitted mounts.
- The runner-only optional-SDK change skips the one positive capability fixture
  when the client is absent, while preserving actionable missing-client coverage.
  Source/client-installed tests still exercise all capability assertions.

## Independent validation

All runtime work used a fresh full clone, its own installed wheel, own image and
private state, and only resources carrying that state's executor/operation labels.
No live stack, production database, host login/configuration, model credentials,
or paid model calls were used.

Environment: Python 3.12, rootless Docker 29.9.3/API 1.56, cgroup v2, overlay2,
seccomp and private cgroup namespaces, kernel `7.3.4-1-cachyos`.

- Built task image:
  `sha256:020364dc9df92138249f6cdf48c83b3855c849b024e112d76536cbb04ab31015`.
- Pinned disposable PostgreSQL image:
  `sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
- Installed executor wheel SHA-256:
  `c0c14229942612130c2e0f59be9afc8181fc046ebc40b6b541ba1437caa5b16c`.
- Installed executor source binding:
  `4aa3c41c04201f4dda2f392c7705fe62fe17845b07e83fd669590f365d7d0a99`.

| Check | Actual result |
| --- | --- |
| `python -m unittest discover -s vibearr_executor -t . -v` | 70 passed |
| `python -m unittest coding_runner.test_native_runtime -v` | 9 passed with client available |
| Installed runner-only wheel, same native-runtime tests outside checkout with `-I` | 8 passed, 1 positive SDK fixture skipped |
| Configuration API tests on unique disposable PostgreSQL test database | 8 passed; test database destroyed |
| Black, Ruff, mypy | Passed; Black/mypy covered 308 source files |
| Migration drift and Django system checks | No changes; no issues |
| Installed executor full OCI qualification outside checkout | All 11 v2 checks passed twice, including a changed registry |
| Installed `artifact_smoke` outside checkout with `-I` | Auth rejection, actual offline task, verified export, and confirmed termination passed |

The actual qualification covered credential-free package installation, permitted
HTTPS without model authentication, blocked egress/control/host access, hostile
source/import and forged-receipt attempts, secret redaction/export rejection,
ENOSPC/PID/CPU enforcement, deliberate memory exhaustion, restart with the same
container, detached-descendant termination, full PID occupancy cancellation, and
fresh PostgreSQL/cross-operation isolation. Resource jobs exited 0 and had verified
exports. Both complete qualification runs observed protected OOM results with
`oom_kill=1`, memory limit 268435456, and swap limit 0.

Additional reviewer-authored probes used installed modules and real Docker:

1. Prepared two ordinary recipes, then removed one and replaced the other. Ran
   full qualification against the new registry. With actual current capabilities
   reporting qualified, both old operations were rejected before launch;
   `launch_intent` remained false and neither container existed.
2. Removed a profile after its task started. A start retry retained the original
   container ID; cancellation terminated it and exported its started marker.
3. Removed a profile immediately before its first gate. Both initial dispatch and
   retry kept `/state/go` and the task's started marker absent. Stop removed the
   original container; the export contained no started marker and task exit stayed
   null. The probe used a local Runtime subclass solely to inject revocation at
   that precise checkpoint; Docker execution and result readback were real.
4. Inspected `/proc/mounts` inside an actual task: `/work`, `/tmp`, `/home/task`
   had nosuid/nodev and allowed execution; `/state` and `/secrets` had noexec.
5. Killed an actual init with SIGKILL. Status stayed unknown, recorded engine exit
   137 and OOMKilled=false, retained the same container identity, and fenced a
   replacement operation. Explicit stop confirmed removal, retained null task exit,
   and could not export a workspace. Arbitrary signal death did not become OOM
   qualification or successful task evidence.
6. Replaced the local proof temporarily with version 1/incomplete check evidence.
   Actual capabilities revoked qualification; restoring the original v2 proof
   restored readiness.
7. Repeated the exact fixed memory probe four more times in independent installed
   service state; all four recorded protected OOM counters and confirmed cleanup.

Local raw evidence is under `/tmp/vibearr-review-runtime-fix-r1-*.json` and private
state directories with the same prefix. It includes the full qualification,
artifact smoke, registry revocation, gate/mount, and unknown-outcome observations.
All containers and networks for those completed probes were confirmed absent.

## Limits

Six independently executed memory probes selected the allocating child as the OOM
victim. The lost-init OOM branch was inspected in code and its identity/signal/limit
negative tests passed; the author's separate four-iteration evidence was inspected
and includes an actual init OOM with null task exit and unavailable export. That
author observation is supplemental, not an independently reproduced init OOM.

This review did not run a native coding CLI or the later two-issue delivery/merge
release scenario, and does not certify the upcoming validation-profile provider.
The complete application suite was not rerun locally; the focused application,
executor, and runtime tests above passed. At review time, both required PR checks
were successful on exactly the reviewed source:
[Tests](https://github.com/innateessence/nightly-queue/actions/runs/43853116891/job/133250478156)
and
[Executor boundary](https://github.com/innateessence/nightly-queue/actions/runs/43853116834/job/133250477362).
