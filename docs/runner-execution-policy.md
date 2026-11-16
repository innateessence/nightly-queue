# Native execution policy

New built-in runner configuration uses `execution_kind: native`. The authenticated
configuration API retains outer schema version 1 for compatible readback and adds
`delivery_contract` with native receipt version 2. Each project and claim snapshot
contains `execution_policy`, its canonical SHA-256 `policy_digest`, and
`delivery_contract_version: 2`. Secrets remain outside configuration and snapshots.

## Configured permissions

`RunnerSettings.execution_policy` is the global default. Project overrides use the
existing inherit/replace mechanism with the `execution_policy` key; clearing the
policy is invalid. The settings form accepts JSON. The default is:

```json
{
  "schema_version": 1,
  "execution_kind": "native",
  "workspace_access": "write",
  "runtime_profile": "native-default",
  "validation_profile": "project-default",
  "network_hosts": ["api.openai.com", "pypi.org", "files.pythonhosted.org"],
  "cpu_limit": 2,
  "memory_mb": 4096,
  "pids_limit": 128,
  "workspace_mb": 2048,
  "repair_limit": 2,
  "delivery_mode": "pull_request"
}
```

`vibearr_runner.policy.ExecutionPolicy.from_data` validates the complete exact
schema. Unknown capabilities, versions, host paths, URLs/wildcard/private DNS
hosts, and out-of-range limits are rejected. Resource limits require integers;
booleans are not numeric limits. Runtime and validation profiles are registered
identifiers, not commands or images selected by task data. Delivery mode is
`pull_request` or `merge`; merge execution is a later feature.

The runtime must resolve and enforce allowed host addresses, private-network
restrictions, mounts, and resource limits. A policy document alone does not
implement isolation. Default profile names are contract identifiers, not installed
runtime capabilities. Until runtime and native adapters are available, readiness
is false even if settings are structurally valid or historical release evidence
was qualified.

Native configuration requires an implementation backend/model/credential and
GitHub issue/delivery credentials. Codex initially accepts an API token, not a
subscription session. Separate research backend/model/credentials remain available
as saved settings but are inactive for native requirements and account inventory.
Setup and validation command settings remain until trusted validation profiles
are implemented. The native executor will select immutable registered recipes;
agent-supplied commands cannot change policy.

`runner_control.execution_policy.current_native_policy` checks active ownership,
claim token, enabled/pause controls, and agreement between current policy and the
saved digest. It is a policy-binding check, not a runtime permit: schedule, quota,
qualification, and executor checks are also required at dispatch.

## Native delivery contract

`vibearr_runner.native_evidence.validate_native_evidence` defines the version 2
receipt shape without requiring a human approval, action proposal, or separate
research-plan handoff. The configuration API publishes its required root fields.
The receipt binds run/operation/runner IDs, configuration and policy digests,
context, pinned runtime and agent session, repository/base/branch/tree/commit,
validation profile and actual check outcomes, pushed SHA, and exact PR identity.
Merge-mode evidence adds passing remote checks bound to the head, an observed
merge commit, and optional issue-completion operation evidence.

Validation command records include stable IDs, UUID operation IDs, exit code,
timeout status, timezone-aware start/end times, and log reference/digest. Duplicate
command IDs, nonzero/timed-out checks, changed trees/commits, foreign PR identity,
mixed legacy fields, and merge claims without matching remote checks are rejected.

This module validates structure and internal bindings. It does not authenticate
an execution, determine required profile coverage, query GitHub, or authorize
success. Feature 04 owns server/supervisor integration of those checks. Until
then native `verified_delivery` writes and success remain rejected rather than
falling through the legacy approval-based validator. Native progress artifacts
can use the existing owned-run APIs without a mandatory legacy saved plan.

## Upgrade and legacy history

Migration `0009_autonomous_execution_policy` retains existing queue items, claim
identity, plans, logs, and approval decisions. It disables execution and requests
cancellation for legacy active runs, recording `legacy_execution_stopped` evidence
and a retry diagnostic. Active state/slot and claim stay intact, including when
an approved command was consumed and its outcome is unknown. The migration never
claims that a process stopped, releases ownership, or requeues work automatically.

Reconcile children and external side effects with the original controller first.
After termination is confirmed, the cancellation/finish path can release ownership
and an explicit new attempt can use native execution. Existing disabled projects
stay disabled. Historical approvals remain readable; new native claims cannot
create, decide, or consume human approval actions, and their inbox readback has
no decision controls. Legacy proposal code is retained for history and unit
contracts but cannot dispatch a native run or make the native configuration ready.

The [settings screenshot](screenshots/native-policy-settings.png) shows the actual
policy form rendered against an isolated empty database, with no live credentials.
