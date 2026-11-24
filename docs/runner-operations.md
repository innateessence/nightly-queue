# Runner operations and release verification

> Historical implementation reference. Deployment and authentication instructions
> here are superseded by the [three-container runtime](docker-only-runtime-plan.md)
> and [current operations](docker-operations.md). Do not start the old worker fleet,
> use model API keys, or reuse its qualification evidence for the integrated runner.

Use [Docker operations](docker-operations.md) for current container commands,
identity bootstrap, qualification, backup and empty-target restoration.

> **Docker-only runtime requirement:** Follow the [Docker-only runtime plan](docker-only-runtime-plan.md)
> and [deployment contract](mvp-deployment.md). Every Python, Git, coding CLI,
> packaging, test, qualification and browser command in this reference executes
> inside its appropriate container, never on the host. Host-service/daemon-launcher
> procedures and their old proofs are historical and cannot qualify the new worker
> boundary. Missing container tools must be implemented in D1–D4; no host fallback.

## Native policy upgrade

The [native execution policy](runner-execution-policy.md) replaces human command
approval. Migration `0009_autonomous_execution_policy` disables execution, preserves
approval history, and requests cancellation of legacy active runs without releasing
their ownership fence. Stop/reconcile recorded children and unknown external effects
before finishing/retrying that work. Do not restore an old approved command merely
because its decision still reads Approved.

New claims snapshot native policy and a version 2 delivery contract. Research-only
settings no longer gate native execution. The [OCI executor](isolated-executor.md)
provides the earlier protocol reference. The replacement qualification runs in
containers against the socketless worker stack.
Native account readiness, worker qualification and delivery authority are
separate gates. Runtime diagnostics work before any production claim. The
controlled proposal qualification procedure below is retained for historical
recovery/readback and cannot qualify native work. Use the [MVP release
contract](vibearr-mvp.md) for the replacement automated qualification path.


The operator UI remains a private-LAN, single-user interface. Browser writes use
CSRF protection. Runner bearer authentication and the private controller proof
stored in its volume are separate machine boundaries. Do not expose the browser
UI publicly as an authenticated multi-user service.

## Bootstrap and upgrades

1. Back up PostgreSQL, the credential encryption key, and the stopped runner
   volume. Keep the key and tokens outside Git and outside task workspaces.
