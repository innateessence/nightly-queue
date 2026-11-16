# Automatic PR merge

Automatic delivery uses the existing native execution policy's
`delivery_mode: "merge"`. The default remains `pull_request`. A merge-mode run
keeps its original claim from native execution through remote CI, merge, issue
reporting, and final acknowledgement. It does not request command, plan, or human
merge approval. A delivered PR, a merged change, and a deployed application remain
separate outcomes; automatic deployment belongs to feature12.

## Trusted project configuration

Set the project's `merge_profile` override through the existing runner settings.
An empty profile blocks merge mode. Register numeric repository, actor, workflow,
and producer identities from GitHub, not from issue text or the candidate branch.
The example below shows the self-development repository's observed IDs; verify
them again during setup and replace workflow digests with actual SHA256 values of
the accepted base's workflow files.

```json
{
  "schema_version": 1,
  "repository_id": 1353963957,
  "actor_id": 17239750,
  "ci_timeout_seconds": 1800,
  "review_timeout_seconds": 1200,
  "control_base": "vibearr/reviews",
  "delete_branch": false,
  "checks": [
    {
      "name": "Run test.sh",
      "app_id": 15368,
      "workflow_id": 398885048,
      "workflow_path": ".github/workflows/test.yml",
      "workflow_digest": "<SHA256 of accepted test.yml>"
    },
    {
      "name": "Validate candidate",
      "app_id": 15368,
      "workflow_id": 420053010,
      "workflow_path": ".github/workflows/vibearr-validation.yml",
      "workflow_digest": "<SHA256 of accepted vibearr-validation.yml>"
    },
    {
      "name": "Qualify OCI executor",
      "app_id": 15368,
      "workflow_id": 419888620,
      "workflow_path": ".github/workflows/executor-boundary.yml",
      "workflow_digest": "<SHA256 of accepted executor-boundary.yml>"
    },
    {
      "name": "Check native Codex image",
      "app_id": 15368,
      "workflow_id": 420002541,
      "workflow_path": ".github/workflows/native-codex.yml",
      "workflow_digest": "<SHA256 of accepted native-codex.yml>"
    }
  ],
  "reviews": [
    {
      "role": "independent",
      "context": "vibearr/ai-review-independent",
      "creator_id": 17239750,
      "report_repository": "innateessence/nightly-queue",
      "report_repository_id": 1353963957,
      "report_prefix": "docs/acceptance/reviews/"
    },
    {
      "role": "root",
      "context": "vibearr/ai-review-root",
      "creator_id": 17239750,
      "report_repository": "innateessence/nightly-queue",
      "report_repository_id": 1353963957,
      "report_prefix": "docs/acceptance/reviews/"
    }
  ]
}
```

Both AI review roles may use the existing trusted coordinator's GitHub identity.
They must reference distinct AI sessions and independently assessed reports.
Candidate runtimes receive no GitHub publication credentials. A review file or a
successful message written by the task agent cannot publish a trusted status.
The supervisor only reads review statuses; the separate coordinator publishes
them after independent and root AI review find no P2 or higher issues.
For this repository, all four current CI workflows are configured requirements;
the plan's Tests and validation workflows are the minimum contract. A failure in
another required workflow cannot be ignored because two minimum checks passed.

The provider gate requires effective active branch rules with no bypass actors,
strict up-to-date checks, and exactly the configured nonempty required contexts
and workflow producers. Unsupported effective rules and overlapping classic
branch protection stop with a diagnostic. Required human review rules cannot be
bypassed; configure the autonomous repository policy deliberately. An owner
account can be the delivery actor once all effective rules apply to it without
bypass. An ordinary dedicated identity is also supported. A token's `admin`
permission alone never proves an acceptable merge policy.

The originally inspected main ruleset26003400 had an empty required check set and
an always-bypass actor for repository admin role5. Those observations do not
qualify merge mode. GitHub native auto-merge being disabled is harmless: this app
uses the direct expected-head squash endpoint, with no bypass parameter.

## Explicit one-time policy setup

Prepare an owned0600 profile JSON and an explicitly selected owned0600 token JSON
with the single field `secret`. Keep both outside candidate checkouts and version
control. The tool never discovers host credentials. The default command performs
provider reads and prints the exact current and proposed ruleset documents:

```sh
python -m vibearr_runner.merge_setup --profile /private/merge-profile.json \
  --token-file /private/github-selected.json --base main --ruleset-id 26003400
```

Review that concrete preview against the current effective policy before using
`--apply --checkpoint /private/merge-setup.json`. Apply updates only the selected
existing repository ruleset, preserving its other rules, requiring the configured
checks and two AI review contexts, and removing its bypass actors. It does not
enable execution, change saved credentials, toggle GitHub native auto-merge, or
change other/inherited rulesets. The checkpoint preserves the original policy.
Run the same command again to reconcile and verify the observed applied policy;
an unknown write is never blindly repeated. Other effective bypasses or missing
permissions still block readiness and need their own reviewed setup action.

Merge preflight reads the current selected actor, numeric repository, workflow
IDs and base workflow digests, and enforced branch rules before a paid session.
Changing a trusted workflow/profile requires reviewing and registering its new
base definition. Candidate changes to required workflow definitions cannot
silently weaken their own merge checks.

## AI review machine interface

The built-in coordinator runs two genuine isolated native review sessions under
the original run's ownership, fixed aggregate review deadline and cancellation
controls. It publishes their protected receipts and reports without per-task
operator commands. An optional third `external_root` role can enforce the finite
goal's separate root AI handoff; ordinary installed profiles use the two built-in
roles. See the [review protocol](automatic-review-contract.md) for ownership and
terminal evidence.

