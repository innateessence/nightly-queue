# Feature 04 independent review, round 4

Reviewed source: `7c1169d979e0fc2ac3bab6e1ac04574a07327555` (PR 100).
Comparison base: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
Reviewed source tree: `c2226e792db3141a93d53afaba477351ebfc06aa`.

**Outcome: changes required — one P2 finding.** This fresh review covers the
complete feature, including the server/runtime integration and the R3 push
correction. It does not clear this source for merge. A different fresh reviewer
must assess the complete feature after correction.

## P2: a pause before Git process launch permanently fences an unstarted push

Location: `vibearr_runner/native_delivery.py:141-144`, through
`vibearr_runner/delivery.py:337-343` and `vibearr_runner/processes.py:165-176`.

The R3 correction removes the redundant remote read from `push_once`, but the
called transport still executes `processes.run`. That function performs its own
local and controller checkpoints before `popen`. If processing pauses at this
checkpoint, it raises `BackendInterrupted` before starting any Git process.
`NativeDelivery` has already persisted `push_dispatched=True`. The lifecycle
handles the interruption as a normal wait and preserves that marker.

After processing resumes, an empty remote ref is therefore treated as a lost
push response. The run cannot publish, and cancellation cannot establish its
publication termination: it retains the global claim and active slot indefinitely
despite no push process ever having started. A temporary queue outage or a
deadline failing at the same prelaunch checkpoint has the same ordering problem.
This breaks ordinary pause/resume and cancellation after otherwise successful
agent execution and all nine validation checks.

Independent full-lifecycle reproduction at the exact reviewed source:
`/tmp/vibearr-review04-r4-push-pause-lifecycle.py`, with output in
`/tmp/vibearr-review04-r4-push-pause-lifecycle.log`. It uses the existing native
lifecycle executor/queue fixtures, a real local Git checkout, verified import,
real candidate commit, all nine fixture validation checks, real `SupervisorGit`
transport preparation, and the real `processes.run`/tracked process boundary.
Only provider readback is fixed to the known empty remote, and an ordinary pause
is introduced immediately before the push transport's checkpoint. A spawn
observer confirms that no push process is started. After journal reload and
resume, then cancellation, the output is:

```json
{"cancel": "Publication outcome is unknown; cancellation retains its claim and slot.", "dispatch_persisted": true, "finish_requests": 0, "first": "wait", "native_starts": 10, "push_entries": 1, "push_spawns": 0, "resume": "Push response was lost and its ref is not visible; retain publication ownership.", "validation_checks": 9}
```

A smaller direct publication reproduction is retained at
`/tmp/vibearr-review04-r4-push-pause.py` and its matching `.log`.
No network mutation or paid invocation was used for either reproduction.

Bind dispatch to the actual tracked launch boundary, after all known prelaunch
checks, or preserve typed proof that an interrupted operation never launched.
Cover pause, transient queue failure, cancellation and deadline rejection before
spawn, and preserve the genuine unknown-outcome fence after an actual launch.
Clearing dispatch on every interruption would be unsafe because an interruption
can also occur after a process has published remotely.

## Full review scope

Used a new complete independent clone at `/tmp/vibearr-review04-r4`. Read
`AGENTS.md`, feature 04 in `docs/autonomous-ai-queue-plan.md`, the MVP execution,
validation, recovery and acceptance contracts, native queue/runtime documentation,
and historical supporting reports. Historical clearance was not inherited.

Reviewed the complete changed-file set and relevant surrounding code for normal
controller candidate selection and claims; live account/model/profile preflight;
frozen source/context/plan/prompt; current policy, credential and source authority;
stable repository identity; clone/process recovery; native invocation and verified
export/import; recoverable candidate commits; all nine fixed exact-tree checks
and derived dependency provenance; shared repair accounting and original
deadlines; push/PR mutation identity and read-only reconciliation; server v2
receipt relationships; protected termination before completion/slot release;
pause, cancellation, explicit retry and ownership loss; executor instance and
operation presence; build controls; selective retention and tombstones;
standalone/optional-SDK packaging; and minimum run/context/plan visibility.

R1's direct GitHub read retry and R2's owned-clone retry/recovery changes are
present. Reads have durable three-attempt/backoff records, including GitHub
rate-limit times; successful reads clear their own records. Unknown mutation
outcomes remain distinct. Partial clone cleanup requires original tracked-process
recovery and its exact owned destination, while completed clones resume local
setup. R3's pre-read DNS case is covered by the new regressions, and the push
argv remains the original non-forced exact-commit push. The remaining prelaunch
control boundary above is not covered by those completed-process mocks.

No additional P0/P1/P2 defect was established. A real queue-client source-refresh
POST HTTP 503 remains an ambiguous transport result handled by controller
pause/reconciliation; it is not a generic terminal `APIError` failure.

## Independent validation at the reviewed source

