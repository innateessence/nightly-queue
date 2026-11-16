# Feature 04 independent review, round 5

Reviewed executable source: `c3ed8743e6ca39d4aaab7fae759f78c5c682f93b`.
Reviewed source tree: `7a0c15833202adb7a5ec480c9725988ec96fa8cc`.
Comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Topic: [PR 100](https://github.com/innateessence/nightly-queue/pull/100),
`feat/unattended-issue-to-pr`, feature 04 / issue 68.

**Outcome: changes required — one P2 finding.** This is a new independent full
feature review after the round 4 tracked-launch correction. It does not clear a
later correction or authorize feature merge. A different fresh reviewer must
review the complete feature after the P2 is corrected.

## P2: ordinary prelaunch pauses exhaust the remote-read retry budget

Location: `vibearr_runner/native_reads.py:25-28`, called by
`SupervisorGit.command` / `_command` in `vibearr_runner/delivery.py`, through
`NativeDelivery.step` and `processes.run`.

The durable wrapper increments the read attempt count before invoking the
transport. An ordinary pause arriving at the Git subprocess's prelaunch
checkpoint raises `BackendInterrupted` before `popen`, but leaves that increment
saved. Three such pause/resume cycles consume the complete three-attempt
allowance without one remote read starting or one transport failure occurring.
On the next resumed cycle, `NativeReadExhausted` fails the already implemented,
independently validated task. This violates the expected pause/resume behavior
and misreports operator controls as exhausted transport failures.

Independent full-lifecycle reproduction:
`/tmp/vibearr-review04-r5-read-controls.py`, output
`/tmp/vibearr-review04-r5-read-controls.log`. It starts with the existing local
Git lifecycle fixture and completes its one agent plus all nine validation
operations. Delivery uses the actual `NativeDelivery`,
`SupervisorGit(read=engine.read)`, durable read wrapper and `processes.run`.
The fixture substitutes queue/executor/GitHub facts; actual local Git commands
run. At each `ls-remote` entry the simulated operator pauses processing, and
the real subprocess prelaunch checkpoint rejects it. An assertion rejects any
unexpected remote-read `popen`. Each following cycle reloads the same journal
and resumes controls. Exact-source output:

```json
{"attempts_by_cycle": [[1], [2], [3], [3]], "executor_operation_count": 10,
 "failure": "Remote read failed after three bounded attempts.",
 "read_entries": 3, "read_spawns": 0,
 "results": ["wait", "wait", "wait", "reporting_pending"]}
```

The queue receives one failed finish with that error, despite zero dispatched
remote reads. No new paid operation or publication occurs.

Only positively known rejected prelaunch work should leave the read budget
uncharged. Do not refund every `BackendInterrupted` or API exception: the same
process helper also checks controls after a child has launched or returned.
Real and uncertain transport attempts must retain their durable accounting;
process/clone/publication identity and original deadline fences must remain.
Regression coverage should use this actual lifecycle/transport/checkpoint path,
including repeated prelaunch pause/resume, postlaunch interruption and a real
three-failure exhaustion case.

The author raised this concern before the source freeze; this reviewer then
independently established the complete-path reproduction and P2 assessment.

## Full review and previous correction

Used a new full clone at `/tmp/vibearr-review04-r5`, fetched the exact committed
source into that clone, and executed only there. Read repository instructions,
feature 04's plan, MVP, native queue and execution/evidence contracts. Historical
server/runtime and earlier feature review reports were context, not inherited
clearance.

Reviewed the full feature surface against the accepted base: normal controller
source-eligible selection and claim; selected account/model/credential/runtime
preflight; frozen source/context/plan/prompt; stable repository authority;
isolated native dispatch and exact-operation recovery; verified file export,
trusted import and recoverable commit; all nine independent fixed checks and
derived dependency provenance; bounded repairs and original deadlines;
journaled push/PR publication; server version 2 evidence; protected termination
before claim/slot release; pause/cancel/retry/ownership loss; immutable instance
and presence binding; retention/tombstones; optional SDK distribution behavior;
Git-free application guards and native run views. No additional P0/P1/P2 defect
was established in this review.

Round 4's push marker ordering is corrected at this source. The callback reaches
the tracked child's launch gate after durable PID identity and the final local,
remote-control and deadline checks. A rejected unreleased worker is killed,
reaped and closed before the mutation marker is saved. The marker is durable
before release; uncertainty afterward retains publication ownership. The original
deadline also caps communication after checkpoint latency. Existing generic
process and delivery call signatures retain compatibility.

An independent ordinary functional check additionally used a real trusted Git
checkout, imported/committed candidate and local bare-repository push through the
actual tracked launch gate. It paused after worker creation, verified no dispatch
marker, no remote ref and stopped local processes, then reloaded the journal:

- Resume completed one real local push, one fixture PR and delivered finish,
  retaining the original one agent and nine checks.
- Cancellation completed canceled with zero actual pushes and zero PRs; all
  owned local processes remained stopped.

These checks are `/tmp/vibearr-review04-r5-controls.py` and
`/tmp/vibearr-review04-r5-controls.log`. They do not replace live OCI/model/provider
acceptance. The later read-budget P2 above still blocks feature clearance.

## Independent validation at the reviewed source

| Check | Result |
| --- | --- |
| Full PostgreSQL `manage.py test --noinput` | 1,763 tests, 65.005 seconds, OK; four distribution tests skipped in app tools without build packages |
| Full `python -m vibearr_runner.verify` | 811 tests, 66.145 seconds, OK |
| Actual standalone artifact tests, separately | All four `vibearr.test_distributions` tests passed, 72.835 seconds; own Python 3.12 environment with setuptools 80.9.0 / wheel 0.48.0 |
| Black / Ruff / mypy | Passed; 377 Python source files |
| Own database migrations / drift / system checks | Migrations applied; no pending changes; no system-check issues |
| Ordinary full-lifecycle tracked Git pause/resume/cancel | Passed as described above |
| Full-lifecycle repeated prelaunch read pause | Confirmed P2 above |

App tests used only `vibearr_review04_r5` / `test_vibearr_review04_r5` on the assigned
disposable PostgreSQL service. Logs are
`/tmp/vibearr-review04-r5-{full,runner,artifacts,black,ruff,mypy,migrations}.log`.
Only this report is committed; supplemental checks remain outside the repository.

## Actual acceptance evidence and limits

Read the public issue 99 acceptance record and its private captured delivery and
server readback without controlling those services. Independently verified:

- Original delivery SHA256
  `46c4e00f47678fc759b0efdd54825e2638745181de7ec4e51cd9c255078ed16a`;
  the public `result` exactly equals the private delivery document.
- Captured server readback SHA256
  `19a84da4ebc8937041422205b5dd45f809c5d49306a4bf535eb2124c1f836ffb`;
  public version 2 delivery and termination exactly equal those server facts,
  finish digests agree and reporting acknowledgement is true.
- Genuine run `d913fa25-5e89-4725-8d24-b381d95d51fa`, session
  `01a1e51b-2843-78e1-af02-cb7d4278ffcb`, original generated commit
  `a44468bf797fd3d0dba0b7e39378ac9df4abf878`, tree
  `268c088c788109c9daebef22cc887c7324b9ab8b`, and PR 104 are preserved.
  Nine real OCI check receipts passed, no repairs; four restart checkpoints use
  distinct supervisor PIDs. The server capture contains the latest ten of
  eleven evidence records, so it does not establish complete intent history.

That paid successful run used immutable supervisor source
`24386a2d11df53e159be0b3e09e54658c31c7370`, not this review's source. Later push
changes are assessed by fresh source/functional checks; no fourth paid task was
run or claimed. The three recorded attempts retain their honest cancellation,
failed executor-registry restart and successful-delivery history. This is PR-only
proof; CI/review/merge and the later bootstrap two-issue/failed-validation live
scenarios are separate work.

Unchanged executor source and Q3/Q13 qualification evidence were inspected in
context; expensive OCI qualification was not repeated without a new runtime
concern. No paid provider call, source/provider write, live service control,
credential/journal mutation, adversarial/exploit probe, original workspace edit,
or merge was performed. Report publication checks and parent-source provenance
are recorded separately by root; root alone merges review PRs after its own
exact-head checks.
