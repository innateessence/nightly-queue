# Current implementation and release status

The supported stack is [four-container Vibearr](architecture.md): application,
PostgreSQL, trusted runner and one execution worker. Queue/import scheduling,
native Codex/Claude Code/OpenCode integration, immutable factory artifacts,
trusted validation, routed reviews, PR delivery, operator controls and recovery
are implemented in source.

The [Docker check harness](docker-operations.md#development-and-ci) produces fresh
source/image/boot-bound engineering reports. Repository snapshots and screenshots
are not runtime readiness inputs; retain actual generated evidence with the
revision and installation that produced it.

Live selected-provider renewal and the complete two-task human-merge acceptance
loop remain release gates. Factory execution is disabled by default, and humans
merge every PR. See the [release contract](vibearr-mvp.md) and
[read-only acceptance observer](factory-acceptance.md).
