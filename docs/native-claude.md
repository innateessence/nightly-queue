# Native Claude Code sessions

The optional `claude` native backend runs **Claude Code 2.1.325** in the same
registered OCI boundary, immutable session lifecycle, independent validation
and supervisor-owned Git/PR delivery as Codex. It does not use the historical
Messages API proposal adapter, human approvals or a permission broker.

The implementation has portable conformance tests and actual offline CLI
version/help/missing-auth observations. **Live Claude authentication, editing,
cancellation, restart and issue-to-PR acceptance remain unproven until a selected
API key is supplied and those scenarios pass.** A fixture response or configured
key never establishes live readiness. Runtime qualification and account
authentication diagnostics remain separate from a real delivered project change.

## Image and configuration

Build the reviewed image and record its immutable ID:

```sh
docker build -f vibearr_executor/Claude.Dockerfile -t vibearr-claude:2.1.325 .
docker image inspect vibearr-claude:2.1.325 --format '{{.Id}}'
```

The recipe pins the Python 3.12.12 Debian base, Git and architecture-specific
official Claude native package integrity. It verifies SHA512 before extracting
only the regular binary; no npm lifecycle or downloaded installer runs. It
checks the actual CLI version. The native binary needs no Node/SDK dependency.
Register the resulting immutable image, not a mutable tag. Changes to installed
executor code, images or profiles require actual boundary qualification again.

This image contains the CLI, Git and Python, but not this app's requirements.
For self-development, build a reviewed derived image without model/GitHub
credentials, include the app dependencies, and register a disposable PostgreSQL
sidecar through `claude_profile(image, 'token', database_image=...)`. Candidate
Dockerfiles and settings cannot choose the image or test profile. The agent
uses only model egress; setup/dependency builds and all 9 independent validation
checks remain separate credential-free operations.

Create the profile with
`vibearr_runner.native_claude.claude_profile(image, 'token')`; its default ID is
`claude-token-v1`. The helper fixes argv, API host, selected-secret slot and
resource ceilings. Register it alongside the trusted validation profile using
the [executor deployment procedure](isolated-executor.md). Set the selected
project's native `execution_policy.runtime_profile` to this ID, keep its trusted
validation profile, and permit `api.anthropic.com` in policy network hosts.

In global settings or project overrides, choose implementation backend `claude`,
an explicit available API model ID and the dedicated saved Claude token
credential. No Compose model/token variable or per-run approval is needed.
Research-stage selections are inactive for native work. Existing configuration,
context and run views preserve the provider/model/account selection. A changed
current selection stops a running attempt; recovery never switches its frozen
backend or borrows another account. Unsupported model/provider/auth errors fail
with a visible reason. Normal execution does not fall back to Messages API.

## Authentication and clean invocation

Only a deliberately selected dedicated Anthropic API key is supported. The
scoped preflight grant or owned-run implementation grant supplies that key to a
private per-operation executor secret. The fixed wrapper passes it as
`ANTHROPIC_API_KEY` to the CLI. No host login, OAuth/setup-token, refresh token,
cloud-provider credential, custom gateway, auth helper or personal config is
discovered or inherited. A Claude subscription alone does not satisfy this
integration; bare mode ignores its login credentials.

The wrapper creates a fresh 0700 HOME/config directory outside the exported
workspace and a 0600 prompt file read through stdin. It allows only fixed locale,
executor proxy and disposable database environment fields; queue/GitHub tokens,
host database values, encryption keys, shell startup variables and alternate
Anthropic/provider endpoints are discarded. The image has no operator-managed
settings or mounted home. The CLI never receives supervisor Git credentials.

The exact-version launch uses `--bare`, `--safe-mode`,
`--disable-slash-commands`, empty user/project/local setting sources, strict
empty MCP configuration and `--no-session-persistence`. Tools are explicitly
limited to coding/file operations; the pinned CLI's bare mode exposes Bash,
Read and Edit. Native tool permissions use `dontAsk`, an explicit allowlist and
`--permission-prompts none`. Denied operations fail without waiting for a person.
There is no automatic classifier, Agent, web tool, remote connector or MCP
permission service. Repository instructions remain task data and cannot expand
the executor policy. Frozen context supplies coding conventions explicitly;
CLAUDE.md and project CLI settings are not auto-loaded.

Actual 2.1.325 help is narrower than some online bare-mode descriptions:
image-bundled features remain active. The pinned probes exposed four bundled plugin identities in init
metadata: `cc-plugin-agents-md`, `cc-plugin-telemetry`, `cc-plugin-privacy` and
`cc-plugin-plugin-authoring`, all with path/source `builtin`/`<name>@builtin`.
These are part of the pinned binary, not inherited project/user plugins. With
nonessential traffic disabled, the final offline probe omits the telemetry
plugin. The
parser permits only those exact bundled identities; installed/external plugin,
MCP, skill or command discovery fails the initialization check. Updates and
nonessential traffic are disabled; the executor allows only the API host.
CLI flags complement the actual OCI filesystem, process and network boundary.

