# Feature07 server review, round1

Verdict: **one P2 finding; implementation clearance withheld**.

Reviewed source: `44cf68af8fe169ec4ca7f8d5da008e5007add534`, tree
`4b2362201854c3d0fc5b4235761adcc707f2440d`, PR114. The child diff starts at
parent snapshot `76e2bf5fd4ff889ccf0f2efe41fb772ccf8ff7e9` and contains eleven
files, 1,690 additions and36 deletions. Review used a new independent full clone
at `/tmp/vibearr-review07-server-r1`; no worktree or author test environment was
reused. This report describes that source even if a later commit fixes it.

## P2: permit the remaining local repair after a base revision fails validation

Location: `runner_control/native_services.py`, the published-CI-repair check in
`validate_intent_binding`, lines644–656 at the reviewed source.

The condition tests whether **any historical** `native_delivery` contains a PR.
Once the initial candidate has a PR, every later agent repair must have a new
`ci_repair` revision intent for its exact preceding workspace. That requirement
also applies when the preceding workspace is an unpublished base-update
revision whose independent local validation failed.

Reproduced sequence through the real PostgreSQL-backed server services:

1. Complete initial native editing, validation and PR delivery at attempt0.
2. Authorize a normal base-update revision and record its inspected workspace at
   attempt1, preserving the original paid session attribution.
3. Register all nine validation operations for that revised tree and record
   their terminal receipts, with the CLI check exiting1. The server accepts the
   failed validation fact; the revised candidate is correctly never published.
4. Request agent repair2 from that revised workspace, with the original agent
   deadline and frozen `repair_limit=2`.
5. The server rejects it with
   `Published CI repair requires its explicit publication revision intent.`

Creating a `ci_repair` intent does not supply a valid way forward:
`publication_binding` requires a known `native_delivery`/PR for this latest
workspace, which has not passed validation and must not be pushed. The configured
second repair is therefore unavailable in an ordinary base-update failure path.

Distinguish local validation repair of an unpublished candidate from remote CI
repair of an already published candidate. Preserve the same PR/publication,
consecutive shared attempt limit, latest inspected source, original deadlines
and prior-operation termination checks. Add coverage for this sequence and for
a remote CI repair whose resulting local validation needs the second repair.

Reproduction files: `/tmp/vibearr-review07-server-r1-evidence/repair_repro.py`
and `repair_repro.log`. The isolated reproduction runs one test against a unique
disposable PostgreSQL database. Its intentional assertion fails at the guard
above after checking that validation is failed and the frozen repair limit is2.
Provider observations use the existing protocol fixture; this is a server
authorization reproduction, not a real GitHub merge or OCI execution claim.

## Scope and validation

Read all child production changes: configuration, merge services, native binding,
credential scopes, completion/finish behavior, settings/forms/validators, models
and migration0012. Also read the parent merge profile/review/receipt contracts,
provider exact-head/check/rules/review observations, native artifact contracts,
and the relevant existing native protocol and retention paths.

Independent checks on the reviewed source:

| Check | Result |
| --- | --- |
| Full Django suite with PostgreSQL | 1,832 tests passed; four distribution tests skipped in the intentionally build-tool-free environment |
| Real distribution tests, run separately after discovery completed | Four passed in74.951s; actual sdists/wheels and installed imports |
| Black |390 files unchanged |
| Ruff | Passed |
| mypy | Passed for390 source files |
| Migration0011→0012 on a separate owned PostgreSQL database | Passed; retained custom paused/disabled settings and polling, runner digest, native run status and project cutoff; unset merge profile and no fabricated fences |
| Migration drift and Django system checks | No changes detected; no system issues |
| Additional remaining-repair reproduction | Confirmed the P2 above |

The distribution environment used Python3.12, setuptools80.9.0 and wheel0.48.0.
It ran sequentially after the full suite, avoiding build staging directories
being discovered as tests. The migration smoke fixture was corrected to reuse
the pre-existing settings singleton and include the current project migration
state; those fixture setup errors were not product failures. All owned test and
migration databases were dropped. Logs and reproduction source remain under
`/tmp/vibearr-review07-server-r1-evidence/`.

No live app/configuration changes, provider policy writes, merges, paid model
calls or new deployment boundary probes were performed. The existing concurrency
test ran against real PostgreSQL. This review does not establish feature07's
final integrated runner, repository-policy setup or two-issue automatic-merge
acceptance. A new independent reviewer must assess the fixed implementation;
passing the existing suite does not clear the finding.
