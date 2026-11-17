# Docker runtime checkpoint

These are intermediate D1–D3 results on the disposable `vibearr-docker-dev`
Compose project. They do not qualify production, enable autonomous execution,
or establish D4 delivery/merge acceptance. The supervisor was not started and
no paid model calls were made.

## Evidence

- `docker-foundation.json`: ten real Docker isolation/reset/control probes.
- `docker-worker-qualification.json`: eleven checks per worker, including actual
  PID exhaustion with API cancellation and forced-restart reconciliation.
- `docker-memory.json`: a separate 384 MiB disposable worker received a kernel
  OOM kill, confirmed descendant cleanup and successfully ran the next job.
  This tests the same worker implementation with an explicitly smaller profile;
  it does not claim that the 4096 MiB fleet slots were exhausted.
- `docker-runtime-inspection.json`: sanitized operator inspection of actual
  container images, mounts, privileges and resource ceilings. The inspection
  container received JSON facts over stdin, without engine access.
- `docker-validation.json`: all nine checks passed against the recorded exact
  disposable Git tree. Django discovered 2018 tests. Packaging also ran in an
  offline tools container, where all four distribution tests passed.
- `docker-dependency-validation.json`: all nine checks passed after adding
  `six==1.17.0` to candidate requirements. Setup produced a 189808640-byte,
  9953-file artifact. Setup and validation independently verified its digest and
  inventory. Provenance retains the original immutable validation image and
  binds the artifact to its source, run, requirements, setup receipt and boot.
- `docker-backup-restore.json`: a quiescent backup restored into a separate
  Compose project. All 32 table fingerprints matched before authority fencing;
  business data still matched afterward. The original key decrypted the saved
  synthetic credential, and queue/run history survived. The retired runner token
  was rejected by the restored API; a new identity authenticated with processing
  disabled. No publishing supervisor was started.
- `docker-browser.json` and `../screenshots/docker/`: actual Chromium desktop and
  mobile PNGs in both themes, with released static CSS and no page errors or
  failed requests. Desktop light and mobile dark output were visually inspected.
- `docker-drain-restore.json`: a later disposable operations run started a
  processing-disabled supervisor, acknowledged the current drain request, and
  stopped it before backup. Restore preserved the controller journal in its
  archive, rejected the old token and authenticated a fresh disabled identity.
  The observations record that run's images and uncommitted snapshot scope.

Image and tree identities in the reports identify their particular test
generations. Later review fixes receive their own unit tests and fresh boot
qualification. Final release acceptance must run the integrated D4 workflow
against the final images; earlier reports cannot be relabeled as that evidence.

## Fixed review findings

- Unfinished dependency transfers reserve their complete declared size, and
  repeated prepares remain usable when storage is full.
- A lost dispatch response reconciles the same operation. Unknown execution
  cannot create a replacement operation.
- Dependency publication rejects a changed fleet binding, and retention cannot
  recreate removed artifacts or remove dependencies during active validation.
- Task loopback allows local test/CLI RPC while denying trusted worker ports.
  Private networks, public destinations and metadata remain restricted.
- Repository and trusted Black checks now both select Python 3.12.
- Distribution resources include the worker Dockerfile.
- Existing PostgreSQL volumes cannot receive implicit replacement credentials.
  Credential publication is atomic and durable before database identity creation.
- Restore rejects nonempty targets, altered payloads, links, path traversal and
  insufficient disk capacity. It disables copied authority and requires explicit
  fresh-identity bootstrap, preserving the prior journal in a separate archive.
- Actual broker stop/start exposed missing entrypoint traversal and signal-forwarding
  capabilities. The corrected broker restarted and shut down with exit code zero.
- Tools commands load private credential files through their entrypoint. Bare
  container exec does not inherit secrets loaded into the running web process.
- New native receipts identify the protected slot instance and boot. Delivery
  binding and restart evidence use those identities rather than fabricated
  per-task container IDs. The focused API suite covers the complete receipt chain.

The operations checkpoint passed 976 application tests, 19 focused
initialization/filesystem/environment tests, Black, Ruff and mypy. Backup and
restore capacity checks require a 1 GiB reserve. Snapshot bundles stay in private
named volumes; only sanitized evidence and browser PNGs are in the repository.

This is an intermediate review, not the final no-P2-or-higher signoff. D4 still
requires the final integrated checks, pinned CI against the committed candidate,
native delivery/browser recovery, completion of documentation replacement and
real dependent two-issue acceptance.
