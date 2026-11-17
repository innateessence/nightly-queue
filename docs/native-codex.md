# Native Codex sessions

> **Docker-only runtime requirement:** Follow the [Docker-only runtime plan](docker-only-runtime-plan.md)
> and [deployment contract](mvp-deployment.md). Every Python, Git, coding CLI,
> packaging, test, qualification and browser command in this reference executes
> inside its appropriate container, never on the host. Host-service/daemon-launcher
> procedures and their old proofs are historical and cannot qualify the new worker
> boundary. Missing container tools must be implemented in D1–D4; no host fallback.

vibearr runs the pinned Codex CLI in its OCI task environment. The native adapter
prepares one session, starts it without interactive decisions, reads bounded
events, and reconciles that same operation after a supervisor restart. The
supervisor retains GitHub credentials and delivery permissions.

This adapter is separate from the historical Responses API proposal transport.
The native queue lifecycle connects sessions to source refresh, independent
validation, and supervisor-owned GitHub delivery. The validation profile remains
an independent, credential-free operation. See [unattended execution](native-queue.md)
for deployment and recovery.

## Image and profiles

Build the agent image from the repository root:

```sh
docker build -f vibearr_executor/Codex.Dockerfile -t vibearr-codex:0.169.0 .
docker image inspect vibearr-codex:0.169.0 --format '{{.Id}}'
```

The recipe pins Python 3.12.12, Git, and Codex **0.169.0**, including its separate
`codex-code-mode-host` executable. It verifies the published archive SHA-256 before
extracting each executable. Linux x86-64 and ARM64 archives have separate pins.
Deploy the resulting immutable `sha256:` image ID, rather than a mutable tag.

That image contains the CLI, Git, and Python; it does not contain this
application's requirements. For repository work, bootstrap must register a
separately reviewed image derived from that immutable Codex image with the
repository's common requirements installed. Build it once without model or
GitHub credentials, using a trusted recipe and reviewed requirements. Qualify its
new image ID and select that ID in the agent profile. Do not select the bare
fixture image for Django application work or let candidate Dockerfiles choose
the agent image. Later candidate dependency changes are resolved and checked by
the independent, credential-free validation builder; the model phase retains
only its model endpoint allowlist.

Use `vibearr_runner.native_codex.codex_profile(image, kind)` to construct the
registered profile. Its default IDs are `codex-token-v1` and `codex-session-v1`.
The helper fixes the launch recipe and selected-secret slot. Profile limits are
operator ceilings; each session manifest uses the project's smaller configured
limits. The adapter checks the complete recipe digest against executor
capabilities before preparing work.

Register the agent profile alongside the project's trusted validation profile.
Set `execution_policy.runtime_profile` to its ID and
`execution_policy.validation_profile` to the validation profile's ID. Qualify the
installed images, profiles, executor code, and host using
[the runtime verifier](isolated-executor.md). A native account probe can run
before any production claim; historical proposal release evidence is unrelated.

## Authentication

A dedicated Codex API key is the default. Select a saved Codex token credential;
the adapter supplies it only to this task's CLI as `CODEX_API_KEY`. Its agent
profile permits `api.openai.com`. Dependency installation belongs to a separate
credential-free setup phase, and validation has no external network.

An explicitly selected Codex session credential is an additional supported path.
It accepts the pinned CLI's `auth.json` format, including the optional
`login_owner_id` and `oauth_provenance` metadata. These fields do not select an
issuer, endpoint, permission, or refresh mechanism. The supervisor removes the
refresh token and metadata before delivery. The wrapper uses the pinned CLI's
non-refreshing `chatgptAuthTokens` mode with a private temporary login file. This
profile permits only `chatgpt.com`; `auth.openai.com` is not available to the task.
An invalid access token, or one expiring within five minutes, fails diagnostics.
Replace an expired session through the credential store outside task execution.
Tasks never refresh or write back the operator's login.

