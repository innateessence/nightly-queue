# Verified pull request delivery

Feature 10 adds `coding_runner.delivery.DeliveryStage` and strict owned-run
completion evidence. Execution remains disabled pending the controlled release
checks in feature 12. No live task repository or paid backend call was used to
qualify this feature.

The supervisor receives feature 09's `delivery_ready` evidence, the saved plan,
the frozen workspace, and a selected repository credential from feature 03.
It never executes an agent-provided Git command, trusts an agent-provided PR URL,
merges a PR, or closes an issue. Project commands still require feature 06's
single-use human approvals. Automatic supervisor Git publication is part of the
explicitly enabled runner workflow; saving a plan does not approve commands.

`DeliveryStage(journal, client, github, repository_git=..., gate=...)` exposes
`step(state, run_id, workspace, evidence, plan_document, claim_token)`. The gate
callback must fetch/evaluate current global controls, including reserve and pause;
a missing/closed gate returns `wait`. The stage holds the workspace lock while
checking validation, committing, publishing, and recording delivery. It imports
feature 09's exact-content verifier with `already_locked=True`; a commit may
change HEAD metadata only, with the full validated content and base unchanged.
Validation is never rerun by this stage. Changed files require returning to
feature 09's explicit revalidation path.

The stage returns `finished`, `wait`, `failed`, `reconciliation`, or
`reporting_pending`, with the known `pull_request` and concise `error` when
available. The recovery coordinator reports known failures through owned finish,
preserving an existing PR URL. An uncertain operation retains its claim and
requires reconciliation. It must never send a failed outcome merely because a
successful delivery's finish response was lost.

## Durable operations

The existing atomic/fsynced journal stores `state.delivery`, bound to `run_id`:

| Phase | Recorded evidence and restart behavior |
| --- | --- |
| `ready` | Stable operation UUID before delivery work |
| `commit_pending` | Intent records parent, exact staged tree, and a supervisor-generated message containing the delivery UUID |
| commit observation | Record actual `commit_sha`; after a lost local observation, accept only the exact intended parent/tree/message |
| `push_pending` / `pushed` | Intent precedes non-forced push; `ls-remote` verifies the actual branch SHA. A retry recognizes an already pushed SHA |
| `pr_pending` | Record creation uncertainty before POST. Query actual repository/head/base before creating |
| `pr_verified` | Store GitHub's number, canonical URL, repository, head, base, and actual head SHA |
| `evidence_pending` | Upload strict `verified_delivery` artifact using the same operation UUID on retry |
| `delivered` / `reporting_pending` | Reconcile owned run and retry completion reporting only; no Git, model, or validation work |
| `finished` | Confirmed matching successful result |

An ambiguous PR POST is never blindly repeated. Lookup must discover the same
open PR; if lookup remains empty, creation stays uncertain for operator recovery.
Multiple matching PRs, closed PRs, remote divergence, unexpected commits, and
changed validated content stop publication. Existing PR text and branches remain
intact. A successful creation uses a draft PR containing the problem,
implementation, passing validation evidence digests, and original issue link.

## Server trust boundary

A `verified_delivery` artifact is accepted only from the separately authenticated
owner of an active run and its current claim token. This is a trusted supervisor
attestation boundary, not a proof accepted from arbitrary issue text, a generic
API token, an agent URL, or an `execution_ref` string. The server has no task Git
checkout or GitHub credential; remote inspection occurs in the supervisor.

Server validation checks the current saved plan/context and handoff, frozen
repository/head/base, actual pushed commit and commit-tree SHA, canonical GitHub
PR identity, nonempty validation bound to the exact workspace/base/content/diff,
zero integer exit codes, uninterrupted execution, and required plan/configured
checks. Each check must reference a consumed human-approved action execution on
this run with the exact action binding. Configured string commands must match
`/bin/sh -c` actions and the authoritative frozen configuration digest. The
commit/tree and remote SHA are supervisor observations; the server checks their
consistency and shape and does not independently contact GitHub.

Owned success revalidates this evidence and requires its exact PR URL. Legacy
`delivery: {verified: true}` or free-form validation artifacts cannot authorize
success. Generic queue clients retain their existing success behavior, and they
cannot finish runner-owned work. No migrations or new environment settings are
required.

## Credentials and network

`SupervisorGit(token=...)` receives only the selected repository credential.
Its clean subprocess environment contains no token, API credential, or claim
secret. A private anonymous temporary descriptor outside agent-visible files
feeds a fixed Git helper through `/proc/<supervisor>/fd/<descriptor>`. Git receives
only the helper path, descriptor identity, and repository scope. The helper
responds only for HTTPS `github.com` and that repository. The descriptor closes
after each operation; secrets are absent from Git URLs, argv, journal, prompts,
and display errors. The agent's enforced filesystem/PID boundary must remain
active. `trusted_local=True` is a deterministic acceptance-test transport and
must not be enabled for a real task.

Git disables hooks, filesystem monitors, ambient credentials, SSH, and redirects;
it pushes an explicit SHA to the bound branch without force. The GitHub client
uses selected-token headers, disables proxies/redirects, fixes the API origin,
and limits request/response sizes and request time. Wrong repository, head, base,
revision, URL, closed PR, and incomplete/ambiguous lookup are rejected.

## Verification

Run application/API checks normally and the Git-dependent tests explicitly:

```sh
python manage.py test runner_control.test_delivery runner_control.test_api
python -m unittest coding_runner.test_delivery_http
python -m unittest coding_runner.acceptance.test_delivery
```

The acceptance directory intentionally has no `__init__.py`; the application
image does not install Git. Tests use real temporary bare repositories and stub
GitHub/queue transports, covering commit observation loss, new files, disabled
hooks, push failure and divergence, existing PR reuse, stale validation, global
gates, unknown creation, and reporting-only finish reconciliation. Real local
HTTP tests check token headers, redirects, malformed/oversized responses and
uncertain mutations; the credential helper is exercised with an actual private
descriptor and rejects other repositories/hosts. Django tests reject false or
unbound delivery and validate configured string commands and consumed approvals.

Production Docker's namespace probe remains blocked by its host policy (feature
06). The designated external task repository/backend smoke and queued-to-PR
release scenario remain unverified. These deterministic checks do not authorize
normal execution or claim live integration acceptance.