Publish reports on a separate control/report branch and PR targeting the persistent
`control_base` (`vibearr/reviews`), never the application base. Never add review
report commits to an active app-owned candidate head. A report URL must identify
an immutable commit and the configured report path/repository. Each report is
finite JSON with these fields:

```json
{
  "schema_version": 1,
  "role": "independent",
  "run_id": "<owned run UUID>",
  "context_digest": "<frozen context SHA256>",
  "repository_id": 1353963957,
  "pull_number": 123,
  "head_sha": "<exact validated head>",
  "base_sha": "<exact validated base>",
  "tree_sha": "<exact validated tree>",
  "session_id": "<actual independent AI review session>",
  "reviewer": "<trusted coordinator/source identity>",
  "findings": [],
  "verdict": "no_p2_or_higher"
}
```

The root report uses role `root` and another actual AI session. Unresolved
findings can only be P3/P4. Preserve the independent review's actual evidence and
root assessment in the report PR; do not fill session IDs or verdicts from a
candidate assertion. The coordinator's binding JSON contains `run_id`,
`context_digest`, `workspace` (the exact v2 workspace), and `pull_number`.

```sh
python -m vibearr_runner.merge_review --profile /private/merge-profile.json \
  --binding /private/review-binding.json --role independent \
  --report-url https://github.com/OWNER/REPO/blob/COMMIT/docs/acceptance/reviews/report.json \
  --token-file /private/github-coordinator.json
```

The default verifies identity/report/PR and previews the status. After the trusted
coordinator accepts its own evidence, `--publish --state-file /private/review.json`
journals and publishes one exact-head status. Repeat the same invocation to
reconcile a lost response; a missing result after dispatch retains the checkpoint
and cannot create a duplicate. This publication path is separate from the
candidate runtime and the runner's completion-only credential client.

## Exact candidate and recovery

Every required check must be the current completed successful job from the
configured workflow ID/path and GitHub App. The runner verifies check ID, suite,
workflow run/attempt, exact repository/PR/head and tested base. Missing, pending,
skipped, canceled, stale, duplicate, superseded or unexpected-producer checks
cannot merge. Both trusted AI review statuses/reports must bind the same exact
run, context, base, head and tree. The server independently rechecks provider
authorization before saving merge intent and reads actual merged state before
recording dependency evidence.

Immediately before squash, source eligibility, current configuration and selected
credentials, claim/controller ownership, PR/base/head, provider policy and checks
are read again. GitHub's expected `sha` prevents a different head from merging;
strict enforced up-to-date rules and absence of bypass close a concurrent base
movement race. The API has no base-SHA compare-and-swap parameter.

When the base moves, the supervisor saves a publication-revision intent, merges
the new base normally into the delivered branch, records both parent SHAs, and
validates the new tree independently before another normal push. CI repair uses
a new genuine agent attempt from the preceding inspected workspace. Both consume
the same maximum two repairs as local validation failures. They retain one PR,
publication ID, frozen source/account, original paid-operation evidence and stage
deadlines. No force-push, rewritten history, reused old checks, or ad-hoc reset of
a terminal run is permitted. A conflicting base merge leaves the existing PR and
a diagnostic. The CI wait is bounded and its deadline does not reset on repair.

Merge dispatch is journaled once. A lost response, HTTP502, or HTTP405 may leave a
provider merge in progress for many minutes. The runner only rereads that exact
PR and retains claim/fence ownership until the actual result is established,
even after cancellation, pause or an elapsed task deadline. It cannot replace
the agent or count an open PR as proof that the request failed. Actual merged
state wins a cancellation race and is reported truthfully.

After provider verification, immutable `MergeEvidence` unlocks dependencies.
Issue closure alone never does. The runner then publishes one stable run-marker
comment, closes only the frozen source issue, optionally deletes only the exact
owned branch, and acknowledges completion before releasing the active slot.
Unknown reporting outcomes retain ownership and reconcile before retry. Manual
queue tasks finish with the verified merge and have no GitHub issue to close;
their optional branch cleanup is left to normal repository policy.

## MVP acceptance

Feature06's ordinary-controller driver is extended for merge mode. Reserve two
distinct finite UI source tasks: A adds queue status filtering with every status
and malformed-input tests; B depends on A and preserves status in filter links
and clear behavior with regression tests. Do not preimplement these tasks or use
documentation-only changes as the merge-loop proof. Root reviews the finite issue
bodies before publication and can exclusively delegate those two app merges to
the controller after current policy and review-producer preflight.

The report requires genuine native Codex sessions, all nine isolated checks,
trusted exact-head remote checks and both AI review sources, one PR/merge per
issue, provider-verified completion, real rendered screenshots, and B's initial
base containing A's actual merge commit. No manual merge or approval record may
substitute. Interrupted launch/validation/push/PR/merge/completion, real validation
failure, moving base/head, concurrency/source policy and operator-control rows
from the MVP contract must have actual receipts. Missing evidence fails rather
than generating an agent-authored checklist marked complete.

Unit tests cover protocol edge cases; the local Git acceptance test proves normal
base-update ancestry and invalidation of old validation. They do not themselves
prove a paid model session or provider merge. Until real acceptance is recorded,
do not label the complete autonomous MVP ready.

Provider references: [merge endpoint and expected SHA](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request),
[effective branch rules and rulesets](https://docs.github.com/en/rest/repos/rules),
[workflow run identities and attempts](https://docs.github.com/en/rest/actions/workflow-runs),
and [required status check behavior](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).
