# Feature 04 independent review, round 3

Reviewed source: `24386a2d11df53e159be0b3e09e54658c31c7370`.
Accepted comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Feature: [PR 100](https://github.com/innateessence/nightly-queue/pull/100),
`feat/unattended-issue-to-pr`; issue 68. Review date: 2026-11-16.

**Outcome: one P2 finding; no P0 or P1 finding identified.**
This is a fresh independent review of the complete feature against the accepted
main, including the server and runtime topics, rather than a review limited to
the round 2 clone correction. The reviewed source is not cleared. Any correction
requires a new fresh independent reviewer; this historical report does not clear
a later implementation. Root retains sole merge authority.

## P2: a pre-push read can permanently fence a push that never ran

Locations: `vibearr_runner/native_delivery.py:141-144`,
`vibearr_runner/delivery.py:405-406`.

After one successful empty remote-ref lookup, `NativeDelivery.step()` durably sets
`push_dispatched = True` and calls `SupervisorGit.push()`. That helper first calls
`observed_remote()` again, before executing the push command. If this second
read has a completed transient DNS failure, the safe-read wrapper raises
`NativeReadWait`. No push command has run. The next cycle's recovered successful
empty read enters the `push_dispatched` branch and raises
`ReconciliationRequired("Push response was lost and its ref is not visible...")`.
Every later cycle repeats this fence. The ordinary transient read has therefore
stranded an already implemented and validated task indefinitely as an unknown
publication, despite no mutation ever being dispatched. Cancellation cannot
resolve a dispatched push with an empty remote ref either.

The author first reported this adjacent issue while this review was in progress.
It was then independently reproduced in the reviewer's exact source clone using
real `NativeDelivery`, `SupervisorGit` and the durable-read wrapper, with only the
Git transport command response controlled. The reviewer did not execute the
author's reproduction script. The first cycle recorded two `ls-remote` calls,
zero `push` calls, and `push_dispatched=True`; two later successful empty reads
both produced the false unknown-push fence. No finish was reported. Independent
reproduction and output are `/tmp/vibearr-review04-r3-prepush.py` and
`/tmp/vibearr-review04-r3-prepush.log`.

Keep pre-dispatch safe reads outside the mutation dispatch boundary. Persist the
dispatch marker immediately before the actual push command, with remote
verification/readback reconciled separately. Preserve the same publication and
candidate identities and the existing conservative handling of a genuinely
dispatched push whose outcome is unknown. This report contains no code fix and
does not assess the proposed correction.

## Scope and findings

Used a new full clone at `/tmp/vibearr-review04-r3`, pinned to the source above.
Read repository instructions, feature 04's plan and MVP contract. Reviewed the
production diff and relevant existing controller, client, workspace, native
session, validation, executor and queue paths, their tests, packaging, CI and
operator documentation. Historical server/runtime reviews were context, not
inherited clearance.

Traced eligible-project selection and normal claims; live selected-account
preflight; strict source refresh and stable repository authority; frozen source,
context, plan and prompt; original-operation agent recovery; verified export and
recoverable trusted import/commit; all nine independent checks and dependency
image provenance; exact committed source/tree and publication; bounded repair
count and original stage/task deadlines; current ownership/policy/credential
checks; protected termination and delayed reporting; retry/cancellation fences;
resource presence/instance binding; retention and Git-free application packaging;
and the minimum existing-run UI changes.

The P2 above is the only P2-or-higher finding. The preceding read-retry and clone
findings are fixed on their direct paths: safe remote reads persist a
three-attempt budget and retry time, while
GitHub rate-limit headers can extend that time. Mutations retain their original
dispatch identities and reconcile rather than replay. A clone records its exact
repository/argv/owned path before dispatch, reconciles tracked local processes
before touching a partial checkout, and only deletes its registered incomplete
checkout after confirmed process recovery. A durably completed clone is reused
for remaining local setup. Unknown process state, an unregistered checkout or a
changed binding remains fenced. The legacy manager's default still refuses
incomplete checkouts.

The application tests now explicitly guard Git-dependent fixtures; the actual
Git-equipped runner verifier and trusted validation retain Git acceptance. Both
canonical and legacy distributions were really built and installed outside the
checkout. No optional SDK import was substituted with an availability flag.

## Independent validation

| Check at the reviewed source | Observed result |
| --- | --- |
| PostgreSQL-backed `manage.py test --noinput` | 1,748 tests passed in 65.316 seconds, with four expected artifact-environment skips |
| Git-equipped `python -m vibearr_runner.verify` | All 797 tests passed in 65.951 seconds |
| Real sdist → wheel → installed distribution tests | All four `vibearr.test_distributions` tests passed in 72.025 seconds, none skipped |
| Additional actual workspace/lifecycle cases | All ten passed in 0.838 seconds |
| `black --check .` / `ruff check .` | Passed; Black checked 376 files |
| `mypy . --config-file mypy.ini` | Passed on 376 source files; existing untyped-body notes only |
| Migration and system checks | Own database migrated, `migrate --check` passed, no migration drift, no system issues |
| `git diff --check` | Passed |

The full application suite used reviewer-owned database names
`vibearr_review04_r3` / `test_vibearr_review04_r3` on the assigned disposable
PostgreSQL service. Distribution cases ran separately and sequentially in an
independent Python 3.12 build venv with setuptools 80.9.0 and wheel 0.48.0, after
test discovery had finished. The initial drift check reported the not-yet-created
empty reviewer database; after creating and migrating it, the check was repeated
successfully with no warning.

The supplemental cases exercised the real `WorkspaceManager`, `NativeClone` and
`NativeLifecycle`. Only remote transport and executor/provider responses were
controlled fixtures; Git clone/checkout/commit actually ran against a private
local repository. They confirmed:

- A completed DNS clone failure with partial files resumes under the same argv
  and read budget, then reaches one agent and all nine exact-candidate checks.
- Three completed transient clone failures exhaust the budget and finish once
  without an agent call.
- Restart after the completed local task-branch checkout reuses the completed
  clone and does not clone again.
- Pause, cancel, original deadline and ownership loss do not launch another clone
  or agent; cancellation/deadline finish with accurate outcomes.
- Unknown local process termination, unregistered partial content and changed
  clone binding preserve the checkout and ownership fence.

These private supplemental files are
`/tmp/vibearr-review04-r3-functional.py` and its matching `.log`.
Real artifact output is `/tmp/vibearr-review04-r3-distributions.log`;
migration output is `/tmp/vibearr-review04-r3-migrations.log`.

## Deployment evidence and limits

The executor executable source is unchanged from
`0a2fc3fba47a44ea9fa6a32c61f5ada342e647a2`.
Independent Git comparison found no `vibearr_executor/` difference to the reviewed
feature source. Inspected the existing actual 21-profile qualification proof at
`/tmp/vibearr-feature04-acceptance/qualification-final.json`, whose SHA-256 is
`f403adcec10994c15cd27463bb8cc87e472ec4b9225675da49dde4539e5ca82e`.
Its source binding is
`bb447de81c9a7956daf800981033c0325949e316126c81ac8d975bf7c4c74b6f`;
both the common agent image `sha256:ef056d326350ef92e29f109f1d7528f9c419c1182d2518314a9b145291f79888`
and validation image `sha256:624110a15c30119a4eb13c3b4b3980d6c8be9caa47fd3dfa6f6ec349706d92dd`
have all 13 required Q3 checks. This reviewer did not repeat the unchanged
expensive deployment qualification or count it as independent new execution.
Independently recalculated the binding over all 32 non-test executor Python
files using the qualification algorithm and obtained that same source digest.
Qualification's `native_agent_ready: false` is not live selected-account proof;
normal preflight and the real session must supply that separately.

The review used ordinary functional, regression, packaging and source checks.
It did not run a paid model, publish source changes, mutate the author acceptance
database/service/journal, change the original workspace, perform adversarial or
host-network probes, or merge a PR. Only this report is committed. Server and
executor fixtures are not a substitute for actual complete issue-to-PR acceptance.

At report time the third real acceptance attempt was still active, on immutable
source `24386a2`, run `d913fa25-5e89-4725-8d24-b381d95d51fa`, native operation
`01c6bf11-d19c-476a-8d60-2f8e7f92fe7d`, genuine session
`01a1e51b-2843-78e1-af02-cb7d4278ffcb`. Final delivery proof did not yet exist;
this report does not claim actual issue-to-PR acceptance passed. A prior running
UI screenshot was visually inspected at
`/tmp/vibearr-feature04-native-running.png`; it shows the second attempt and is
not proof of final delivery by the third attempt. Earlier canceled/failed
attempts and root's bounded third-attempt authorization remain preserved by the
author. This reviewer did not control any of these live attempts.

On the exact reviewed head, [Tests](https://github.com/innateessence/nightly-queue/actions/runs/43903138177),
[Executor boundary](https://github.com/innateessence/nightly-queue/actions/runs/43903138318)
and [Native Codex image](https://github.com/innateessence/nightly-queue/actions/runs/43903138383)
were successful; [Vibearr validation](https://github.com/innateessence/nightly-queue/actions/runs/43903138179)
was still running. The Tests log independently confirmed the intentional
Git-free application image passed 1,748 tests with 44 guarded skips, the separate
real distribution job passed all four cases, and the Git-equipped runner image
passed all 797 checks. That log is
`/tmp/vibearr-review04-r3-ci-tests.log`. CI success cannot clear the ordinary
functional P2 reproduced above. Root must inspect all required checks at the
exact report head before any serial merge. The reviewer-owned test database was
destroyed by the suite and the empty reviewer database was dropped afterwards.
