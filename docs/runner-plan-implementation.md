# Saved-plan implementation and validation

This branch adds the supervisor stage in `coding_runner.implementation`. Runtime
execution remains disabled: the service loop still only probes configuration and
gates. This code is injectable and tested using local repositories and fake model
responses. It does not establish live backend or release acceptance.

The caller prepares or reopens the exact claimed workspace with
`WorkspaceManager.prepare(run_id, item)` before constructing the stage. That
manager creates or reuses the returned task branch, records its resolved remote
base, and preserves dirty work. The stage verifies the branch, base ancestry and
unmerged index at every boundary. It never resets, commits, pushes or creates PRs.
Delivery belongs to feature 10.

`ImplementationStage(backend, journal, store, executor, sources, budget, snapshot,
checkpoint=..., progress=..., stop=...)` takes the frozen claim configuration and
an owned-run `ContextPlanStore` with a client sized for bounded complete context
responses (`max_bytes=8388608`). Its `step(state, run_id, item, workspace, request,
issue_client=...)` returns `ImplementationResult(status, detail)`:

- `continue`: a durable proposal or stage transition completed.
- `approval`: the exact action is awaiting a human decision; the claim remains.
- `wait`: pause, schedule, reserve, shutdown or an interrupted approved command
  prevents further work at a safe checkpoint.
- `failed`: active time is exhausted or the implementation produced no changes;
  detail gives an explicit explanation.
- `delivery_ready`: reviewed task changes and every required check match the
  current exact revision and workspace content.

Malformed proposals, changed settings, conflicts, stale plans and uncertain prior
side effects raise an error for recovery. They never authorize a retry, success,
reset, claim replacement or publication. Backend success text alone only requests
diff review. It cannot create validation evidence.

## Context, amendments and proposals

Each step fully refreshes the issue/manual context and verifies the server's saved
plan handoff. Closed issues or incomplete discussions stop work. The original
saved context is read back to validate the plan; a newer fetch timestamp can keep
the plan when the actual discussion is unchanged. Changed discussion, resolved
base, or inspected base requires a saved assessment and revised plan. Material
plan changes retain their prior/new IDs and explicit reason and invalidate checks.
The existing workspace is preserved throughout.

Backends must use the common structured proposal contract with native tools
unavailable. Implementation permits bounded reads, exact action proposals,
grouped patches, material amendments and completion summaries. Patches bind both
the base revision and the supervisor's current content digest. Every changed file
also has an expected byte digest, or `null` for a new file. All file conflicts are
checked before changes begin. Relative paths cannot traverse parents, symbolic
links, hard links or `.git`. New directories and UTF-8 files are supported; new
files receive mode `0644` and existing file modes are preserved. A crash during a
grouped change leaves an unresolved journal marker and requires recovery; partial
work is never discarded or blindly repeated.

The model receives the saved plan, complete current context, file inventory,
workspace binding and bounded observations. It receives no supervisor environment,
API credential or claim token. An unresolved paid model request is journaled
before the call and blocks automatic replay after a crash. Reads and accumulated
observations are bounded; oversized contexts fail closed.

## Commands and validation

Plan setup and validation actions are structured `ActionSpec` proposals. Frozen
configuration setup/validation commands are additional mandatory actions. They
are displayed as explicit `/bin/sh -c` actions with operator configuration
provenance and require their own human approval; settings and saved plans grant
no execution permission. The approval broker uses supervisor-computed source and
workspace digests, exact argv/mode, workspace ID and base revision. Changed source
or files produce a new binding and a fresh approval request. A denied decision
never executes the action.

Use the existing `ApprovedExecutor` with its verified task-only Bubblewrap
boundary. Both its normal gate and periodic gate must read current global
controls/accounts using `GateEvaluator`; the stage additionally composes stop and
active-budget checks into the periodic gate. Human waits occur between `step`
calls and consume no active time. Source digests are recomputed by `SourceStore`;
backend-supplied provenance alone cannot authorize an action. The approval is
single-use, and a journaled uncertain command cannot be repeated automatically.

A completion proposal leads to supervisor diff inspection against the recorded
base, then required validation. Each check records its exact action, execution
ID, exit code, interruption flag, before/after workspace fingerprints, and output
byte count/digest. Raw command output is omitted from durable display evidence;
the full output is untrusted and is not a new command source automatically.
Failed checks return to implementation for repair within the remaining budget.
Any check that changes workspace content invalidates all previous checks and
requires diff review and a complete fresh validation pass. This includes generated
files: projects should choose stable validation commands.

Delivery readiness contains schema version 1, plan/context IDs, plan digest,
amendment history, current workspace fingerprint and passing validation records.
The fingerprint includes task workspace ID, base revision, branch, HEAD,
content digest, and diff digest relative to the recorded base (including untracked
files). A later file change invalidates readiness. `revalidate(state, run_id)`
resets to diff review and validation without another model call when validation
alone is needed.

`verify_delivery_ready(workspace, evidence)` checks exact current readiness.
Feature 10 may make a supervised commit and then call it with
`committed_head=<verified commit>`. Only HEAD may change: content, diff, base and
branch must still match and Git status must be clean. The returned attestation
records validated HEAD and committed HEAD. Identical validated files do not need
duplicate checks just because the supervisor committed them. Any changed file or
diff requires fresh validation. This helper does not approve Git operations,
verify a remote PR or finish a queue item.

## Active time and recovery contract

Create one `ActiveBudget` for the run and share it with research/planning and
implementation/validation. Its limits come from the frozen task/stage timeouts.
`run(stage, operation)` persists an active segment before calling
`operation(remaining_seconds)`, accumulates monotonic elapsed time on every exit,
and checks both the task total and that stage's accumulated time. The stage passes
the remaining time to supervised models and commands; command process groups and
the model worker are stopped by their existing timeout/periodic supervision.
`checkpoint()` is suitable for periodic gate composition.

Research and planning use the `research` allowance. Context refreshes also count
as research. Implementation calls and setup commands use `implementation`;
required project checks use `validation`. Time spent waiting for human decisions,
processing windows or usage is excluded. Persisted totals survive restarts. An
unresolved active segment blocks execution until explicit recovery accounts for
it; a restart cannot grant a new full budget. Recovery must reconcile model/action
markers and terminate surviving children before clearing any uncertain state.

## Verification and remaining release blockers

Run portable budget tests with:

```sh
python -m unittest coding_runner.test_budgets
```

Run actual Git/Bubblewrap acceptance on the runner host or runner image:

```sh
python -m unittest coding_runner.acceptance.test_implementation
```

The acceptance suite uses temporary local repositories and explicit fake human
approval decisions. It covers manual and issue contexts, timestamps, stale plans,
amendments, task branch preservation, conflicts, unsafe patches, mandatory settings
commands, missing approvals, no-change failure, repairs, total budgets, pause,
uncertain side effects, actual sandbox validation, actual timeout interruption,
post-commit attestation and invalidation after further changes. Git-dependent
acceptance lives outside default Django discovery so the web image does not need
runner tools. These tests do not call paid backends or real queue items.

The production image's namespace policy still needs the explicit controlled
verification documented in `runner-workspaces-approvals.md`. A designated live
test repository, authorized backend calls and successful release verification are
still required. No container policy was weakened and execution remains disabled.
