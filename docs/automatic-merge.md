# Automatic PR merge

> **Docker-only runtime requirement:** Follow the [Docker-only runtime plan](docker-only-runtime-plan.md)
> and [deployment contract](mvp-deployment.md). Every Python, Git, coding CLI,
> packaging, test, qualification and browser command in this reference executes
> inside its appropriate container, never on the host. Host-service/daemon-launcher
> procedures and their old proofs are historical and cannot qualify the new worker
> boundary. Missing container tools must be implemented in D1–D4; no host fallback.

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
  "reviews": []
}
```

AI review gates are optional and disabled for the MVP (`reviews: []`). They are
not needed to claim, validate, merge, or complete a task. For this repository, all
four current CI workflows are configured requirements; passing only the plan's
two minimum workflows cannot excuse a failure in another required workflow.

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
checks and removing its bypass actors. It does not
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

## Optional review integration

The MVP leaves `reviews` empty and publishes no review statuses. Optional external
review producers can be configured explicitly for another policy; their exact
head/report/producer checks apply only when selected. Background AI review and
review-purpose image hardening are preserved separately on the future review
branches and are not an execution, image or qualification prerequisite for this
CI-only MVP.

## Exact candidate and recovery

Every required check must be the current completed successful job from the
configured workflow ID/path and GitHub App. The runner verifies check ID, suite,
workflow run/attempt, exact repository/PR/head and tested base. Missing, pending,
skipped, canceled, stale, duplicate, superseded or unexpected-producer checks
cannot merge. Optional configured review statuses must bind the same exact run,
context, base, head and tree; an empty review set adds no review gate. The server independently rechecks provider
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

Feature06's ordinary-controller driver is extended for merge mode. Use
`python -m vibearr_runner.self_acceptance --scenario merge --execute` with the
same selected state/project and both finite issue IDs; `merge_acceptance` also
provides the dedicated entrypoint. Resume the same journal and receipt files
after interruptions. Supply `--screenshots` only after the trusted operator has
rendered each exact validated candidate in an owned app with installed Chrome;
the native agent must not download a browser or fabricate screenshots.

Reserve two
distinct finite UI source tasks: A adds queue status filtering with every status
and malformed-input tests; B depends on A and preserves status in filter links
and clear behavior with regression tests. Do not preimplement these tasks or use
documentation-only changes as the merge-loop proof. Root reviews the finite issue
bodies before publication and can exclusively delegate those two app merges to
the controller after current no-bypass policy and required-check preflight.

The report requires genuine native Codex sessions, all nine isolated checks,
all four trusted exact-head remote checks, one PR/merge per
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
