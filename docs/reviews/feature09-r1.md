# Feature 09 independent code review R1

Reviewed 2026-11-16 in the independent full clone
`/tmp/vibearr-review09-r1`, on `review/feature09-r1`.

## Exact scope and decision

Reviewed source: `cec98a96ba502497eb8f2ead0be8ca8da3a365a3`, PR #109,
`feat/native-opencode-runner`. The explicit stacked comparison base is
`7c1169d979e0fc2ac3bab6e1ac04574a07327555` (feature04), not main. I reviewed
the entire resulting 16-file diff, including the shared adapter introduced by
`32284d3c46df6ba722977ca1889815ed83bb0059` and its partial-log correction.

**No new P0, P1, or P2 code defect found in this feature09 diff.** This is a
code-stage decision only; it does not clear feature09 for merge or claim a usable
authenticated integration. I made no candidate source changes.

The feature04 push-dispatch fence finding is an upstream finding already owned
by feature04 and ROOT. This review neither reclassifies it as feature09 nor
accepts its pending repair. Accepted feature04 and feature06 must be normally
merged from main into feature09, followed by fresh review and checks of the exact
integrated head before a merge decision.

## Review performed

Read AGENTS.md, feature09's plan requirements, the MVP and execution-policy
contracts, PR #109's body, native OpenCode documentation, and the offline
acceptance report. Traced the new registry entry through existing preflight,
native lifecycle, immutable session, receipt, export, validation and delivery
interfaces. Inspected all new provider and shared conformance tests, wrapper
tests, compatibility modules and distribution changes.

The selected route requires an explicit OpenCode connection containing an OpenAI
API key, an official fixed endpoint and full `openai/model` selection. Unsupported
providers, OAuth/session kinds, changed models and additional launch fields are
rejected. Credential identity/version, account, source, context, runtime binding,
profile, image, operation and deadline are frozen. Reconciliation uses the same
operation without a new prompt, and uncertain execution cannot produce success.

The wrapper starts in its private HOME and passes `--dir /work`, uses the pinned
CLI's native in-process server route, private XDG/auth/database/config paths and
the selected provider material. The trusted configuration excludes ambient
provider credentials, project configuration, external/default plugins, external
skills, sharing, LSP and formatters. Fixed permissions permit coding tools and
deny unspecified operations. Egress remains `api.openai.com` only; no Ollama,
model proxy, host bridge or GitHub credential is introduced.

Reviewed bounded native JSONL parsing against the pinned implementation: session
and part identities, partial records, duplicate parts, tool-call intermediate
steps, terminal stop, provider errors and token units. Protected successful exit,
nontruncated matching logs, descendant termination and verified export remain
necessary for a successful outcome. Auth readiness requires a short-lived probe
bound to the actual selected credential/model/runtime and unchanged fixed source;
configured material alone reports readiness false.

The Codex adapter and Codex task wrapper have no diff from the stacked base.
Executor imports remain lazy in the installed runner; the actual bare runner
artifact checks below exercised the optional SDK boundary.

## Independent verification

All checks used the reviewed source and Python 3.12. Application dependencies
came from read-only `/tmp/vibearr-tools`; distribution checks used a separate
owned venv with setuptools 80.9.0 and wheel 0.48.0. Only the reviewer's PostgreSQL
database `vibearr_review09_r1` and its Django-created test database were used.

| Check | Result |
| --- | --- |
| `python manage.py test --noinput`, PostgreSQL | 1,812 passed, four artifact tests skipped and separately run |
| `python -m vibearr_runner.verify` | 851 portable runner/Git acceptance tests passed |
| `python -m unittest coding_runner.test_native_opencode vibearr_executor.test_opencode_task coding_runner.test_native_codex` | 99 passed |
| `python -m unittest vibearr.test_distributions`, owned build-tools venv | All four actual sdist/wheel checks passed |
| `black --check .`, `ruff check .`, `mypy . --config-file mypy.ini` | Passed |
| `python manage.py makemigrations --check --dry-run`, `python manage.py check` | Passed |

Built an independently tagged/labeled image from the exact candidate recipe:
`sha256:a4662f1daa0bb07dc36ece810457fedcc8d07cc2ad85e659335d95fb99f35a10`.
The first build invocation could not write the read-only default Docker client
activity path; the successful build used owned `/tmp` Docker configuration.
No host configuration was changed.

Ran its actual `python -I -m vibearr_executor.opencode_smoke` in an owned,
automatically removed container with network disabled, read-only root, UID 1000,
dropped capabilities, no-new-privileges, private tmpfs HOME/tmp and finite
CPU/memory/PID ceilings. The real binary version/help/effective configuration
and missing-selected-auth wrapper checks passed. Actual result:

```json
{"pinned_version":"1.19.5","offline_cli_configuration":true,"missing_auth_rejected_before_cli":true,"authenticated":false,"native_task_acceptance":false}
```

Checked upstream source at tag commit
`810c0eba1a7348036f8bdf61e6632d0c36c6876c`, including native run/events, auth,
configuration discovery, plugins and permissions. The official release API
confirms the recipe's Linux x64-baseline digest
`17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d` and ARM64
digest `fae5be262d693b7c5e562856cc473e59e741772936db0c0600ac0d5ed5bd088b`.
The preparation reference `588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a`
belongs to the separate ordinary x64 archive; the recipe deliberately uses the
official baseline asset. No V2 `--standalone` behavior was assumed.

Primary references: [release](https://github.com/anomalyco/opencode/releases/tag/v1.19.5),
[native run](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/cli/cmd/run.ts),
[auth](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/auth/index.ts).
Local command logs and downloaded inspection-only upstream source are in
`/tmp/vibearr-review09-r1-evidence/`.

## Gates still open

There is no evidenced usable selected OpenCode provider key. No authenticated
provider call, paid model run, genuine native tool/edit/test, real provider
cancellation/descendant proof, provider restart/reconnection, or useful real
issue-to-PR acceptance was performed by this reviewer. No host login was read,
refreshed or changed. Existing Codex ChatGPT authentication is not OpenCode proof.

Those live acceptance requirements, accepted feature04+06 main integration,
fresh integrated-head review, ROOT's review and all four exact-head required CI
checks remain mandatory. ROOT alone may make the eventual serial squash-merge
decision. This report cannot substitute offline fixtures for any live gate.
