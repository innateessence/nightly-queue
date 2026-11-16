# Feature 08 independent code review R1

Date: 2026-11-16. Implementation PR: #108 (`feat/native-claude-runner`).

Reviewed source: `11653dc25ba7abba64e96c846e6037095a9fb93b`.
Explicit stacked base: `7c1169d979e0fc2ac3bab6e1ac04574a07327555` (feature 04).
Scope: the entire 20-file feature 08 diff against that base, not the inherited
feature 04 implementation compared with main. This review used a fresh full
clone at `/tmp/vibearr-review08-r1`; it did not use an author worktree.

## Verdict

No feature 08 P0, P1, or P2 code defect found in the reviewed source.
This is a code-stage verdict, not main-integration or release acceptance.
The feature must not merge to main on this report alone.

Feature 04 is still under separate review/fix and feature 06 is not integrated.
Normal-merge their accepted main versions, retarget the implementation PR to
main, and review/test the resulting exact head before merging. The separately
tracked inherited feature 04 Git launch/pause barrier issue is not reclassified
as a new feature 08 finding here.

No usable selected Anthropic API credential was available to this review.
Actual selected-account authentication, native edit, restart, cancellation,
and a useful issue-to-PR run with all nine independent validation checks remain
required. Codex evidence and fixture results do not satisfy those Claude rows.
No paid provider request, host-auth inspection, token refresh, queue activation,
live service control, or implementation merge occurred during this review.

## Code assessment

- Read `AGENTS.md`, feature 08 in `docs/autonomous-ai-queue-plan.md`, the native
  lifecycle and acceptance requirements in `docs/vibearr-mvp.md`, PR #108's body,
  and the changed setup/evidence documents. The implementation and documents
  accurately distinguish native execution from the historical Messages proposal
  transport and distinguish offline observations from live acceptance.
- `native_sessions.py` adds explicit Claude registry selection. The new adapter
  and shared `native_adapter.py` freeze provider/model/credential ID and version,
  auth kind, image, qualified runtime binding, profile, source/context digests,
  operation identity, deadline and resource limits. Profiles fix argv, the selected
  secret slot and Anthropic API egress. Reconciliation and cancellation reuse the
  persisted operation; they do not select a latest transcript or another provider.
- Reviewed both SDK calls and owning native lifecycle/control-plane paths.
  Protected executor state, descendant termination, matching run/operation IDs,
  log digest and verified source export participate in outcome handling. Auth
  proof requires the fixed unchanged probe source/context and protected completion
  time, binds the selected account/model/profile/image/runtime, expires in at most
  15 minutes, and cannot be refreshed by replay. Configured key presence alone
  leaves `authenticated` and `native_agent_ready` false.
- `ClaudeEvents` bounds bytes, lines and events, requires stable canonical UUID
  identity, validates pinned version/model and clean init metadata, rejects
  unconfigured tools and post-terminal output, and uses terminal aggregate token
  usage rather than counting repeated assistant blocks. Success additionally
  requires `is_error=false`, no permission denials, protected exit 0 and no
  timeout/cancel/log truncation. A real missing-auth `subtype=success` is not
  mistaken for successful work. Partial progress records remain pending until
  finalization. Quota allowance is explicitly unsupported.
- `claude_task.py` supports only a selected dedicated API key. It rejects OAuth
  setup tokens and inherited/custom authentication, creates fresh private config,
  passes the prompt through stdin, checks the installed CLI version and rebuilds
  the environment from an allowlist. The pinned invocation uses bare/safe mode,
  disables skills/slash commands, selects empty setting sources and strict empty
  MCP configuration, and has noninteractive coding-tool permissions. The API key
  is task-visible by design; no claim is made that task tools cannot read it.
  The image/OCI boundary and independent credential-free validation remain the
  enforcement boundary, with Git/PR credentials owned by the supervisor.
- The Dockerfile pins the Python base, CLI version and official architecture
  artifact SHA512 values, extracts only a regular binary and verifies its version.
  Installed executor resources and canonical/legacy runner imports are included
  in real built artifacts. Imports keep the optional executor dependency lazy.
