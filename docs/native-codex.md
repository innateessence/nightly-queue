# Native Codex sessions

vibearr runs the pinned Codex CLI in its OCI task environment. The native adapter
prepares one session, starts it without interactive decisions, reads bounded
events, and reconciles that same operation after a supervisor restart. The
supervisor retains GitHub credentials and delivery permissions.

This adapter is separate from the historical Responses API proposal transport.
It does not enable the queue lifecycle or publish changes by itself. The native
lifecycle connects sessions to source refresh, validation, and delivery; the
validation profile remains an independent, credential-free operation.

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

## Real acceptance

The acceptance command creates only run-owned disposable resources and needs a
private credential file. Use a dedicated state directory rather than an active
executor's state:

```sh
python -m vibearr_runner.codex_acceptance \
  --image sha256:<codex-image-id> \
  --database-image sha256:<postgres-image-id> \
  --state /var/lib/vibearr/codex-acceptance \
  --credential-file /run/secrets/codex-key \
  --kind token --model <configured-model-id> \
  --evidence /var/lib/vibearr/codex-acceptance.json
```

It runs the real boundary qualification, asks Codex to edit a fixture and run its
test, restarts the executor during the session, retries the same start, verifies
the exported changes with an independent offline test, cancels a second session,
and exercises the bounded authentication-probe interface. The candidate project
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
