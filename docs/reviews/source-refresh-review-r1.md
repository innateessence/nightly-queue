# Source-refresh review, round 1

Verdict: **no P0, P1 or P2 findings in the reviewed change**.

Reviewed source: `3c9710032e230524b5077b02c6573d5c9b994c29`, tree
`83ed401353b7b36db2f9e1862e4f62978b9774d8`, PR122. The comparison base is
accepted main `9dd2e8eb1245c7fcfac1e96e0d07348579ae6daa`. The diff contains
eight files, 388 additions and 25 deletions. Review used a new independent full
clone at `/tmp/vibearr-review-source-refresh-r1`; no author checkout, worktree
or live controller state was reused.

## Scope and behavior

Read the complete production and test diff, the operations documentation, and
the existing source refresh, authority comparison, HTTP client, native read
budget, checkpoint, resource control and termination paths used by the change.

The authority comparison still rejects a provider reply whose observation has
been superseded. Only a strictly advanced observation version with every other
authority field unchanged receives the typed transient classification. Changed
repository, owner, default branch or URL retains the ordinary rejection. The
existing identical-observation CAS regression remains in the passing suite.

The source endpoint catches that narrow exception after the transactional apply
has rolled back. The client recognizes its bounded JSON code only on a POST to
the source-refresh endpoint. Other conflicts, malformed or oversized responses,
authentication errors and ordinary source rejections do not become retry grants.

The supervisor recollects provider context through the existing durable read
budget: at most three attempts, persisted backoff, and no new paid operation.
It journals the desired paused state before issuing resource control calls, so a
lost pause response does not erase the later resume requirement. A successful
read resumes the same operation; original task and operation deadlines remain.
Exhaustion and cancellation use the existing termination and reporting paths.

Checked the actual launch and publication callers as well as the new tests.
Agent, dependency and validation starts, and Git delivery, retain their forced
source checkpoints. The ordinary cached checkpoint does not authorize those
side effects while the forced read is still waiting for fresh collection.

## Independent validation

| Check | Result |
| --- | --- |
| Full Django suite with PostgreSQL | 1,788 tests passed in 67.397s; four distribution tests skipped in the build-tool-free environment |
| Real distribution tests, run separately after discovery | Four passed in 73.697s; actual sdists/wheels and installed imports |
| Black | 377 files unchanged |
| Ruff | Passed |
| mypy | Passed for 377 source files |
| Fresh owned PostgreSQL migration through 0011 | Passed |
| Migration drift and Django system checks | No changes detected; no system issues |

The full PostgreSQL suite includes the separate-connection import/source-read
race, transactional non-mutation assertions, fresh recollection, owner-change
rejection and the older strict authority-fencing regressions. The lifecycle
tests cover same-operation restart/resume, lost pause response, persisted retry
limits, cancellation, deadline expiry and generic conflict rejection. Their
executor and provider fixtures establish protocol behavior, not live OCI or
model execution.

Distribution tests used a separate Python3.12 environment with
setuptools80.9.0 and wheel0.48.0. They ran sequentially after the full suite,
avoiding build staging directories entering discovery. All owned test and
migration databases were dropped. Logs remain under
`/tmp/vibearr-review-source-refresh-r1-evidence/` and the adjacent static-check
logs. No product source was changed during review.

## Evidence boundary

The earlier failed rehearsal captured an HTTP400 and its 102-byte response
length during a normal import; its response body was not retained. The author's
pre-fix deterministic PostgreSQL/API reproduction produces a matching 400 body
for an otherwise identical newer repository observation. I read that failure
log and independently ran the fixed PostgreSQL race tests. Attribution of the
original rehearsal to this exact exception remains an inference, rather than a
claim that its original response body was captured.

This review does not establish a successful live retry of issue119, complete
bootstrap acceptance, or the unattended merge loop. Those require their own
recorded execution and delivery evidence. No paid model calls, live application
or control-project changes, issue edits, provider-policy changes, merges or
additional deployment boundary probes were performed by this reviewer.

This report is committed on a separate branch based on `vibearr/reviews`; its PR
contains only this document. It does not change PR122's tested source head.
ROOT retains final source review and exact-head CI gates for both PRs.
