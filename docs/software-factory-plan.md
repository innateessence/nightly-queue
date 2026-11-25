# Factory execution contract

The current pipeline is:

```text
Issue → Intake → Specification → Build → Trusted checks → Reviews → Decision → PR → Human merge
```

Intake and specification freeze scope, risk, acceptance criteria and affected
surfaces. Only the builder may modify candidate source. Code review always runs;
trusted paths and intake surfaces add security and UX/UI reviewers. Missing
required role or browser capability stops dispatch. Role settings cannot change
the selected account, backend, executable, network policy or tool grants.

Contexts, plans, role outputs, routing records and round decisions are immutable
and bound to the owning run, selected connection, configuration, specification,
candidate and protected worker identity. A model's narrative, `passed` boolean or
PR URL cannot replace trusted execution, validation or delivery evidence.

Checks and reviews apply to one exact candidate. Repairs share the minimum of the
execution-policy allowance, frozen factory allowance and hard maximum of two.
An explicit zero allows no repairs. A changed candidate requires fresh validation
and routed reviews. An exhausted or canceled run cannot publish a change.

Native adapters retain the prepare/start/inspect/events/outcome/cancel/reconcile
contract. Unknown execution retains its fence; lost responses never authorize a
replacement invocation. Worker termination, source exports, complete bounded
transcripts and usage evidence remain distinct authorities.

The runtime is the [four-container architecture](architecture.md). Factory mode
is disabled by default. Enabling a setting does not establish a qualified worker,
account, model, browser or release. Engineering evidence is source/image/boot
bound and cannot substitute for live selected-provider acceptance.

Humans merge every PR. Automatic merging and deployment remain disabled until a
separate explicit rollout. A delivered PR releases the execution slot so unrelated
eligible work can continue while a human reviews it. Dependent work waits for
verified human merge ancestry.

See [settings and routing](factory-routing.md), [native conformance](native-factory-conformance.md),
[Claude Code](native-claude-factory.md), [OpenCode](factory-opencode.md), and the
[read-only acceptance observer](factory-acceptance.md). The
[release contract](vibearr-mvp.md) defines the evidence still needed for live use.