| Check | Result |
| --- | --- |
| Full PostgreSQL `manage.py test --noinput` | 1,750 tests in 63.986 seconds, OK; four distribution tests skipped because shared app tools lack build packages |
| Actual distribution artifact tests | All four passed in 72.780 seconds in an own Python 3.12 venv with setuptools 80.9.0 and wheel 0.48.0; canonical runner, compatibility runner, executor and CLI sdist/wheel/install paths exercised sequentially |
| Full `python -m vibearr_runner.verify` | 799 tests in 64.944 seconds, all passed, including Git-equipped acceptance |
| Git-availability guard simulation | All 30 selected native lifecycle/workspace/supervision tests skipped cleanly after `shutil.which` returned unavailable before import; this is a guard simulation, not actual Git-free image execution |
| Black / Ruff / mypy | Passed; Black and mypy checked 376 source files |
| Migrations / drift / system | All migrations including 0011 applied to own database; no model drift; no system-check issues |
| Whitespace | `git diff --check c56dfc7...HEAD` passed |
| Supplemental full lifecycle pause before push spawn | Reproduced the P2 above with zero push spawns, nine passed checks and no completion/claim release |

Logs: `/tmp/vibearr-review04-r4-django.log`,
`/tmp/vibearr-review04-r4-runner.log`,
`/tmp/vibearr-review04-r4-artifacts.log`,
`/tmp/vibearr-review04-r4-gitfree.log`,
`/tmp/vibearr-review04-r4-{black,ruff,mypy}.log`,
`/tmp/vibearr-review04-r4-{migrate,migration-check,system}.log`.

Tests used only reviewer-owned databases `vibearr_review04_r4` and
`test_vibearr_review04_r4` on the assigned disposable PostgreSQL instance.
The original workspace and author runtime resources were not modified. No paid
call, provider source mutation, deployment control, adversarial/exploit probe,
unrelated cleanup or merge was performed. Only this report is committed.

## Independently inspected real acceptance and its limits

The actual issue 99 proof was independently read and checked. The private
generated `/tmp/vibearr-feature04-acceptance/issue99-delivery.json` has SHA256
`46c4e00f47678fc759b0efdd54825e2638745181de7ec4e51cd9c255078ed16a`.
Its contents equal `result` in the public filtered
`docs/acceptance/feature04-issue99.json`. Its delivery equals the captured final
server `verified_delivery`, and public termination equals captured server
`native_termination` in `third-server-readback.json`. Current native evidence and
artifact validators accepted the recorded chain.

Run `d913fa25-5e89-4725-8d24-b381d95d51fa` completed with outcome `delivered`,
reporting acknowledgement true and finish digest
`65583dad5cec222a323518d510afc16a9ca7013bf2227b8f6076e25b7568cd81`.
All nine actual OCI check receipts have exit 0 for original commit
`a44468bf797fd3d0dba0b7e39378ac9df4abf878`, tree
`268c088c788109c9daebef22cc887c7324b9ab8b`; there were zero repairs.
Termination covers ten original executor operations, 46 stopped local processes
and two known-PR publication stages. The recorded 169 supervisor generations
include distinct PIDs at agent completion (generation 83), push (165), PR (166)
and reporting pending (167). The genuine native session is
`01a1e51b-2843-78e1-af02-cb7d4278ffcb`, CLI 0.169.0.

An independent read-only GitHub query found exactly one open, non-draft
[PR 104](https://github.com/innateessence/nightly-queue/pull/104) for branch
`vibearr/issue-99`, repository ID `1353963957`, base `main`, original expected
head `a44468bf797fd3d0dba0b7e39378ac9df4abf878`. Its filtered result is retained
at `/tmp/vibearr-review04-r4-provider.json`. Both actual run screenshots were
visually inspected: they show the running native session and acknowledged
delivered outcome with matching run/session/PR and validation evidence.

The successful paid run used immutable supervisor source
`24386a2d11df53e159be0b3e09e54658c31c7370`, not this final `7c1169d` source.
The later `push_once` change preserves the original positive push argv and has
nonpaid regression coverage, but the P2 above prevents current-source clearance.
The public report honestly retains all three attempts, including the first
operator cancellation and the second prepublication failure after an incorrect
profile registry was restored. No fourth paid attempt was requested or performed.

The unchanged executor source binding is
`bb447de81c9a7956daf800981033c0325949e316126c81ac8d975bf7c4c74b6f`.
Its earlier actual Q13 qualification and control evidence were inspected as
historical evidence; expensive OCI qualification was not repeated in this review.
The final feature still requires fresh full review after correction and root's
exact-head required-check gate. PR-only acceptance does not prove automatic CI
handling/merge or the later bootstrap/MVP live failure/repair release scenario.

## Provider checks and report integration

Required provider check results on the exact report head are recorded in the
review handoff after completion. They do not override this P2 finding. Root alone
may serially squash-merge report PRs after its own exact-head gate; this report
must remain a historical review of `7c1169d`, including after a later correction.
