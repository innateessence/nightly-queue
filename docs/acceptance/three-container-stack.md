# Three-container stack acceptance

This change implements [issue #142](https://github.com/innateessence/nightly-queue/issues/142).
The deployment is `app`, `postgres`, and `runner`. Codex 0.169.0, Claude Code
2.1.118 and OpenCode 1.19.5 are installed together. OpenCode requires an explicitly
configured external Ollama server and installed model. No model API keys, bundled
Ollama, automatic model downloads, or production deployment by task code are supported.

## Deterministic verification

Disposable Docker checks cover the application and portable suites, fixed trusted
candidate validation, offline package installation, actual runtime limits and
mounts, interrupted-operation reconciliation, deadlines, cancellation/replay,
cross-job cleanup, and denial of control/state/direct network access.

Operations checks capture desktop/mobile queue and connection forms, verify
static assets and browser errors, take a quiescent version-2 backup, and restore
into a separate empty Compose project. Database fingerprints must match before
identity fencing; business data must still match afterward. Restored processing
and old runner identities stay disabled. A fresh controller identity and healthy
three-service stack must start and pass Django system checks.

Run `bash deploy/docker/checks.sh full --candidate CHECKOUT --commit SHA` to
produce evidence bound to an exact revision. Uncommitted worktree runs are marked
as disposable snapshots. The check output includes source identities, immutable
image observations, qualification receipts, individual validation logs, browser
PNGs, restore results, and cleanup logs. Failed validation exports its evidence
before temporary state is removed.

Disposable connection-form captures:
[subscription on mobile](../screenshots/three-container-subscription-mobile.png)
and [external Ollama on desktop](../screenshots/three-container-external-ollama.png).
These show UI behavior with no saved account or model invocation; they are not
source-bound native coding acceptance.

## Required operator acceptance

These checks require actual operator accounts and infrastructure. Fixtures,
version probes, saved credential files and unit event transcripts cannot replace
them. Keep processing disabled until the selected deployment passes:

- Complete subscription login inside the runner for Codex and Claude Code.
  Verify persistence across recreation, refresh, reconnect, logout and explicit
  login/allowance failure handling with model API credentials absent.
- Complete one real authorized coding task per CLI in a designated disposable
  repository, including source changes, independent validation and GitHub
  delivery. Verify native session identity and retry/restart publication recovery.
- For OpenCode, use the operator's external Ollama URL and already installed
  model. Verify missing model/unreachable server responses and no cloud fallback.
- Drain and back up the actual existing installation, verify separate restore,
  preserve original keys/history/journals, and rehearse rollback with its retained
  recipe/images. Retire only explicitly identified obsolete services after
  replacement acceptance; retain rollback volumes until checked.
- Update required GitHub checks/merge profiles to **Application, runtime and
  recovery** after confirming the new workflow. No branch protection is changed
  by application migrations.

The implementation changes the deployment contract; it does not authorize
deleting production volumes, enabling processing, or claiming live account and
delivery acceptance from deterministic checks. Follow
[current operations](../docker-operations.md) for the executable procedures and
[runtime limits](../docker-only-runtime-plan.md#boundaries) for the reduced
isolation of a shared runner container.
