# Native OpenCode implementation checks

Recorded 2026-11-16. This is implementation and offline CLI evidence, not live
provider or issue-to-PR qualification. The feature is stacked on published feature04
source `7c1169d979e0fc2ac3bab6e1ac04574a07327555`; accepted feature04+06 main
integration and a fresh independent full review remain required before merge.

The owned image `sha256:4d85f8c12bbca3788706dbaa7b4e875e86f97b88d259ee2f640739e19a88edcc`
was built from `vibearr_executor/OpenCode.Dockerfile`. It uses OpenCode 1.19.5,
whose official x64 baseline archive SHA-256 is
`17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d`.
The image was used only for an owned offline CLI inspection container, with no
network, a read-only root, UID 1000, dropped capabilities, no-new-privileges and
private temporary home. The container was removed afterward.

`python -I -m vibearr_executor.opencode_smoke` verified the real pinned binary's
version, programmatic flags and effective trusted configuration. It also invoked
the actual wrapper with empty selected material and observed exit78 before CLI
execution. Its actual result was:

```json
{
  "pinned_version": "1.19.5",
  "offline_cli_configuration": true,
  "missing_auth_rejected_before_cli": true,
  "authenticated": false,
  "native_task_acceptance": false
}
```

Local checks on the integrated stacked source passed:

- PostgreSQL application/discovery suite: 1812 tests, four distribution tests
  skipped there and run separately.
- Explicit portable runner and Git acceptance: 851 tests.
- All four actual distribution sdist/wheel tests, run separately with
  setuptools80.9.0 and wheel0.48.0, including runner-only installations without
  the executor extra and installed OpenCode resources.
- Focused native OpenCode/shared conformance/wrapper and unchanged Codex
  regression cases: 99 tests. These use fixtures and establish no live access.
- Black, Ruff, mypy, migration drift and Django system checks.

The selected native route accepts only an explicit saved OpenCode connection for
OpenAI API-key authentication and a matching full `openai/model` ID. No usable
selected connection is currently evidenced. No host login/secret was searched,
copied or refreshed, no provider/model request was made, and no local Ollama/model
proxy or live queue service was touched.

These rows remain pending: real provider authentication proof, successful native
model/tool execution, native cancellation/descendant proof, actual supervisor/
server restart with that provider, and one useful real issue-to-PR run with all
nine independent checks and exact tested/pushed head. The bootstrap acceptance
driver's normal controller and `collect_run` evidence collector should supply the
real issue proof after accepted feature06 integration and an explicitly approved
finite scenario. Fixture events and this offline report cannot satisfy those rows.
