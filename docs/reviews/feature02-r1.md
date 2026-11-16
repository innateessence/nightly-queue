# Feature 02 independent review R1

Reviewed PR #58 at exact head `8376377ea6330242408df17dfafeebd676fa6ac5`,
against accepted main `ff58646`, on 2026-11-16. The feature branch still pointed
at that head after validation. Review used the independent full clone
`/tmp/vibearr-review02-r1`; implementation files were not modified.

No P2 or higher findings remain from this review of that exact head. This is a
bounded code and behavior review, not a native coding-backend or MVP release
acceptance claim.

## Scope and conclusions

Read the feature 02 and runtime contracts in `docs/vibearr-mvp.md` and
`docs/isolated-executor.md`, the new executor service/client/contracts, engine
adapter, worker, firewall/proxy, artifacts, qualification/recipes/tests, runner
runtime diagnostics and verifier, backend credential qualification change,
configuration contract, package metadata, Dockerfiles, systemd example, and CI.

The control protocol fixes registered immutable profiles and rejects command,
mount, image, resource, and egress expansion in manifests. Authenticated bounded
requests are separate from candidate source; source archives and returned full
inventories are validated before extraction/import. Protected init/readback use
isolated Python imports. Tasks cannot supply trusted receipts through stdout.

Launch intent, operation identity, original container identity, and launch-gate
intent are journaled. Retry paths preserve the same task container, and unknown
operations retain both the run and same-run operation fence. Container ID
replacement is rejected. Temporary PID starvation does not clear running identity
or reopen a confirmed launch gate. Stop confirms removal before exposing complete
termination. Result readback verifies protected logs and source before cleanup.

Selected backend credentials are private input files, erased before opening the
launch gate, absent from Docker task environment configuration, redacted from
bounded logs/result strings, and rejected from source exports when present as raw
values. Task-side CLI tools retain their own selected key as the documented
residual exposure. Validation/setup cannot receive that credential. Each database
has fresh credentials, tmpfs data, a separate internal operation network, and no
published port.

Runtime diagnostics use authenticated installed executor capabilities and keep
`runtime_ready` distinct from `native_agent_ready`. Native account preflight does
not consume historical proposal release proof. Native lifecycle/adapter work is
intentionally unavailable pending features 03/04.

## Independent validation

- Executor unit/API tests: `python -m unittest discover -s vibearr_executor -t . -v`,
  **55 passed** using Python 3.12.
- Native runtime diagnostic/preflight tests:
  `python -m unittest coding_runner.test_native_runtime -v`, **7 passed**.
- Built and installed real standalone executor and runner wheels into the fresh
  `/tmp/vibearr-review02-r1-installed` venv, with no runtime dependencies or Django.
  From `/tmp`, isolated Python ran `vibearr_runner.verify` successfully:
  **520 passed**. The installed package intentionally omits the optional source
  `coding_runner/acceptance` directory; this count is not the checkout suite count.
- Built the reviewed Dockerfile with a private writable Docker configuration into
  `sha256:69ed0d3d52fdb1784df6f211fb53dc87ca21ef64bb50792d4ebf2d934a18d55d`.
  Used installed PostgreSQL image
  `sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
- From `/tmp`, ran the installed runner's actual `--boundary` qualification against
  those immutable images with private state `/tmp/vibearr-review02-r1-state`.
  **All ten required checks passed** on Docker 29.9.3/API 1.56, rootless,
  overlay2/extfs, cgroup v2, kernel `7.3.4-1-cachyos`.
- Actual probes exercised credential-free pip installation; unauthenticated model
  HTTPS reachability; denied direct gateway/LAN/metadata/DNS/control and IPv6
  routes; proxy host filtering; read-only root/protected state; hostile Python
  shadow files and forged stdout receipts; ENOSPC, fork EAGAIN, OOM and CPU
  throttling; selected-key log redaction and source-export denial; service restart
  retaining the original container; detached descendant cancellation; complete
  32-slot PID occupancy followed by same-operation retry, cancellation and verified
  workspace export; and two fresh SCRAM-authenticated PostgreSQL sidecars with
  cross-operation access denied. The resource recipe exited 0 while its allocating
  child was actually OOM-killed; protected cgroup evidence was asserted.
- After qualification, started the installed service again and exercised real
  installed `runtime_diagnostics` through its client. It returned
  `runtime_ready: true`, `native_agent_ready: false`, without importing Django.
- Independently launched a disposable sleep/descendant task with a 15-second
  manifest deadline. It retained the original container identity, exited with
  actual code -9 and `timed_out: true`, confirmed descendant termination, and
  returned a verified source export.

The full boundary report remains at
`/tmp/vibearr-review02-r1-boundary.json`, SHA-256
`a64dff39ac2c0f170c717c195430de373f9e65317aa857373d2f29b1eba54549`.
Its installed executor source digest is
`107c33f947d9d6ad9df8fcee7ad2f23cf1b056c061009df9d478d7093121fc0c`.
The additional installed diagnostic/deadline result is
`/tmp/vibearr-review02-r1-installed-check.json`. Qualification cleaned up its own
operation resources; the additional service was shut down after its task exited.
No live stack, host service on port 7777, other user's containers, or production
database was changed.

## Limits

This review qualified one freshly built task image with the fixed smoke profiles.
Additional operator task images/profiles require their own qualification. DNS
rebinding and replacement-ID/integrity edge cases were checked in source and unit
tests; qualification's private DNS answer rejection uses a controlled resolver
fixture. No native coding CLI/authentication, paid backend call, queue claim,
delivery/merge, production deployment, or full Django test suite was performed by
this reviewer. Later changes to the feature head require a fresh review.
