# Feature 05 independent review R4

Reviewed PR #55 at exact source commit
`0a8a90d2ec4ce89089a0107988f79b96ea0b22a3`, against accepted main
`2b2a67a872b9d552fe3f9d7b66d67ee965a8f2f0`, on 2026-11-16. Review used the
independent full clone `/tmp/vibearr-review05-r4`. This report changes no
implementation or tests.

**No P2 or higher findings remain from this review of the exact source above.**
Later source changes require another review; this report does not qualify native
agent execution or delivery.

## Scope

Read the full feature implementation diff and its feature 05/MVP and operating
contracts: provider response parsing, conditional pagination and backoff,
repository identity and authority handling, project saves/forms/API, source
selection, branches, dependencies, import/claim/retry transitions, per-run source
history, source-refresh API, configuration/credential/controller interactions,
migrations, templates, and regression/concurrency tests. Accepted runtime feature
02 is the comparison baseline, rather than an unreviewed part of this feature.

The previously reported cases are addressed: unclaimed directives cannot reserve
other work's branches; configured base and remote default are protected; stale
project edits preserve server-owned metadata; replaced claims and changed runner
identity/proof are rejected before source side effects; source history belongs to
each run; current and frozen issue revisions must agree; malformed oversized
foreign dependencies cannot abort subsequent imports; manual fallback branches
remain independently claimable.

The latest authority fix captures the complete accepted repository state and a
monotonic server-owned version before collection. Both trusted observation writers
compare that snapshot under the project lock. Every accepted observation advances
the version, including identical metadata. Import pages and the final sync cursor
also check the accepted authority; a newer observation prevents an earlier reply
from restoring an owner, default branch or canonical URL. Normal project saves
retain that version. The real PostgreSQL importer/refresh race regressions in both
directions passed in the full suite.

## Independent validation

- `python manage.py test --noinput`: **1,443 tests passed, four expected
  distribution-environment skips**, in 50.362 seconds. This includes the feature's
  PostgreSQL concurrency regressions and historical migration tests.
  Log: `/tmp/vibearr-review05-r4-full.log`.
- Black, Ruff and mypy: **passed**; Black and mypy checked 307 source files.
  Migration drift and Django system checks also passed.
  Log: `/tmp/vibearr-review05-r4-static.log`.
- Four additional independent service probes against a second disposable
  PostgreSQL test database all passed:
  - An owner observation changes 42 to 777 and back to 42 while the old refresh
    response is in flight. The version reaches two, refresh rejects, and every
    queue-item field remains unchanged, including its unfinalized comments.
  - A newer identical accepted observation blocks an older importer before it can
    consume issue pages.
  - An empty issue-page response cannot finalize a successful cursor after another
    accepted observation advances authority.
  - A stale operator name save retains the newer owner and authority version.
  Probe and log: `/tmp/vibearr-review05-r4-probes.py` and
  `/tmp/vibearr-review05-r4-probes.log`.

Tests used Python 3.12 from the shared read-only development environment and
review-owned database names on the designated disposable PostgreSQL server.
Django created and dropped its test databases. No live application, production
database, coding backend, host login or unrelated container was changed.

## Limits and handoff

Provider responses were controlled test data; this review did not perform live
GitHub imports or writes, paid/native-agent calls, delivery, merge, host runtime
qualification, or browser screenshot verification. UI templates and their existing
application tests were inspected. Those later execution and delivery capabilities
are outside feature 05.

Feature 04 must continue to bind checkout and delivery to the stable GitHub
repository ID. Source reconciliation refreshes the canonical project URL while
retaining already frozen checkout settings; an old URL by itself is not proof of
repository identity across a rename or transfer. This is a downstream integration
requirement, not a claim that native dispatch is implemented by this feature.
