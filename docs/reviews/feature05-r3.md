# Feature 05 independent review R3

Reviewed PR #55 at exact source commit
`e5e9a03177ff1a5128c1c5f6f59fd1266ca3759d` against accepted main
`ff5864685083e5edccf9f737254a1b5a417bb5ba` on 2026-11-16. Review used the
independent full clone `/tmp/vibearr-review05-r3`. This report changes no
implementation files. **One P2 finding remains; this head is not clear to merge.**

## P2: fence repository authority observations across provider reads

Location: `work_queue/services.py:590–594`; the corresponding scheduled writer
is `work_queue/tasks.py:97–102`.

The final refresh transaction checks claim, runner, credential, project and issue
identity, but then unconditionally replaces the project's owner, remote default
and canonical URL with repository data fetched outside the transaction. It does
not check whether another operation accepted newer repository authority while
that response was in flight. A second provider read reduces the window without
fencing it.

A deterministic PostgreSQL probe reproduced this through the actual import
service, with controlled provider responses and no external requests:

1. Save repository 99 with owner 42, import open issue A by author 42, and claim it.
2. Begin source refresh and hold its second repository metadata response carrying
   owner 42. The issue response still has the frozen revision A.
3. Run `import_auto_issues()` with a newer repository observation carrying owner
   777. Assert one successful project sync and saved owner 777. Its issue-page
   fixture is empty, leaving the existing issue at revision A; the real importer
   similarly commits repository metadata before fetching/importing issue pages.
4. Return the older owner-42 response to the pending refresh.

Refresh returns an authorized source snapshot and restores saved owner 42. The
probe prints `AUTHORIZED True OWNER_AFTER_ACTUAL_NEWER_SYNC 42`; the assertion
that owner 777 remains saved fails. Thus an accepted owner transfer can be undone
and the default author policy can authorize the former owner. The same missing
fence also covers remote-default protection and canonical URL observations.

Capture and recheck a repository-authority revision across the network interval
under the project lock, before source, cancellation, comment or authority writes.
All trusted authority writers must participate, including scheduled sync and
fresh refresh. A delayed earlier operation must wait/reconcile rather than undo
an observation accepted by a newer operation. Add a regression exercising both
writers through their services and preserving the newer state on conflict.

The original isolated probe and the stronger actual-service probe are outside
the checkout at `/tmp/vibearr-review05-r3-probe.py` and
`/tmp/vibearr-review05-r3-service-probe.py`, with matching `.log` files. Both fail
on the reviewed head as described. The finding was sent to the implementer and
root before publishing this report.

## Scope and independent validation

Read the full implementation diff, feature 05/MVP contracts and operational docs:
provider parsing and bounded reads, conditional pagination/backoff, project
policy and save behavior, import/claim/dependency/branch/retry services, source
refresh API, runner configuration and snapshot/history wiring, schemas and
migrations, forms, templates, and regression/concurrency tests. Also examined
controller, credential and settings locks used by the changed paths.

The earlier fixes are present: unclaimed branch directives do not reserve other
work's branches; both configured base and remote default are protected; stale
ordinary project saves retain server-owned state; old claims and changed runner
identity/proof are checked before applying fetched data; each new run retains
its source snapshot; oversized dependency numbers are bounded before conversion.
The remaining finding concerns a newer accepted repository observation, rather
than those already covered stale-claim and stale-issue-revision cases.

- Python 3.12 full application suite against the reviewer's disposable PostgreSQL
  database: **1,373 tests passed, 3 expected distribution-environment skips**.
  Log: `/tmp/vibearr-review05-r3-full.log`.
- Full-repository Black, Ruff and mypy: **passed**; mypy checked 282 source files.
- Migration drift check and Django system check: **passed**.
  Static log: `/tmp/vibearr-review05-r3-static.log`.
- Additional independent PostgreSQL probes: **the one finding reproduced in two
  probes**, including the actual scheduled import service interleaving above.

Tests used only review-owned database names on the disposable development server.
No live application, production database, external coding backend, GitHub issue
import, delivery or merge was exercised. UI changes were inspected in templates
and application tests; no independent browser session was run. Later source
changes require a fresh independent review, even if this report's CI passes.