- `claude_acceptance.py` uses a separate empty state directory, preserves existing
  evidence, independently tests the exported fixture, reconciles the same
  container across restart/duplicate start, waits for foreground tool activity
  before cancellation and checks descendant termination. Its report explicitly
  excludes issue-to-PR and full-project validation. This driver was inspected,
  not run with a credential.
- Verified that `vibearr_runner/native_codex.py`, `vibearr_executor/codex_task.py`
  and `vibearr_executor/Codex.Dockerfile` have no diff from the explicit base.
  The common fixture conformance suite also exercises unchanged Codex behavior.

## Independent verification

All commands used Python 3.12.12. Application/static tools came from the shared
read-only `/tmp/vibearr-tools` environment. PostgreSQL tests used only the owned
`vibearr_review08_r1` database at `127.0.0.1:32771`, via an external test settings
module naming that same database. No application/executor/bootstrap service or
foreign database was controlled.

| Check | Result |
| --- | --- |
| Full `manage.py test --keepdb --noinput` with owned PostgreSQL settings | 1,829 tests in 64.066s; OK, four distribution tests skipped because the discovery environment intentionally lacks build tools |
| `python -m vibearr_runner.verify` | 866 tests in 67.548s; OK |
| `python -m unittest coding_runner.test_native_claude coding_runner.test_native_claude_lifecycle vibearr_executor.test_claude_task -v` | 76 tests; OK |
| `python -m unittest vibearr.test_distributions -v` in owned build venv | All four real sdist/wheel/install tests ran sequentially and passed in 71.706s; setuptools 80.9.0, wheel 0.48.0 |
| `black --check .` | 387 files unchanged |
| `ruff check .` | Passed |
| `mypy . --config-file mypy.ini` | Passed; 387 source files |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `manage.py check` | No issues |
| Owned Claude Dockerfile build | Passed; image `sha256:91e53af784dc1968ce4b94418635d9f6c1f3de9cb7ad4fa5eb084b8971e467ce` |
| Actual CLI `--version` / `--help` | Exit 0; version 2.1.325; launch flags present |
| Actual CLI missing auth, fixed reviewed flags | Exit 1; terminal `subtype=success`, `is_error=true`; parser completed and failed as required |

The owned build used tag `vibearr-review08-r1:2.1.325`, image label
`io.vibearr.review=feature08-r1`, and an isolated Docker CLI configuration.
Offline probes used `--network none`, read-only root, unprivileged UID/GID,
no capabilities, no-new-privileges, bounded CPU/memory/PIDs and disposable tmpfs
HOME/work directories, without a credential or host mounts. These are CLI
observations, not deployment boundary qualification or live model acceptance.

The actual init had Bash/Edit/Read, empty MCP servers, skills and slash commands,
and only pinned image-bundled agents-md/privacy/plugin-authoring plugins with
strict `builtin` path/source identities. This agrees with the documented
limitations of bare mode; it is not a claim that bundled plugins disappear.

Offline hashes:

- Wrapper SHA256: `55f98fade96867060319b4416d5147b38bf5d7b680d435b167c2ec1d24e74274`.
- Version stdout: `52c422d6abf19c096c57fff7414eab62ee955dbd98a90df251a77ba89a8da532`.
- Help stdout: `1e734dc647e2d7c775d2e581b3aa68a8741ff4fdde7e864c4e5799d70777558c`.
- Missing-auth stdout: `e689065bc083c3cf92a27ea9b2a60bb9207a13b56c5031c188ec0223a1a39d8a`.

Local proof paths: `/tmp/vibearr-review08-r1-{django,portable,focused,artifacts,black,ruff,mypy,image}.log`
and `/tmp/vibearr-review08-r1-offline/report.json` plus its stdout/stderr files.
They contain no selected secret. The implementation's independently inspected
redacted observation is `docs/evidence/native-claude-offline.json`.

At report preparation the reviewed implementation SHA had green `Run test.sh`,
`Qualify OCI executor`, and `Check native Codex image`; `Validate candidate` was
still running. CI completion and exact-head re-review after integration remain
separate requirements. This report does not authorize merging implementation PR
#108 into its temporary feature 04 base.
