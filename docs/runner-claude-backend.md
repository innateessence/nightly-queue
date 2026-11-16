# Claude backend candidate

The `claude` registry entry is a **Claude Messages API candidate**, not a native
Claude Code CLI integration. Execution remains release-gated. No paid API call,
live authentication check, or controlled remote delivery/recovery scenario has
been performed for this branch. Native Claude Code is unsupported. A successful
version/help probe does not establish native tool mediation or safe credential
isolation.

## Transport and credentials

Select Claude independently for research or implementation in Settings or the
project overrides. Select a saved `claude` token credential for each stage and
enter an explicit Claude API model ID. Model suggestions are editable browser
choices; they do not assert availability on the selected account. There is no
Compose model or token setting. Codex research with Claude implementation, the
reverse, and two distinct Claude accounts retain separate selections.

Only Anthropic API tokens are supported. Claude subscription/OAuth sessions,
Bedrock, Vertex, Foundry and arbitrary proxy URLs are unsupported. The scoped
secret endpoint still enforces the selected run/stage credential; the adapter
checks provider, credential ID and kind on each request. It never falls back to
another account, ambient environment credentials, or another provider.

The supervisor launches a trusted Python worker with isolated Python, a fixed
worker path, `/` as its working directory, an empty inherited environment,
private stdin/stdout, closed unrelated file descriptors and a new process group.
Python may add its locale variable. The token goes through private stdin, never
an argument, environment variable, checkout file, model prompt or progress event.
The worker is supervisor code: it cannot execute model output or repository code.
The command sandbox remains a separate process with no access to this worker's
credentials, descriptors, filesystem or process namespace.

