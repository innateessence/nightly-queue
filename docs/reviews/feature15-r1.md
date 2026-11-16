# Feature 15 independent review, round 1

Reviewed source: `7a1e6b679195c4bddaa4edd337e610f7f15dd19f`.
Comparison base: `38ee11830d5e3dbf3e5c6f23da133322b71ac198`.
Candidate Git tree: `ffc21e7821f3603511785e54ed9aa2b207c022cf`.

**Outcome: changes required. Three P2 findings remain at this historical revision.**
This report does not clear subsequent fixes, the final workflow, or a later source
revision. The author acknowledged the first two findings and is preparing fixes.

## Findings

### P2 — A missing workspace export can still qualify a validation check

`vibearr_runner/native_validation.py`, `ValidationRunner.status()`, checks the
protected command outcome but does not reject `export_error`, require `artifact`,
or verify its source/export receipt before accepting a check. The worker records
an export failure separately from `exit_code`; `Runtime.harvest()` accepts a
result without an export artifact.

This occurred in the independent actual OCI run: the required `mypy` operation
exited zero with no artifact and
`export_error = "Rejected: Workspace export exceeds artifact bounds."` The runner
accepted that result and launched migrations, system checks, and Django tests.
An installed-package status reproduction also returned `passed` for all nine
zero-exit results after one contained an export error. Thus successful command
exit alone currently bypasses the required verified-export fence.

Require successful protected export evidence and verify its binding to the input
source and returned bytes before advancing or issuing a passing receipt. Ensure
ordinary tool caches do not make a valid candidate exceed the export limit.

### P2 — The type-check plugin can load from the candidate and exit early

`vibearr_executor/validation_task.py`, `main()`, imports `mypy.main` before
inserting candidate paths, but the configured `mypy_django_plugin.main` is loaded
later. At that point `/work` precedes the image packages on `sys.path`.

An independent Python 3.12/mypy 1.13 reproduction used the entrypoint's import
ordering and trusted `validation_mypy.ini`. A candidate plugin module was selected
instead of the installed plugin, and the type-check invocation returned zero
before reporting a deliberately incompatible annotated assignment. Importing
`mypy.main` first therefore does not preserve the configured plugin's trusted
origin. This reproduction exercised the installed toolchain on the host; it was
not repeated inside OCI before the review was interrupted.

Select the plugin from the image-owned toolchain before candidate paths become
available, and verify that the required type check cannot report success through
premature termination during plugin loading.

### P2 — The unchanged candidate fails required distribution tests

`vibearr_executor/validation_app.py` launches application tests with the clean
`/opt/application-python/bin/python` interpreter, while
`vibearr_executor/validation.Dockerfile` creates that environment without the
application/tool dependencies installed into it. The parent process explicitly
adds dependency paths, but the distribution tests launch fresh subprocesses.

The actual exact-revision Django check discovered 1,505 tests and failed four
distribution tests because those subprocesses could not import `setuptools`:
the canonical runner sdist, compatibility runner sdist, CLI sdist, and executor
sdist tests. The test run reported four failures and six skips. This prevents the
known candidate from satisfying the required profile even though setuptools is
pinned in the image's validation requirements.

Provide the required reviewed dependencies to these clean subprocesses while
preserving the interpreter/startup-hook boundary. Rerun the full required profile
against the corrected exact source revision.

## Validation actually performed

The review used a separate full clone, its own installed executor/runner wheels,
and a Python 3.12 virtual environment outside the checkout. Wheel installation
completed before tests were run. No implementation changes were made.

| Check | Observed result |
| --- | --- |
| Full main-to-source diff and surrounding validation/build/runtime/API/artifact code | Inspected; audit remained incomplete |
| Installed native-validation, dependency-build, and validation-profile unit tests | 45 passed |
| Actual host OCI qualification | Version 2, all 11 required boundary checks passed |
| Exact candidate Black, Ruff, mypy, migrations, system checks | Protected command exits were zero; mypy export failed as described above |
| Exact candidate Django check | 1,505 tests; four failures and six skips; exit 1 |
| Exact candidate CLI, runner, Git acceptance checks | Not executed after the Django failure; prepared operations were stopped |
| Full six-case validation acceptance and candidate dependency image acceptance | Not completed in this review |
| Final trusted-base GitHub Actions workflow | Not present at the reviewed revision; not cleared |

The actual run used validation image
`sha256:b8021bbc50938bd629c20a1216a30695332c2a7d071ca33b327ef6fa15f754bf`
and database image
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
The image was already built; this reviewer independently qualified and exercised
it, but did not rebuild it. Qualification and candidate operations used this
reviewer's own private state and disposable resources.

Local evidence retained for the author and root reviewer:

- `/tmp/vibearr-review15-r1-evidence.json`
- `/tmp/vibearr-review15-r1-state/qualification.json`
- `/tmp/vibearr-review15-r1-state/candidate/session.json`
- `/tmp/vibearr-review15-r1-state/candidate/django.log`
- `/tmp/vibearr-review15-r1-state/operations/` protected operation records
- `/tmp/vibearr-review15-r1-run.log`

## Review limits

A platform content flag interrupted further review. At the root reviewer's
instruction, no additional probes, fixtures, or security tests were performed;
this report records only results already obtained. The report is a historical
finding record, not complete security or release clearance. A fresh review must
inspect the final exact source and actual workflow and run ordinary validation
after the fixes. No native model call, production application/database operation,
or merge was performed by this reviewer.
