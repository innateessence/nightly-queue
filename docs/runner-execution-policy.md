# Native execution policy

Vibearr dispatches only into a registered, authenticated, qualified
`compact-worker-v1` runtime. The app controls frozen configuration, ownership and
grants; the trusted runner controls native dispatch and publication; the worker
enforces the task boundary. Host execution and runtime Docker-daemon control are
unsupported.

Trusted project configuration selects setup/validation commands, native provider,
account/model, registered profiles, permitted network hosts, time/token/repair
budgets, resource limits and delivery behavior. Task content cannot change these
permissions. Unknown capabilities/versions, arbitrary host paths, wildcard/private
network escapes and invalid limits are rejected.

Routine setup, context retrieval, editing and validation need no per-command or
plan approval inside that policy. Immutable specifications and role JSON describe
work; they do not grant capabilities. Only builder profiles permit source changes.
Intake, specification, review and decision profiles are read-only. Visual reviews
also require qualified browser execution and current source-bound captures.

The effective repair allowance is bounded by both frozen execution and factory
policy and never exceeds two. Explicit zero remains zero. Retries and resumes
retain original deadlines, usage charges and operation identities. Unsupported
quota information cannot authorize further work.

Validation uses fresh candidate snapshots and trusted image-owned commands.
Dependency artifacts are run-bound, and protected tool changes cannot replace
the checks that evaluate them. Publication requires the complete current factory
chain, original terminal receipts and exact source/export verification.

Use PR delivery. Every PR requires a human merge, and automatic deployment remains
disabled. Configuration changes do not qualify a worker, provider or account.
See [architecture](architecture.md), [factory routing](factory-routing.md),
[native conformance](native-factory-conformance.md), and [release acceptance](vibearr-mvp.md).