2. Follow [Docker initialization](docker-operations.md#initialize-and-bootstrap).
   The migration container applies migrations before web startup. Use the
   `docker_bootstrap_runner` tools command to persist the separate identity and
   token in the private secret volume without rotating existing authority.
3. Configure backend, model, validation commands, issue-reading credential, and
   repository-delivery credential through Settings. Issue-import credentials do
   not implicitly authorize runner access. Explicit preflight credential grants
   remain necessary. GitHub permissions must cover issue/comment reads, repository
   contents writes, PR writes, and Actions run/artifact reads for qualification.
4. Build the independent runner image and start the Compose `runner` profile.
   Set only infrastructure bootstrap variables (`VIBEARR_URL`, runner token, private
   state volume) outside the UI. The service has no database or encryption key.
5. Run the authenticated `--check` command. It reads configuration without binding
   new ownership or claiming work. Use the same private volume on every restart.
6. Qualify the [Docker-only worker stack](docker-operations.md#worker-readiness). Keep execution
   disabled until the native adapter/authentication, validation and dispatch path
   is installed. The historical controlled proposal qualification below cannot
   authorize native execution.

Before upgrading, pause through Settings, wait for a safe checkpoint, and stop
supervisor and all recorded children. Snapshot the entire private runner volume,
including journal, controller proof, task workspaces, source bindings, and logs.
Upgrade server migrations before starting its matching runner image. Restore
PostgreSQL and runner volume from a consistent checkpoint. Never copy a live
volume to start a second supervisor; the remote proof and local lock both matter.

## History, controls, and recovery

Runs lists paginated attempts and the whole queue's blocking reasons. Detail
shows saved context and plan versions, exact approvals, configuration frozen at
claim, stage/wait evidence, validation, diagnostics, commits, and PR identity.
Quota is per account and allowance window, with observation/reset/recheck times;
an expired report is not permission to resume. Storage reports also display their
received time; they do not establish that an old process has stopped.

A PR can be delivered while completion reporting remains pending. Preserve its
volume, claim token, PR identity, and delivery evidence. Restart the same owner to
reconcile and report completion; do not create another branch or PR. Cancellation
requests retain ownership until backend and child-process termination is confirmed.
Pause, schedule, and reserve changes apply at safe checkpoints. Resume those
controls in Settings; unrelated waits or unknown outcomes still require recovery.

An issue import can supersede an in-flight source read even when repository
identity is unchanged. The API returns `409 source_observation_superseded` only
for a newer observation of the same authority. The native supervisor pauses its
owned operations and collects fresh provider context, with at most three attempts
and journaled backoff across restarts. Success resumes the same operations without
extending their deadlines. Exhaustion terminates normally before releasing the
claim. Changed repository/owner/default branch, permissions, and other source
rejections retain their existing fences; a generic conflict is not retry permission.

For a missing or damaged volume, stop the old supervisor and children first.
Restore the corresponding protected volume backup. An active server run with no
matching journal or private controller proof fails closed; neither elapsed time,
a new empty volume, nor the browser cancellation button grants takeover. Inspect
Git/PR outcomes before considering an operator-owned queue requeue; requeue makes
the old run obsolete and fences its writes. Pending obsolete approvals cannot be
decided or consumed and keep their original context for reconciliation. There is
no automatic destructive journal reset or controller-secret replacement action.

## Retention and disk pressure

Settings owns workspace, context/plan, and log/uploaded-artifact retention days.
The server exposes a bounded cleanup plan over HTTP; the runner removes its own
UUID-named local directories under locks and acknowledges removal. Django never
assumes a shared runner filesystem. Only redacted structured evidence is uploaded.
Raw command output is bounded at 1 MiB per execution, stored mode 0600 outside
agent workspaces, and referenced by run/execution IDs in uploaded diagnostics.
Raw logs are available to the operator through the protected volume, not the UI.

Operator pages and report readbacks redact only currently selected credentials and
credentials selected by the report's own saved run, including their retained secret
versions. Missing selected material or encryption keys hide that content. Display
copies never rewrite immutable context, plans, or stored evidence. Redacted release
readbacks are marked presentation-only; deployment and credential-version metadata
keep their original identity. The runner's executable configuration API is unchanged.
A settings form containing selected secret material is withheld instead of saving
redacted replacement instructions; remove the material through the trusted admin.
Project override forms use the same object-first JSON and choice-label guard.
Saved effective project values and project-list metadata are display copies; safe
forms remain editable, and sanitized validation feedback remains visible.

A run is eligible only when completed and the owner has separately acknowledged
receiving the exact finish result. A lost finish response, active or paused run,
pending approval, uncertain operation, or unconfirmed child process protects data.
Approval context remains even after a decided approval. A locked workspace is
preserved. Cleanup checks eligibility again before server purge/removal recording.
The ordinary immutable-document save/update/delete guards remain intact; only
this transactional terminal-retention service has a narrow purge escape hatch.

History, final error/result, finish digest, stage/recovery summaries, validation,
and verified delivery evidence remain after expiry. Context/plan and local-volume
removal dates/counters become history tombstones. Missing eligible directories
are recorded as missing; missing active volumes require recovery instead.
After reporting acknowledgement, bulk per-run journal observations are compacted;
current/pending window accounting and active run state survive. Closed counters
are pruned only with a known end time and no remaining references.

Less than 1 GiB free prevents new claims and reports a useful storage reason.
Existing owned work is reconciled first. Expand storage, back up, or remove only
eligible terminal data; disk pressure never deletes recoverable work as a fallback.

## Verification layers

`bash test.sh` runs the full disposable Docker pipeline described in
[Docker operations](docker-operations.md#development-and-ci). Portable runner
checks and Git acceptance run in a tools image; worker qualification additionally
exercises actual UID, network, resource, cancellation, restart and reset controls.
The full run also validates the candidate and a dependency change, takes browser
captures and verifies backup/restore. Portable success alone does not qualify a
deployment or enable claims.

Dispatch `.github/workflows/runner-deployment-boundary.yml` on a designated
self-hosted runner labelled `runner-deployment-qualification`. Supply the reviewed
Compose project name and installed tools image ID (`sha256:...`). The job reads
the authenticated fleet proof and selected operator Docker inspection facts;
it does not restart workers or qualify them itself. Perform the disruptive
synthetic qualification with the supervisor stopped as described in
[worker readiness](docker-operations.md#worker-readiness) first. Every worker
restart invalidates its prior boot proof. Unsupported controls fail qualification;
privileged mode, host security changes or a host executor are not substitutes.

## Live native acceptance

Select the deployment, dedicated project/repository, model account and finite
issue scope explicitly. Keep ordinary claims disabled while preparing the stack.
Boundary qualification makes no paid calls. Then run the
[native Codex fixture](native-codex.md#real-acceptance) in its qualification
container to verify actual authentication, editing, independent validation and
foreground cancellation on the selected account.

Use the [ordinary-controller merge acceptance](automatic-merge.md#mvp-acceptance)
for two owner issues, with B depending on A. The acceptance process uses the same
private runner journal after interruptions and verifies provider CI/merge,
termination and acknowledgement evidence. Collect Docker browser receipts for
each exact validated tree before final readback. These commands make real model
calls and change the selected repository; disposable CI cannot replace them.

Legacy structured-proposal qualification and its human command approvals are
retired. Their stored reports remain historical readbacks and cannot qualify
native workers. The superseded operational procedure remains available in Git
history. A successful live report still requires explicit operator enablement;
fixtures and old worker generations never grant ordinary claims.

Screenshots in `docs/screenshots/runner-operations-*.png` show actual rendered
browser pages with clearly labelled synthetic fixtures; they do not show live
execution acceptance.

## Recover a terminated automatic delivery

A failed native run that already published a PR cannot be requeued for coding.
When its original workspace and protected receipts remain available, the same
controller can explicitly recover delivery once, using the scoped API and CLI:

```sh
docker compose --profile runner run --rm --no-deps --entrypoint python vibearr-runner \
  -m vibearr_runner.delivery_recovery --state /var/lib/vibearr \
  --run-id ORIGINAL_RUN_UUID --expected-finish-digest ORIGINAL_FAILED_FINISH_DIGEST \
  --expected-publication-id ORIGINAL_PUBLICATION_UUID --expected-head-sha ORIGINAL_HEAD
```

This previews the retained local workspace and counted claim. Add `--execute` to
request the single audited recovery generation, then run the normal supervisor
with that same journal. The API requires the original failed completion to have
positive termination and reporting acknowledgement, the latest queue item,
current controller proof/configuration, an exact existing open PR, and no active
run or unresolved merge. It archives the prior failure, finish digest, original
acknowledgement, timestamps and consumed repairs before fencing a new claim token.
Old tokens and old finish acknowledgements cannot affect the resumed generation.
The original run ID, model session, context, paid operation IDs and task deadline
remain unchanged; the task counter is not incremented or reset. A missing local
workspace, private resource record or accounting checkpoint blocks recovery.

Delivery recovery cannot obtain coding credentials or dispatch an agent. A changed
base can consume the remaining ordinary repair allowance for a clean ancestry
update and fresh credential-free validation, then all required CI for that new
head. Each new revision has bounded validation/CI time inside the original task
deadline. If the base is unchanged, the original CI deadline still applies.
Conflicts or failed validation finish visibly without a model repair. Unknown
merge outcomes retain the existing claim/fence and read-only reconciliation.
A second recovery generation is rejected.

Recovery completion uses a separate `delivery-recovery:1:complete` marker. The
original failure comments and immutable recovery archive remain available. Run
readback and acceptance receipts include this archive; acceptance also requires
actual supervisor restarts around the final revision's validation operation and
its new final acknowledgement. Migration `0013_nativedeliveryrecovery` must be
applied before starting the updated API and supervisor. Older supervisors must
not own a recovered run: its API descriptor requires explicit recovery restore.
