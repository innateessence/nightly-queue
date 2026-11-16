# Feature 04 independent review, round 1

Reviewed parent: [PR 100](https://github.com/innateessence/nightly-queue/pull/100),
`feat/unattended-issue-to-pr`, source
`da734e52b9ab76f032ab72c91b942390c372cadb` against accepted main
`c56dfc756c8f157043cd87128fdc6f891081abc8`.

This review used a new full clone at `/tmp/vibearr-review04-r1`; it did not use
the author's checkout for execution or inherit a prior review decision. The
scope includes the entire feature diff, including the previously merged server
and runtime topics. There is **one P2 finding**. This report does not approve a
later source revision or clear the finding after a fix.

## P2: Retry transient GitHub reads before failing the owned task

Location: `vibearr_runner/native_lifecycle.py:463-475`, together with
`vibearr_runner/delivery.py:144-176` and the repository reads in
`vibearr_runner/native_lifecycle.py:535` and
`vibearr_runner/native_delivery.py:120-121`.

A temporary DNS/socket error on a GitHub GET becomes `DeliveryError`, which the
native lifecycle catches as a terminal task failure. A GET HTTP 5xx follows the
same path. There is no retry counter, backoff, or rate-limit header handling on
this path. This violates issue 68's transient transport retry requirement and
`docs/vibearr-mvp.md:275-276`, which requires at most three attempts with backoff
and GitHub rate-limit handling.

The impact extends beyond initial checkout: a brief GitHub outage after the paid
agent and all nine independent checks have completed discards the owned delivery
attempt as failed. An operator must explicitly retry the task, potentially
paying for the agent and validation again, although the existing candidate is
already verified. This is an ordinary availability failure, without any
adversarial input or external mutation.

I reproduced both the initial-context and post-validation cases with the existing
portable lifecycle fixture and the real `NativeGitHub.repository` /
`GitHubDeliveryClient.request` implementations. Only the HTTP opener is made to
raise `URLError("Temporary DNS failure")`; no provider requests are made:

```python
from unittest.mock import patch
from urllib.error import URLError
from vibearr_runner.native_github import NativeGitHub

repository = NativeGitHub.repository
from coding_runner.test_native_lifecycle import NativeLifecycleTests

case = NativeLifecycleTests("test_no_change_finishes_with_explanation_and_no_commit")
case.setUp()
try:
    case.advance("delivery")  # Omit to reproduce before the initial checkout.
    with patch.object(NativeGitHub, "repository", repository):
        with patch("urllib.request.OpenerDirector.open", side_effect=URLError("Temporary DNS failure")) as request:
            result = case.engine().step(case.state)
    print(result, request.call_count, case.queue.finishes, len(case.executor.started))
finally:
    case.doCleanups()
```

Both cases return `reporting_pending`, make exactly **one** transport attempt,
and send terminal `status: failed`, `outcome: failed`, with summary
`GitHub response unavailable; reconcile delivery before retrying.` The initial
case has zero started executor operations; the post-validation case has ten
(one agent and all nine validation commands). Local reproducible script and
output: `/tmp/vibearr-review04-r1-transient-repro.py` and
`/tmp/vibearr-review04-r1-transient-repro.log`.

Retain the original run, candidate, session and publication identity while
retrying typed transient reads, with bounded attempts/backoff and rate-limit
handling. Keep permanent authorization/identity errors distinguishable. Unknown
paid execution, push and PR mutations must continue through their existing
read-only reconciliation fences rather than unconditional mutation replay.

## Independent checks and reviewed behavior

- Full Django/PostgreSQL suite: **1,727 tests passed**, four distribution tests
  skipped because the shared application venv has no build tools. Used only my
  own database, `vibearr_review04_r1`, on the authorized disposable PostgreSQL
  instance. Log: `/tmp/vibearr-review04-r1-full-tests.log`.
- All four real distribution tests then **passed sequentially** in my own
  Python 3.12 build venv, with setuptools 80.9.0 and wheel 0.48.0. These built
  sdists/wheels and exercised isolated installs of the CLI, canonical and
  compatibility runners, optional executor SDK behavior, and executor resources.
  Log: `/tmp/vibearr-review04-r1-distributions.log`.
- Black 25.1.0, Ruff, mypy, migration drift and Django system checks passed on the
  reviewed source. Mypy checked 370 source files. Full-suite/artifact execution
  started on `2b067e56f94039a443852a9e676c2de5134f8bfa`; the only subsequent delta
  to reviewed `da734e52` collapses one test assertion for Black, which I inspected.
  The changed integration module was rerun on `da734e52`.
- Traced eligible-project selection before model preflight; selected account /
  version / runtime auth-proof reuse; durable preparation and dispatch presence;
  immutable source/context/plan/prompt; verified file import and recoverable Git
  commit; fixed committed-tree validation and dependency provenance; the shared
  two-repair limit and unchanged task/stage deadlines.
- Traced current server authorization before intents, exact version 2 delivery
  bindings, immutable operation acknowledgements, cancellation-compatible fact
  reporting, original push/PR reconciliation, explicit retry and late-retry
  fencing, ownership-loss suspension, termination before slot release, and
  acknowledgement-gated source/log retention. The ordinary regression suite
  covers these paths; I found no additional P0/P1/P2 in them.
- Inspected native status/context/plan rendering, validation outcome and repair
  count, heartbeat/wait details, delivered PR links, cancel and explicit retry.
  Existing source/credential redaction and historical legacy delivery readback
  remain part of the reviewed scope.

Commands used the independent clone and authorized interpreters:

```sh
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=32771 POSTGRES_USER=vibearr_dev \
POSTGRES_PASSWORD='<disposable database password>' POSTGRES_DB=vibearr_review04_r1 \
  /tmp/vibearr-tools/bin/python manage.py test --noinput
/tmp/vibearr-review04-r1-build/bin/python -m unittest vibearr.test_distributions -v
/tmp/vibearr-tools/bin/black --check .
/tmp/vibearr-tools/bin/ruff check .
/tmp/vibearr-tools/bin/mypy . --config-file mypy.ini
# With the same disposable database environment:
/tmp/vibearr-tools/bin/python manage.py makemigrations --check --dry-run
/tmp/vibearr-tools/bin/python manage.py check
/tmp/vibearr-tools/bin/python manage.py test runner_control.test_runtime_integration --noinput
```

## Actual proof inspection and limits

I independently recomputed the qualification algorithm's digest of current
executor Python sources:
`bb447de81c9a7956daf800981033c0325949e316126c81ac8d975bf7c4c74b6f`.
It matches the current 13-check boundary and protected-presence proof in
`docs/evidence/native-runtime-controls.json`, and the author's actual scoped
`/tmp/vibearr-feature04-acceptance/qualification-final.json` (also 13 checks).
I inspected the strict pause-acknowledged / still-paused counter measurement,
bounded fixed observer, deadline enforcement, selective retention, image
ownership and terminal tombstone implementation. Historical proofs for earlier
executor source digests are not treated as current-source qualification.

I read `/tmp/vibearr-feature04-acceptance/first-cancellation.json`: actual issue
99, run `6a16f1de-5db4-4f71-8f6c-0438715338c0`, completed agent operation
`fe0112b1-0cd1-41c0-853d-90630692a0c2`, provider session
`01a1e4e9-e586-7850-8d48-172696d14e66`. Its record shows pending retry with slot
retention until termination, one termination artifact, canceled/failed terminal
outcome, then requeue. This is inspection of the author's recorded proof; I did
not operate their service or database.

The second real issue-to-PR recovery acceptance was still in progress when this
report was written. I therefore do not claim to have independently observed its
final pushed ref, PR, all restart checkpoints or acknowledged delivery readback.
No extra paid model call, GitHub source publication, executor qualification run,
host-network probe, production stack change or author-service mutation was
performed for this review. Current actual runtime evidence was source-bound and
inspected rather than re-created in this review.
