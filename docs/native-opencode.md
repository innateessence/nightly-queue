# Native OpenCode sessions

The native `opencode` adapter runs OpenCode **1.19.5** in a registered OCI task
profile. It implements the same immutable session, protected result, independent
validation and Git delivery contract as the other native adapters. The historical
tool-free provider API transport remains separate.

The first supported native provider is **OpenAI with an explicitly selected API
key connection**. Select backend `opencode`, a saved OpenCode connection with
provider `openai`, and a full native model ID such as `openai/<model-id>`. The
endpoint must be `https://api.openai.com/v1`. Other stored connection providers,
OAuth logins and local Ollama are unavailable in this native implementation.
An unsupported provider/model fails without switching provider or credentials.

No usable provider credential was supplied for this implementation's acceptance.
Actual provider authentication, successful native model/tool execution and a real
issue-to-PR run remain **unproven**. Key presence, unit fixtures, model catalogs
and the offline CLI smoke do not make `native_agent_ready` true.

## Install and register

Build from the reviewed source:

```sh
docker build -f vibearr_executor/OpenCode.Dockerfile -t vibearr-opencode:1.19.5 .
docker image inspect vibearr-opencode:1.19.5 --format '{{.Id}}'
```

The recipe pins Python 3.12.12 and the official release archive SHA-256 for Linux
x64 baseline and ARM64. Deploy the resulting immutable image ID. The x64 archive
digest is `17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d`;
the ARM64 digest is `fae5be262d693b7c5e562856cc473e59e741772936db0c0600ac0d5ed5bd088b`.
The upstream tag resolves to `810c0eba1a7348036f8bdf61e6632d0c36c6876c`.

`vibearr_runner.native_opencode.opencode_profile(image, "connection")` constructs
the fixed `opencode-openai-v1` agent profile. It permits only `api.openai.com`,
uses the fixed wrapper and backend-secret slot, and supports the executor's
generated disposable PostgreSQL sidecar through `database_image=...`. Register
and qualify this exact profile and the independent project's validation profiles.
Select its ID in `execution_policy.runtime_profile`; the adapter verifies the
whole recipe digest, image, resource ceilings and permitted hosts.

This base image contains the CLI, Git and Python. For application work, use a
reviewed derived image with the project's dependencies installed without model or
GitHub secrets, and qualify that new immutable image. Candidate Dockerfiles or
configuration cannot select the image or validation commands.

## Fixed invocation and configuration

The trusted wrapper validates a bounded context with adapter/model/prompt/auth
kind/account digest. It invokes the pinned CLI with `run --format json --auto`,
the exact selected model, the built-in build agent and `--dir /work`. The prompt
uses private stdin, without shell interpolation. CLI startup cwd is private HOME;
the selected work directory is passed explicitly.

This version creates a private in-process server for ordinary `run`. It does not
connect to a shared user service. The wrapper never supplies attach, continue,
session, fork or command options. Newer V2 documentation describes `--standalone`
and a different permissions/configuration generation; those flags and schemas
are not assumed to exist in the pinned release.

Every invocation gets private HOME, XDG directories, DB, cache and temporary
paths outside `/work`. It discards ambient provider/cloud/Git/application
credentials and host configuration. The only provider auth is the selected
connection's API key in the pin's single-provider `OPENCODE_AUTH_CONTENT` format.
It never discovers, refreshes or writes back a host login. GitHub delivery and
queue credentials stay with the trusted supervisor.

Project config discovery, external/default plugins, external/Claude skills,
Claude prompts, model metadata downloads, update checks and LSP downloads are
disabled. Trusted inline V1 configuration fixes the main and small model, enabled
provider and endpoint, disables MCP/formatters/LSP/sharing/snapshots, and denies
unspecified permissions. Build permissions allow ordinary workspace read/edit/
search/bash and todo operations. Questions, external directories, subagents,
skills and web tools remain denied. `--auto` permits routine coding without an
interactive prompt; it cannot override explicit deny rules.

The selected API key is accessible to the CLI and its tools inside its own
container. It is not isolated from those tools. Structured key values are redacted
from logs and rejected from source exports; arbitrary encodings are not a promised
secret boundary. The OCI runtime owns actual mounts, egress, resources, wall-clock
deadlines and descendant termination. CLI permission rules supplement that boundary.

## Events, recovery and proof

`NativeBackendRegistry().create("opencode", executor_client)` returns
`NativeOpenCodeAdapter`. Persist the complete `NativeSession` before start, and
publish the server dispatch intent through the normal native lifecycle. The
session freezes credential ID/version, full provider/model, CLI, image, profile,
runtime binding, source/context digests, run/operation and original deadline.

The bounded JSONL parser reads the pinned CLI's step/text/tool/error events,
binds every part to one `ses_...` session and rejects duplicated or foreign parts.
An intermediate tool-calls finish does not complete the task. Success requires a
terminal stop reason, successful process exit, confirmed descendant termination,
matching protected log digest and verified source export. Native text/events are
observations; independent validation decides delivery. Reported usage preserves
input/output/reasoning/cache token counts. Account allowance remains unsupported;
token usage is never converted into a remaining percentage.

A supervisor restart restores the same session and reconciles the same executor
operation. Repeating start is idempotent; it does not send another prompt. A lost
server/container is an interrupted or uncertain attempt, not permission to resume
another session. Cross-container CLI resume is unsupported. Cancellation uses
executor stop and requires confirmed descendant termination before another
attempt. Unknown outcomes keep the ownership fence. Repairs preserve provider/
model selection and share the normal repair budget.

`prepare_auth_probe` uses fixed source and prompt. Its completion verifies the
protected receipt, unchanged source and runtime binding. A private proof binds
the actual selected connection/version/model/profile/image/CLI and expires within
15 minutes of real completion. Without that proof diagnostics reports local
availability with authenticated/readiness false. Rotation, expiry, auth failure
or runtime change invalidates it.

## Verification and remaining acceptance

Run the actual pinned CLI offline in an owned container:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --tmpfs /home/task:uid=1000,gid=1000,mode=0700 \
  --tmpfs /tmp:uid=1000,gid=1000,mode=0700 \
  --entrypoint python vibearr-opencode:1.19.5 \
  -I -m vibearr_executor.opencode_smoke
```

This inspects actual version, help and effective configuration using a dummy key,
and verifies missing selected material fails before CLI launch. It makes no
provider request and reports authentication/task acceptance false.
Portable tests cover shared adapter conformance, parsing/limits, selected auth,
provider/model failure, lost replies/reconnect, cancellation, token units and the
fixed launch boundary. The Codex implementation stays unchanged.

Before qualification as a usable integration, run a bounded real authentication
probe, native fixture edit/test, cancellation and restart in a dedicated owned
executor using a deliberately selected usable OpenAI API-key connection. Then
use the bootstrap acceptance driver's normal controller for one approved useful
owner-authored issue. Independently collect native v2 evidence, all nine checks,
exact tested/pushed tree/head and the actual PR. Record provider/auth kind honestly;
an existing Codex ChatGPT login cannot establish OpenCode access. Do not point
this work at an unrelated Ollama service or model proxy.

Primary references:
[pinned release](https://github.com/anomalyco/opencode/releases/tag/v1.19.5),
[pinned run implementation](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/cli/cmd/run.ts),
[pinned permissions](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/core/src/v1/config/permission.ts),
[pinned auth](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/auth/index.ts),
[V1 CLI](https://opencode.ai/v1/docs/cli/),
[V1 configuration](https://opencode.ai/v1/docs/config/).
