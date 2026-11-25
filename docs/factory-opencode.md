# OpenCode factory roles

Issue [#164](https://github.com/innateessence/nightly-queue/issues/164) extends the
[factory plan](https://github.com/innateessence/nightly-queue/pull/146) using the
existing native OpenCode adapter and checksum-pinned CLI **1.19.5**. The selected
operator connection supplies one external Ollama origin and installed model;
the factory does not start Ollama, download a model or fall back to cloud billing.

Factory sessions select the common orchestrator's registered role profile and
bind the selected provider/model, operation, source and worker boot. Trusted
worker code sets `VIBEARR_FACTORY_ROLE`; the context must match it. Build keeps
task-scoped tools. Intake, specification, code/security/UX/UI reviewers and decision
use the pinned V1 `permission` mapping with default deny and read/list/glob/grep
allowed. These CLI permissions complement the worker's independently verified
source immutability; they cannot establish the OS boundary by themselves.

OpenCode runs without `--attach`, `--session` or `--continue`, in a fresh phase
HOME and XDG config/data/cache/state/runtime directories. Image-owned config fixes
the Ollama relay/model, disables external plugins/MCP and ignores candidate config.
Inherited `OPENCODE_*` variables are cleared, including permission, database and
shared-server overrides. Persistent controller, publication and login storage are
not task inputs. Remote compact-worker relay access uses the runner-issued
operation grant; its registered numeric relay is distinct from the external
operator-selected Ollama destination.

The parser requires stable session identities and typed events/parts/token usage.
It rejects malformed/truncated JSON, duplicate keys or terminal results, conflicting
errors and output after completion. Full final-step text is retained separately
from the 4,000-character display preview; the common factory adapter validates the
complete structured role result. Token counts are measured usage, not quota or
billed-cost claims. Actual exit code, protected log/export receipt and descendant
termination still decide whether the native operation succeeded.

## Verification and limits

`vibearr_runner.test_factory_opencode` covers stream, connection and configuration
failure cases alongside existing native/relay regression tests. The explicit
`vibearr_runner.acceptance.test_factory_opencode_cli` suite runs the actual pinned
binary offline: version/help and effective configuration for every role, including
candidate-plugin/model injection and inherited environment overrides. It needs a
Docker image containing the verified 1.19.5 artifact and runs with network disabled.
It makes no inference request and cannot qualify a selected external provider.

Current implementation review must additionally assemble the common factory
adapter/orchestrator and compact-worker role proofs before deployment. Actual
source-write denial, cancellation/reset of CLI/server/tool descendants and worker
network isolation are compact-worker acceptance, not results inferred from config.
The default noninteractive path in the
[pinned run command](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/cli/cmd/run.ts)
uses an in-process server; no shared external OpenCode service is required.
The [pinned permission schema](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/core/src/v1/config/permission.ts)
is the V1 mapping used here, not a guessed newer `permissions` schema.

Real OpenCode-only task execution against the selected external Ollama model and
the complete two-issue human-merge scenario remain separate live release evidence.
Fixtures and offline probes do not enable processing. A human reviews and merges
every resulting PR; this adapter cannot merge or enable auto-merge.
