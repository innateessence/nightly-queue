# Claude factory roles

Implementation for [issue #163](https://github.com/innateessence/nightly-queue/issues/163)
under the [factory plan](https://github.com/innateessence/nightly-queue/pull/146).
It extends the existing `NativeCLIAdapter` and managed subscription connection;
there is no separate Claude orchestration engine or API-key fallback.

The immutable CLI pin remains Claude Code **2.1.118**, installed from the vendor
archive after SHA512 verification in `deploy/docker/install_agents.py`. Model,
selected account, profile, operation and worker identity remain bound by the
shared native/factory contracts. Role output uses the complete protected terminal
result and shared `parse_role_result`; 4,000-character display previews are not
an input to artifact validation.

The worker's registered profile sets `VIBEARR_FACTORY_ROLE`. Context cannot select
or upgrade that role. `build` retains noninteractive coding tools. Intake,
specification, all reviewers and the readiness decision expose only Read/Glob/Grep
through Claude's pinned tool and permission flags. The CLI uses explicit settings,
empty strict MCP configuration, disabled skills/hooks/plugins/Chrome and no local
session persistence. A fresh HOME holds only the selected managed login. Alternate
auth tokens, provider endpoints and cloud-provider switches are rejected.

These CLI restrictions supplement the compact worker's source ownership and UID
boundary. They are not proof of read-only isolation: factory preflight also needs
current registered-profile and worker-boot permission/cleanup evidence from the
shared conformance contract. All roles share the same execution worker sequentially;
this feature adds no runtime containers. Termination and outcome still come from
the trusted worker, not Claude's final text. Unknown operations reconcile rather
than starting a replacement paid session.

Provider stream validation rejects changing/missing completion identities,
duplicate or contradictory results, post-terminal events, malformed/duplicate or
nonfinite JSON, and invalid token counts. A later success cannot erase an earlier
error, and success-looking text cannot override a nonzero process exit or unknown
descendant cleanup. Usage retains actual token counters without claiming billed
cost or subscription allowance.

Offline tests and the actual pinned executable's version/help and role-argv probes
run in Docker without provider calls. They do not qualify a subscription-backed
factory run. Live selected-account, read-only-worker, cancel/restart and two-issue
acceptance remain separate release evidence. Humans review and merge every PR;
no Claude role may merge or enable automatic merging.
