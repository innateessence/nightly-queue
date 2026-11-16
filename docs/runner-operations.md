# Runner operations and release verification

The operator UI remains a private-LAN, single-user interface. Browser writes use
CSRF protection. Runner bearer authentication and the private controller proof
stored in its volume are separate machine boundaries. Do not expose the browser
UI publicly as an authenticated multi-user service.

## Bootstrap and upgrades

1. Back up PostgreSQL, the credential encryption key, and the stopped runner
   volume. Keep the key and tokens outside Git and outside task workspaces.
2. Apply `python manage.py migrate` with Python 3.12. Provision a separate runner
   identity with `python manage.py provision_runner --help`; protect its token.
3. Configure backend, model, validation commands, issue-reading credential, and
   repository-delivery credential through Settings. Issue-import credentials do
   not implicitly authorize runner access. Explicit preflight credential grants
   remain necessary. GitHub permissions must cover issue/comment reads, repository
   contents writes, PR writes, and Actions run/artifact reads for qualification.
4. Build the independent runner image and start the Compose `runner` profile.
   Set only infrastructure bootstrap variables (`NQ_URL`, runner token, private
   state volume) outside the UI. The service has no database or encryption key.
5. Run the authenticated `--check` command. It reads configuration without binding
   new ownership or claiming work. Use the same private volume on every restart.
6. Keep execution disabled until the controlled live qualification below passes.
   A successful qualification still requires the separate **Enabled** setting.

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

`bash test.sh` runs portable Django/control/migration checks plus a real independent
runner image with a read-only root, non-root user, no capabilities, no network,
and private tmpfs state. Its standard-library, Git, HTTP, and deterministic
recovery checks do not require Git in the Django image. The actual namespace probe
must either succeed or produce the fail-closed unsupported result. The two actual
sandbox implementation checks and full OS isolation suite run separately; portable
success does not claim deployment qualification.

Dispatch `.github/workflows/runner-deployment-boundary.yml` on a designated
self-hosted runner labelled `runner-deployment-qualification`. Supply the reviewed
Compose overlay and immutable `repository@sha256:...` image. The job verifies the
configured image, runs the full boundary suite inside that exact deployment, and
uploads `runner-deployment-boundary/runner-boundary.json` only after success.
Unsupported namespaces fail the job; no skip, privileged mode, global sysctl
change, or unconfined policy is a substitute. Match the deployed source commit
and explicit image/policy digest bootstrap variables to the artifact. The artifact
binds source/code, kernel, image, policy, architecture, Python, and bubblewrap.

## Controlled live qualification

This branch implements a Codex Responses API structured-proposal candidate.
Native Codex CLI tools remain unsupported. Stored sessions do not establish API
authentication; unsupported quota blocks reserve enforcement. Claude and OpenCode
require their own adapter feature branches and transport-specific qualification.

A trusted operator must explicitly designate a test repository/project and
credential/model selections and authorize the required repository changes and paid
backend calls. No such designated live repository or paid-call authorization was
provided during this implementation. The hardened production container's namespace
probe is denied on this environment. Live authentication/integration and exact
production deployment acceptance remain blocked; execution is not qualified.

The concrete workflow, after those external prerequisites are satisfied:

1. Bind `NQ_RUNNER_IMAGE_DIGEST=sha256:...`,
   `NQ_RUNNER_SANDBOX_POLICY_DIGEST=<reviewed-overlay-sha256>`, and
   `NQ_RUNNER_SOURCE_REVISION=<source-commit>` in the actual deployment. Run
   `python -m coding_runner.release --probe` there. Unsupported namespaces stop it.
2. Complete the deployment-boundary workflow on that same image/source/policy.
   Download its `runner-boundary.json` artifact as data.
3. In Settings → Controlled release verification authorize the named test repository,
   runner, probe digests, and explicit live test scope for 24 hours. This grants no
   ordinary production claim permission.
4. Queue a designated success test task as the next item, with safe required
   validation and explicit human command approvals. Stop ordinary supervisor and
   use the same private journal with the following command, repeating it to advance
   safe steps and after completing each required approval:

   ```sh
   python -m coding_runner.release --qualification UUID --item-id ITEM_ID
   python -m coding_runner.release --qualification UUID
   ```

   The scoped claim checks the exact next item and authorized repository. Every
   real schedule, reserve, capacity, ownership, and approval gate still applies.
   Only missing release qualification is bypassed for that owned test lifecycle.
   The harness pauses after actual PR creation at `restart_required`. Restart the
   harness process with the same volume; a second real PR lookup verifies one
   unchanged PR, then only completion reporting resumes. Every invocation first
   stops recorded children and reconciles remote ownership. An uncertain model or
   workspace outcome requires operator reconciliation. Lost scoped claim responses
   retain their original UUID; retries that could create work recheck current gates.
5. While the controlled run is active, verify the successful boundary workflow and
   its downloaded artifact using the selected repository credential:

   ```sh
   python -m coding_runner.release --qualification UUID --boundary \
     --workflow-run-url https://github.com/OWNER/APP/actions/runs/RUN \
     --boundary-evidence /protected/runner-boundary.json
   ```

   Change pause, schedule, and reserve through Settings and advance the harness at
   each blocked state. Restore permissive authorized controls and observe a safe
   allowed checkpoint. Codex quota is unsupported, so restore reserve disabled to
   resume; never invent a supported quota reading.
6. After success reporting, run `--record-result` with the same qualification.
   Queue and explicitly execute a failing test task; preserve command/revision,
   exit code, protected log references, and useful failure diagnostics, then record
   its terminal result with `--record-result`. Do not manufacture a failure or PR.
7. Run `python -m coding_runner.release --qualification UUID --submit-evidence`.
   The server checks owned completed runs, saved context/plan/validated delivery,
   failure diagnostics, distinct restart observations, real provider response IDs
   for every account/model/version, UI control changes and runner gate history.
   It independently reads the GitHub Actions run and downloads its unexpired
   artifact, checking successful workflow, source revision, code/environment/image/
   policy binding, test execution, and artifact content. A URL alone is insufficient.
   Evidence reported by the authenticated supervisor remains the trusted boundary
   for PR and provider-response provenance, as in ordinary delivery.
8. Only a complete live report persists a seven-day qualification. Code, environment,
   models, credential versions, command/instruction policy changes, expiry, or
   revocation block readiness again. Operator schedule/pause/reserve and retention
   edits do not invalidate the qualification; their own gates remain effective.
   **Enabled** remains opt-in. Replay/test fixtures can never qualify live execution.

Screenshots in `docs/screenshots/runner-operations-*.png` show actual rendered
browser pages with clearly labelled synthetic fixtures; they do not show live
execution acceptance.