The adapter never discovers a host login automatically. A copied session must be
selected deliberately and kept outside source control. API keys remain preferable
for unattended public-repository work; OpenAI's
[automation authentication guidance](https://developers.openai.com/codex/noninteractive/)
describes the account-session limitations. This implementation's session smoke
uses only a hand-authored disposable fixture.

The CLI and its tools can access the selected API key or access-token material.
That exposure is contained to the task, not isolated from its own tools. They
receive no refresh token, GitHub token, queue token, application encryption key,
or personal configuration. Known structured secret values and their JSON-escaped
forms are redacted from logs and rejected from exports. This does not promise
removal of arbitrary encodings or a provider-side spending limit.

## Fixed CLI configuration

The image wrapper reads a bounded, supervisor-produced context containing only
the model, prompt, adapter version, authentication kind, and an account-reference
digest. It rejects alternate
arguments or configuration fields. Prompts go through a private stdin file,
never shell interpolation.

The wrapper verifies the installed version, creates a clean `CODEX_HOME` under
the task's temporary home, and invokes `codex exec --json` with explicit external
sandbox operation. Interactive approvals are disabled inside that registered
boundary. User configuration, exec-policy rules, plugins, hooks, apps, cloud
skills, and web search are disabled. The project is explicitly untrusted for CLI
configuration, so candidate `.codex/config.toml` cannot add MCP servers or replace
the configured model. Ordinary repository instructions remain task data and
cannot expand the supervisor's policy.

Codex's session files and temporary login live outside `/work`, so they are never
imported as code. After task termination, the executor removes its container and
temporary files. Native JSONL events are observations, including token usage;
they are not validation or delivery attestations. Unsupported account allowance
telemetry remains unsupported.

## Supervisor interface

`NativeBackendRegistry().create("codex", executor_client)` returns the native
adapter. It does not replace `BackendRegistry`'s proposal interface.

1. Choose and journal `run_id`, `operation_id`, deadline, and frozen inputs.
2. Call `prepare(run_id=..., operation_id=..., source=..., prompt=..., model=...,
   policy=..., deadline=..., credential=...)`. `source` is the verified source
   archive; `credential` is the selected scoped secret grant. Prepare uploads the
   immutable artifacts and credential but does not invoke the model.
3. Persist `NativeSession.to_data()` before calling `start(session)`. The record
   contains the exact manifest, runtime binding, image, CLI version, selected
   credential ID/version, authentication kind, and model. It contains no secret.
4. Read `events(session)`, or feed bounded log chunks to `CodexEvents` for live
   progress. `inspect` and `reconcile` retain not-started/running/exited/unknown
   distinctions. A new supervisor restores `NativeSession.from_data(record)`.
5. `outcome(session, original_source)` verifies termination, the original source,
   protected log digest, bounded complete CLI output, and the returned inventory.
   It returns observed events, token usage, protected receipt, and verified source
   bytes. Store returned source bytes in protected artifact storage, not directly
   in JSON run evidence. Independent validation still decides whether to deliver.

Each read reasserts the exact prepared manifest. Start also requires the original
qualified runtime binding. Cancellation and reconciliation remain available after
qualification changes. A lost start response uses status lookup for the same
operation; it never chooses another UUID or starts another CLI session. Unknown
outcomes remain fenced. `cancel` must confirm descendant termination before a
replacement attempt is allowed.

Cross-container CLI resume is deliberately unsupported. Repair attempts start a
new invocation only after the previous operation is confirmed stopped, preserving
its frozen backend selection and existing attempt limits.

## Project test environment

An agent profile can register a pinned `database_image` through
`codex_profile(..., database_image=...)`. The executor creates a fresh PostgreSQL
sidecar and generates its connection under the run's private job. The wrapper
accepts only that executor-provided disposable connection marker, validates its
fixed task database format, and derives `DATABASE_URL`, `POSTGRES_*`, and
`VIBEARR_POSTGRES_*` for the native child. Ambient host database variables are
discarded. Candidate context cannot name a database or select these variables.
The existing redaction and export checks include the generated password and URL,
alongside the selected backend token values.

`NO_PROXY` and `no_proxy` are fixed to `127.0.0.1,localhost` so local HTTP tests
stay inside the task rather than using the model proxy. Docker DNS, gateways,
and private networks are not proxy exclusions; the runtime firewall still
enforces actual egress. Python bytecode writes are disabled, output is unbuffered,
and `TMPDIR=/work` uses the policy-bounded workspace for temporary venvs and tests.
No host temporary directory is inherited. Temporary login and prompt files retain
explicit 0600 modes under a private 0700 home even when project helpers use a
less restrictive umask.

## Authentication diagnostics

Configured key or login-file presence does not prove authentication. Without a
live proof, `diagnostics(policy, grant)` reports local availability with
`authenticated: false` and `native_agent_ready: false`.

Use `prepare_auth_probe` with the same IDs/model/policy/deadline/credential
arguments and the normal journal-before-start sequence. It supplies fixed source
and a greeting-only prompt. After completion, `authentication_result(session)`
verifies the fixed context/source, actual CLI completion, protected logs, unchanged
export, and runtime binding. The resulting proof binds the selected credential
ID/version, model, authentication kind, profile, image, CLI, operation, and session.

Cache that proof only in protected supervisor state. Calling
`diagnostics(policy, grant, model=model, proof=proof)` additionally verifies its
bindings and a maximum 15-minute lifetime. A changed account, model, image,
profile, runtime, expired proof, or locally expired session requires another
automated probe. Issue content and agent output cannot supply this proof. Runtime
diagnostics remain runtime-only and do not infer native account readiness.
The lifetime starts at the protected receipt's actual completion timestamp;
rereading a retained operation cannot renew it.

## Real acceptance

Drain and stop the supervisor, then qualify idle Docker workers as described in
[Docker operations](docker-operations.md). Provision an explicitly selected,
dedicated credential in the private runner secret volume. This command makes
real model calls and requires `--execute`; ordinary qualification does not run it.
Choose new state and evidence paths so previous operations remain available for
reconciliation. Never import the operator's home or agent login.

```sh
docker compose --profile checks run --rm --no-deps --entrypoint python qualification \
  -m vibearr_runner.codex_acceptance --execute \
  --state /artifacts/native-fixture-<new-run-name> \
  --credential-file /run/vibearr/runner/acceptance-key \
  --kind token --model <configured-model-id> \
  --evidence /artifacts/native-fixture-<new-run-name>.json
```

It uses the existing qualified fleet, asks Codex to edit a fixture and run its
test, observes the same running worker operation from an independent controller
process, reloads its saved session, and retries the same start. The registered
offline runner check independently validates the export. It then cancels a
second real foreground tool session and runs the bounded authentication probe.
It does not restart workers or replace their boot qualification. The candidate project
configuration contains an MCP marker that must remain unexecuted. These are real
native calls; the command does not substitute fixture events for provider access.

Portable tests separately cover parsing, output bounds, malformed/missing auth,
signals, hangs, immutable operation bindings, revocation, cached-proof expiry,
credential rotation, and structured-secret redaction. See the PR's acceptance
record for exact host/image results. A session-mode pass does not claim that a
dedicated API key was tested when no key was supplied.

Pinned upstream references:
[Codex 0.169.0 release](https://github.com/openai/codex/releases/tag/rust-v0.169.0),
[noninteractive execution](https://developers.openai.com/codex/noninteractive/),
[authentication](https://developers.openai.com/codex/auth/), and
[the pinned external-token implementation](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/login/src/auth/manager.rs).
