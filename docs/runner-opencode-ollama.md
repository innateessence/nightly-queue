# OpenCode provider API candidate and optional Ollama

The `opencode` backend selects a tool-free provider API transport. Native OpenCode
CLI execution is unsupported. Neither installing the CLI nor saving a connection
establishes integration readiness. Execution stays disabled until the controlled
release procedure qualifies the exact deployment, backend, credential version,
model and transport. No live inference or controlled delivery acceptance was run
for this feature: no designated test repository or paid inference authorization
was supplied, and the production container namespace probe remains blocked.

## Web configuration

Create a credential with provider **OpenCode**, type **OpenCode connection**. Use
**OpenCode inference provider**, **Ollama endpoint**, and **Inference API key**;
operators do not need to construct JSON. Blank editing fields keep the saved
connection. Replacing a connection requires all its necessary fields and rotates
the secret version. Secrets are encrypted and never displayed. The existing
explicit preflight grants and owned-run/stage scoping govern delivery; issue-import
credentials are never a fallback.

Alternatively, connection JSON retains the strict shape
`{"provider":"ollama","base_url":"http://runner-ollama:11434"}` with optional
`api_key`. Supported providers:

| Connection provider | Candidate protocol | Destination |
| --- | --- | --- |
| `ollama` | Native `/api/chat`, JSON, no tools | Explicit trusted local origin or `/api` base |
| `openai` | Chat Completions, tool choice `none` | `https://api.openai.com/v1` |
| `anthropic` | Messages, tool choice `none` | `https://api.anthropic.com/v1` |
| `opencode` | Console OpenAI-compatible Chat Completions | `https://opencode.ai/inference/openai/v1` |

The Console candidate supports its Chat Completions model family. GPT and Claude
models served through other Console API families are not supported by this
connection; select their direct provider instead. Provider/model compatibility,
model availability, authentication and structured-output quality require actual
controlled acceptance. Model names are identifiers, never URLs or CLI arguments.
Select each stage's backend, credential and model in global runner settings or
project overrides. An installed Ollama model ID such as `qwen3-coder:30b` includes
its tag. The adapter never lists, downloads, creates or deletes models.

Hosted destinations are fixed; custom hosted proxies are rejected. Local Ollama
accepts `localhost`, `runner-ollama`, `host.docker.internal`, loopback or explicit
private IP addresses. Link-local, unspecified, multicast and public IP addresses
are rejected. URLs cannot include credentials, query strings or fragments.
Redirects are never followed, so authorization cannot move to another host.
Container `localhost` means that container; an existing host server needs an
explicitly provisioned network route and trusted endpoint. The optional service
below avoids changing an existing host Ollama installation.

## Approval and process boundary

The supervisor launches the trusted Python HTTP worker with isolated Python,
empty environment, `/` working directory, private stdin and closed extra file
descriptors. Credentials remain worker request metadata, separate from model
messages and progress events. The worker has no model tools, repository plugins,
MCP servers, shell, filesystem browser or provider-supplied execution hooks. API
errors and provider response bodies are withheld from diagnostic exceptions.

The model returns one bounded `{kind,data}` JSON proposal. Research accepts only
read/action/plan; implementation accepts read/action/patch/complete/amendment.
Native function/tool calls and incomplete responses are rejected. Proposed actions
use the shared ApprovalBroker and actual source/workspace binding before the
isolated executor runs anything. Each untrusted command needs human approval;
a plan or model tool permission does not grant it. File reads/patches retain the
shared path/digest validation. Schedule, pause, cancellation and timeout controls
apply through the same process transport and stage journal as Codex and Claude.
An ambiguous in-flight inference outcome requires operator reconciliation; restart
does not repeat it automatically.

Remaining account allowance is unsupported for every connection provider,
including local Ollama. Token counts, costs, local inference and an available
endpoint do not establish unlimited allowance. Enabling the global reserve blocks
all projects whenever any configured account lacks supported fresh quota data.
Distinct connections remain distinct accounts across mixed-backend stages.

## Optional CPU Ollama service

`coding_runner/Dockerfile.ollama` pins official Ollama `0.40.16` by multi-platform
image digest. The opt-in `ollama` Compose profile adds `runner-ollama`; the runner
profile remains separate. It has its own model volume, no published host port,
no GPU/device reservation, no application source/home/socket mounts, no database
or broker network, a read-only root filesystem, dropped capabilities and a
non-root user. Its inference network connects only the runner and Ollama;
egress permits explicit operator model provisioning. Cloud inference/web search
are disabled using `OLLAMA_NO_CLOUD=1`.

Provisioning is an infrastructure action, separate from routine web settings:

```sh
docker compose --profile ollama build runner-ollama
docker compose --profile ollama up -d runner-ollama
# Explicitly choose the download and capacity after reviewing its size/license:
docker compose --profile ollama exec runner-ollama ollama pull YOUR_MODEL
```

Then select endpoint `http://runner-ollama:11434` and that installed model in the
web UI. Starting the service performs no model pull. Existing host services,
ports, models and GPU configuration are untouched. Run the namespace and release
checks before enabling the runner; service health alone is not acceptance.

## Evidence and upstream references

The official OpenCode `1.19.5` Linux x64 release archive was downloaded and checked
against its GitHub release SHA-256
`588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a`.
Nonbillable `--version` and `run --help` probes passed in an empty environment and
isolated temporary home. `--pure` disables external plugins; the help does not
establish full managed/project configuration isolation or external ApprovalBroker
mediation of every native tool. The CLI is therefore probe-only and is not
installed into the execution image. Current upstream web docs describe a newer
configuration generation; their controls are not assumed to exist in the pinned
CLI. The candidate transport uses pinned runner Python and the HTTP schema above.

Deterministic tests exercise the shared adapter contract, real isolated workers
against a local fake HTTP server, unreachable inference, refused redirects,
redacted authentication errors, timeout/pause termination, strict proposals,
approval/source changes, ambiguous recovery, mixed providers/accounts and reserve
blocking. These checks do not count as live authentication or delivery acceptance.

The [connection form screenshot](screenshots/runner-opencode-connection.png) shows the actual rendered web settings.

Primary references reviewed:

- [OpenCode configuration](https://opencode.ai/docs/config/) and [permissions](https://opencode.ai/docs/permissions/).
- [OpenCode providers](https://opencode.ai/docs/providers/) and [Console inference](https://opencode.ai/docs/console/api/inference/).
- [Pinned OpenCode release](https://github.com/anomalyco/opencode/releases/tag/v1.19.5).
- [Ollama chat schema](https://docs.ollama.com/api/chat) and [API introduction](https://docs.ollama.com/api/introduction).
- [Ollama local-only configuration](https://github.com/ollama/ollama/blob/v0.40.16/docs/faq.mdx).
- [Pinned Ollama release](https://github.com/ollama/ollama/releases/tag/v0.40.16).
