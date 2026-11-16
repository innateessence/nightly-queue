# Codex model API transport and research

This branch implements a candidate **Codex model API transport**, not a working
native Codex CLI integration. The controller still cannot claim or execute work.
`CodexBackend.readiness()` reports live authentication and acceptance as
unverified, even when the selected credential has a supported local shape.
Research rejects that readiness in production. Fake transports establish the
supervisor contracts without claiming external acceptance.

## Tool and authentication probe

The read-only local probe used the installed `codex-cli 0.169.0`. The adapter pins
that version in `PINNED_CODEX_VERSION`; `probe_codex(binary)` checks it without
loading an account or contacting a model. The observed result was:

```json
{"native_tools_supported": false, "pinned_version": "0.169.0", "ready": false, "reason": "Native tool mediation has not been verified.", "version_matches": true}
```

The local `codex exec --help` exposes JSON output, an output schema, isolated
configuration, and read-only sandbox settings. None proves that every native
tool becomes a supervisor approval proposal. Native CLI execution, cached
sessions, plugins, MCP, shell tools, and automatic approvals are unsupported.
The image deliberately does not install an unused CLI. Python is pinned by the
existing runner image (`3.12.12` plus its image digest); the transport uses only
its standard library.

Official authentication documentation describes API key and ChatGPT
authentication separately, including different billing. This adapter accepts
only a web-selected `codex` credential of kind `token`. It does not convert a
ChatGPT session into an API key or fall back to local login state. Model and
instructions come from the frozen effective stage settings; model IDs are not
hard-coded or inferred from a CLI login. Credential retrieval uses the selected
preflight grant or the owned run's stage purpose, then verifies the returned
provider, credential ID and kind.

The model worker posts to the fixed `https://api.openai.com/v1/responses`
endpoint. Its request has no tools, sets `tool_choice` to `none`, disables stored
responses, and requests one bounded response. Every returned native tool item
is rejected. These options follow the official Responses API contract; no SDK
or undocumented Codex endpoint is assumed. The worker runs isolated Python
outside the checkout with an empty environment and closed inherited file
descriptors. Its anonymous stdin carries the API key; command arguments,
environment, prompts, progress, and captured stderr do not. Only supervisor
code runs in this worker. The model receives data and cannot start a local
process. Agent command sandboxes receive no worker files, credentials, host
process namespace, or supervisor environment.

Sources checked for the integration design:

- [Official authentication documentation](https://learn.chatgpt.com/docs/auth)
- [Official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Official Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)

No supported remaining account allowance API was established. `read_usage`
returns `UsageReading(supported=False)` for every account; response token usage
is never relabeled as remaining quota. Account identity lives in the gate's
adapter key and evidence. Supported future readings must supply every window's
unit, remaining percentage, observation time and reset time using the existing
`UsageReading`/`UsageWindow` contract. The controller registers distinct adapter
instances for the complete configured account inventory. Reserve enabled with
any unsupported account blocks the entire queue, including other projects.

## Shared backend contract

`coding_runner.backends.Backend` defines readiness, capabilities, research,
implementation, usage, progress callbacks, and interruption. Structural
capabilities describe the transport; `BackendReadiness.ready` and
`authenticated` describe verified live readiness and remain false here.
`BackendRegistry.stage(snapshot, stage, scoped_credentials)` selects a backend
and a `BackendRequest` from frozen settings. Later adapters register factories
by provider without changing the queue, CLI, or research stage.

Requests return one `BackendEvent(kind, data)`:

| Kind | Data | Research | Implementation |
| --- | --- | --- | --- |
| `read` | `{path}` relative checkout file | yes | yes |
| `action` | `{action}` complete `ActionSpec` | yes, read-only sandbox | yes |
| `plan` | `{document}` versioned plan | yes | no |
| `patch` | `{base_revision, content_digest, files}` | no | proposal only |
| `amendment` | `{document, reason}` | no | proposal only |
| `complete` | `{summary}` | no | proposal only |

Each patch entry is `{path, expected_digest, content}`. A null expected digest
means create; null content means delete. Paths cannot escape the checkout or
enter `.git`. Patches are limited to 100 distinct files, each at most 128 KiB
of UTF-8 text, and must bind the base and current whole-checkout digest.
Implementation owns actual safe application, plan amendments, inspection,
validation and delivery. No backend event directly commits, pushes, publishes,
marks a task successful, or finishes a queue item.

Model interruption checks stop/pause policy while polling the worker. It first
sends SIGTERM and kills the process group after a bounded grace period. Remote
work or billing can remain uncertain after interruption or timeout. No network
request is automatically retried. Provider errors, refusal text, stderr, and
authentication bodies are withheld from progress and exceptions.
The transport rechecks controls before spawning its worker. Selected token
occurrences are removed from serialized source data as well as instructions;
model selections and returned proposals containing that token are rejected
before they can reach backend progress or the research journal.

## Research supervisor

`ResearchStage.step` consumes the owned run, saved full context from
`ContextPlanStore.refresh`, supervisor `Workspace`, journal, source store,
backend request, and checkpoint. It requires an `ApprovedExecutor` configured
with `read_only=True`. The caller composes the checkpoint with current
`GateEvaluator` controls and supplies the executor's command/periodic gates.
The optional `StageBudget` wraps model and approved command calls, bounds their
timeouts, and joins the checkpoint so later implementation can share task-wide
active time accounting.

The stage validates saved context identity, digest, inspected commit and resolved
base branch; it verifies the original supervisor source bindings rather than
registering redacted readbacks as original content. The backend sees a bounded
checkout inventory, full saved discussion, required plan fields, previous
observations, and current action binding data. It may request bounded UTF-8
files. File reads traverse directory descriptors without following symlinks,
reject metadata and special files, and register actual bytes as provenance.
Reading a file does not execute its contents.

Every command proposal must bind an existing supervisor source and current
workspace. The supervisor requests a human decision through `ApprovalBroker`;
it never approves that request. Approved commands execute through the existing
namespace sandbox with a **read-only checkout**, hidden `.git`, no external
network, and no credentials. Even an approved write command cannot implement
changes or publish from research. Output becomes a supervisor-registered source
for the next turn. No backend-supplied source bytes are accepted as evidence.

Plans require actual inspected repository citations and fresh source hashes.
Every setup and validation action must bind the current workspace identity and
inspected content digest.
The strict branch 07 validator runs before `ContextPlanStore.save_plan`; a
free-form success message cannot substitute for a plan. Saving a plan performs
no implementation or publication. Later implementation must obtain the fresh
context/base handoff separately.

The private journal records a pending model invocation before the paid side
effect, then the returned proposal before handling it. Unknown model outcomes
stop for recovery. Approval waits retain the original proposal and operation
UUID across restarts. A marker persisted before an approved command prevents
automatic reexecution after uncertain execution. Context/account/model changes
require explicit reassessment rather than silently replaying stale proposals.
Repeated successful steps return the already-saved plan.

## Validation and remaining acceptance

Run portable deterministic checks without any backend credentials:

```sh
python -m unittest coding_runner.test_backends coding_runner.test_runtime coding_runner.test_gates
```

Tests cover real fake-process transport, bounded output, structured plan saves,
malformed output, refusal/native tool rejection, authentication failure,
separate account inventory, unsupported quota, model errors, timeout, periodic
pause, shutdown, explicit interruption, immutable provenance, restart safety,
approval waits, and research stage restrictions. Fake HTTP checks exercise the
worker's fixed endpoint, authorization header, response limits and refusal to
follow redirects or retry. The host boundary suite separately exercises actual
read-only bubblewrap write denial and isolation of an active model worker's
credential stdin from an approved agent command.

External acceptance remains pending. No paid model request, live authentication
check, designated test-repository run, implementation, or PR delivery was
attempted. The hardened production container's namespace probe currently fails;
the tool-free HTTP transport does not fix that command boundary. A controlled
release must verify the actual production sandbox, an explicitly authorized
model/auth smoke check, cancellation and billing reconciliation, structured
proposal exchange, human-approved read-only investigation, plan persistence,
and the subsequent complete implementation/delivery workflow. Verified evidence
and a deliberate readiness implementation change are required before this
candidate can become available for live work. Disabling reserve alone grants
neither backend readiness nor release permission.