The key is available to the CLI and its own tools. This implementation does not
claim to hide it from code executing in that task. Use a dedicated account/key
and provider-side spending controls. Executor logs redact selected material and
exports containing known raw/structured/escaped secrets are rejected; arbitrary
encoded leakage cannot be guaranteed absent. The key is removed when the task
stops and never goes into the prompt, manifest, session journal or Git checkout.

## Session and evidence contract

`NativeBackendRegistry().create('claude', executor)` returns NativeClaudeAdapter.
It supports prepare/start/inspect/events/outcome/cancel/reconcile and bounded
authentication diagnostics through the existing NativeAdapter contract.
New-provider shared OCI plumbing is in `native_adapter.py`; Codex's installed
implementation remains unchanged.

Journal the operation, frozen inputs and deadline before prepare; persist the
complete NativeSession before start. The native lifecycle also records the
server dispatch intent before any paid invocation. The session binds provider,
CLI version, credential ID/version, auth kind, model, exact manifest,
profile/image and qualified runtime. Restore that same record after restart.
Lost responses reconcile the same operation/container. Unknown states keep their
fence and cannot start a replacement. Stop must prove full descendant
termination; pause/resume keeps the container and original host deadline.

Cross-container Claude transcript resume is unsupported. No latest-session
lookup or `--continue` is used. Supervisor/executor restart recovery reads the
existing process and protected event log without another CLI invocation.
Repair sessions start only after confirmed prior termination and use the
existing aggregate time limits and maximum two repairs.

The bounded JSONL parser checks init provider version/model/tool/extension
metadata and stable session identity. A successful outcome requires a terminal
success result, `is_error=false`, no permission denials, protected exit 0, no
timeout/cancel/truncation, verified logs and source export, and terminated
descendants. A real missing-auth result can say subtype `success` while also
setting `is_error=true`; subtype alone is insufficient. CLI text is progress,
not trusted validation/delivery evidence. All project checks run against the
exact committed candidate independently before any push/PR.

Usage comes once from the terminal aggregate: input/output/cache-read/cache-
creation counts with unit `tokens`. Repeated assistant blocks are not added
again. No remaining account allowance is implemented (`quota_supported=false`);
enabled reserves block when telemetry is unavailable. Client-estimated costs,
token counts and rate-limit events are never remaining percentages or a dollar
cap. Session/model errors do not trigger provider switching.

A fixed greeting-only probe can produce an auth proof bound to the exact
selected credential/version/model/CLI/profile/image/runtime. Its maximum 15min
lifetime starts at protected completion time and cannot be renewed by replay.
The private preflight cache invalidates changed bindings or actual auth failure,
backs off finite failures, and does not probe empty/idle projects every poll.
Configured material or `claude auth status` alone cannot mint this proof.

## Verification and real acceptance

Portable tests run the same native conformance cases for Codex and Claude:

```sh
python -m unittest coding_runner.test_native_claude vibearr_executor.test_claude_task
python manage.py test runner_control.test_native_claude
```

They cover frozen selection, immutable manifests, missing auth, leakage bounds,
lost starts, unknown fencing, restart, stop, receipt/export integrity, auth proof
bindings, malformed/truncated events, denied tools and clean invocation. Fixture
executables and events remain fixtures. Distribution tests verify the image
recipe, wrapper and canonical/compatibility imports from installed artifacts.

The real fixture driver requires an explicitly selected private API-key file and
an owned disposable state root, never an active executor's state:

```sh
python -m vibearr_runner.claude_acceptance \
  --image sha256:<claude-image-id> \
  --database-image sha256:<postgres-image-id> \
  --state /var/lib/vibearr/claude-acceptance \
  --credential-file /run/secrets/selected-claude-api-key \
  --model <selected-claude-api-model-id> \
  --evidence /var/lib/vibearr/claude-acceptance.json
```

This makes three bounded real native calls: edit/test with executor restart and
duplicate-start reconciliation, cancellation during foreground tool activity,
and selected-account auth proof. It verifies the export using an independent
offline test and checks project hook/MCP markers stayed inert. It fails on
missing evidence. The fixture report explicitly says it does not prove the full
project validation or issue-to-PR scenario. Use the accepted bootstrap scenario
driver and this project's selected Claude backend for that separate useful
issue, all 9 exact-commit checks and one real PR. Root reviews the exact task and
selected account before any live scenario; fixture code must not invent auth or
silently start a global worker.

The [offline observation report](evidence/native-claude-offline.json) records the
exact image, wrapper digest, flags, CLI metadata and honestly unrun live rows.
It is not executor qualification or real model acceptance.

Primary references checked 2026-11-16:
[CLI](https://code.claude.com/docs/en/cli-reference.md),
[programmatic execution](https://code.claude.com/docs/en/headless.md),
[authentication](https://code.claude.com/docs/en/authentication.md),
[permissions](https://code.claude.com/docs/en/permission-modes.md),
[network](https://code.claude.com/docs/en/network-config.md),
[event schema](https://code.claude.com/docs/en/agent-sdk/typescript.md),
[usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md), and
[2.1.325 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.325).