Inference uses only `https://api.anthropic.com/v1/messages`; HTTP redirects and
endpoint overrides are unavailable. `anthropic-version` is pinned to
`2026-10-21`. No provider SDK is installed. Controlled authorization can additionally perform
a fixed-host `GET /v1/models/{model_id}` authentication probe with the same bearer
API token and version header; this issues no inference request. The official
[Get a Model reference](https://platform.claude.com/docs/en/api/models/retrieve)
was reviewed on 2026-11-15. A successful probe is valid only inside the authorized
active scenario; it does not qualify normal execution.

The bounded inference request uses `tools: []`
and `tool_choice: {"type": "none"}` and never enables server tools, MCP,
containers, skills or code execution. The only allowed model IDs match
`claude-[a-z0-9]+(?:-[a-z0-9]+)*` with a 200-character bound. This is syntax
validation, not a live model availability check. CLI aliases and URLs fail before
credential retrieval.

The response must be an assistant message with `stop_reason: end_turn` and one
JSON text proposal. Thinking blocks are ignored; native/server tool blocks,
refusals, truncated responses and unsupported blocks fail closed. Research allows
`read`, `action`, `plan`; implementation allows `read`, `action`, `patch`,
`complete`, `amendment`. Provider-neutral process and proposal validation lives in
`coding_runner/proposal_backend.py`; Codex retains compatibility imports.

The API shape and version were reviewed against Anthropic's official
[Messages reference](https://platform.claude.com/docs/en/api/messages/create)
and [version policy](https://platform.claude.com/docs/en/api/versioning) on
2026-11-15. Suggested model IDs (`claude-opus-5-5`, `claude-sonnet-5-5`,
`claude-haiku-5-5`) come from the official
[model catalog](https://platform.claude.com/docs/en/models/overview), reviewed on
the same date. Operators must confirm model availability during authorized live
qualification; the catalog can change independently of this code.

## Approval, interruption and recovery

An `action` event only proposes an `ActionSpec`. Research/implementation stages
verify the actual source and workspace bindings using `SourceStore`, request an
individual human approval and invoke `ApprovedExecutor` only after approval.
Backend output cannot approve itself. Changed source, checkout/base or command
requires a new approval. Research retains the read-only executor restriction.
The adapter has no shell, Git, filesystem or publishing tool of its own.

Every model call checks pause/schedule/reserve/cancellation before starting and
through the shared periodic process checkpoint. Timeout, cancellation or a
closed gate terminates and reaps the worker process group, escalating to SIGKILL
when necessary. This stops local work; it does not prove an already accepted
remote request was unbilled. Durable stage markers keep ambiguous model calls
and approved-command outcomes from being automatically repeated after restart.
An approval wait retains the claim and exact proposal. Recovery requires operator
reconciliation when the outcome is uncertain.

## Allowance and release status

`read_usage` returns `UsageReading(supported=False)`. Message token counts,
per-request limits, billing observations and rate-limit headers are not remaining
account allowance percentages. This adapter implements no verified allowance
API. With global reserve enabled, unsupported Claude allowance blocks all
projects, including projects using another backend. Each provider/credential
pair is evaluated independently; readings are never averaged or borrowed from
another account. Explicitly disabling reserve removes only that gate.

A structurally valid saved token is not authenticated readiness. Proof protocol
`structured-proposals-v1` includes an account entry with exact provider, credential
ID, secret version, model and transport `claude-messages-structured-proposals-v1`.
Codex evidence, native-CLI evidence, another account/model, rotated credentials
or a changed deployment cannot qualify Claude. Readiness also recomputes the
real sandbox/code/environment binding and checks proof expiry.

The candidate
must be qualified against the deployed sandbox, exact transport/model/credential
version and an authorized test repository before release. The production Docker
namespace probe remains blocked by the current deployment policy. No sandbox
policy was weakened and no runtime execution was enabled for this feature.

## Pinned native probe

The official npm Linux x64 package `@anthropic-ai/claude-code-linux-x64@2.1.118`
was downloaded for a nonbillable version/help probe outside the checkout. The
package bytes were checked against npm's SHA512 integrity before extracting only
the executable. It is **not installed in the runner image**, because no native
CLI execution path is supported.

- Artifact: `https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.118.tgz`
- SHA512 (base64): `t6aNIvNa1T+ZR5IkJARqjTy+U5LH59FuWok4QoXa/RpT4C0njeeE/SdUzvvhwmH3ji/Rh6EX4zgbx/v5yGtG8Q==`
- Actual `--version`: `2.1.118 (Claude Code)`.
- Actual `--help`: supports `--bare`, `--tools`, `--disable-slash-commands`,
  `--setting-sources`, `--strict-mcp-config`, `--no-session-persistence`.
- No prompt, login, API call, repository task or paid smoke was invoked.

The official [CLI reference](https://code.claude.com/docs/en/cli-reference) and
[settings precedence](https://code.claude.com/docs/en/settings) were inspected.
Tool-empty mode and configuration controls exist, but current online documentation
also describes newer flags absent from the pinned help. Version/help evidence
alone cannot prove managed policy, hooks, helper processes and credential access
are isolated during a real session. `probe_claude` therefore always reports native
readiness false, even for the matching version. API candidate support must never
be advertised as installed or verified Claude Code.

## Deterministic validation and remaining acceptance

Run the shared provider contract suite, Claude worker/schema/process suite, mixed
account reserve suite and stage approval/recovery suite without paid requests:

```sh
python -m unittest coding_runner.test_claude_backend coding_runner.test_backends
python manage.py test runner_control.test_claude_settings
```

The explicit Git-dependent `coding_runner.acceptance.test_claude_recovery` suite
replays the actual lifecycle with real Claude parsing and local Git, fixture
provider responses/credentials and a test approval executor. It reaches individual
approval, implementation, validation, delivery, lost finish response and restart
reporting without a second PR or model retry. These are deterministic fixtures,
never live release evidence. Portable image verification includes this suite.

These tests cover both providers through the same contract, real local worker
processes, credential transport, no tool execution, stage restrictions, malformed
output, timeout/pause termination, individual approval, changed-source
invalidation, restart ambiguity, separate accounts, missing/stale allowance and
global reserve without averaging or fallback. Worker HTTP contract tests inject a
local connection double and assert the exact endpoint/version/header/body; they
do not establish live compatibility.

Before release, an operator must authorize the selected API account/model and a
designated integration repository. Perform live authentication, tool denial,
process interruption, approved validation, push/draft-PR delivery, interruption
and restart reconciliation with the real deployed sandbox. Record genuine
provider-specific release evidence. A designated repository and paid-call
authorization have not been supplied; the live smoke and controlled delivery
scenario remain outstanding.
