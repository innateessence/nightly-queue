# Human-merge factory acceptance

`python -m vibearr_runner.factory_acceptance` is a finite, resumable read-only
observer for the [factory plan](software-factory-plan.md). Run it in the qualified
four-container deployment's runner. It reads the control and GitHub APIs and
authenticated worker status. It never starts a controller, provider session,
browser or service, publishes an issue, merges a PR, enables auto-merge, closes an
issue, or changes repository rules.

Run the observer as the controller's UID `10001`, including checkpoint capture,
so its private journal and `/proc` process environment can be inspected:

```sh
docker compose exec --user 10001:10001 runner \
  python -m vibearr_runner.factory_acceptance --help
```

The queue token is read from the configured private credential file. Worker reads
use the configured worker URL and validated control-token file directly; observer
construction needs no controller mediation environment or manifest directory.

The implementation and synthetic unit tests are harness evidence. Actual paid
provider execution, four-container qualification, failure scenarios and two human
merges remain release prerequisites. No live acceptance was performed by adding
this harness. Missing, stale, fixture-only or unavailable proof keeps the report
unqualified.

## Freeze two actual source tasks

Choose two small unmet source changes with testable acceptance criteria. B must
explicitly contain `Depends on: #<actual A issue number>`. Publish those issues and
authorize any paid controller execution separately. Save a private owned `0600`
scenario JSON in the runner's operator-input volume:

```json
{
  "schema_version": 1,
  "project_id": 7,
  "repository_id": 9,
  "repository": "owner/repository",
  "base_branch": "main",
  "provider": "codex",
  "agent_version": "<actual pinned CLI version>",
  "connection_id": 90,
  "configuration_revision": "<authenticated configuration SHA256>",
  "source_revision": "<deployed factory implementation Git SHA>",
  "repair_limit": 2,
  "tasks": [
    {"issue_number": 10, "body_digest": "<canonical JSON SHA256 of exact issue body>",
     "acceptance_criteria": ["First unmet source behavior with tests"], "surfaces": ["ui"]},
    {"issue_number": 11, "body_digest": "<canonical JSON SHA256 of exact B issue body>",
     "acceptance_criteria": ["Extend A with a regression test"], "surfaces": ["ui"]}
  ],
  "required_checks": [
    {"name": "<required GitHub job name>", "app_id": 12, "workflow_id": 13,
     "workflow_path": ".github/workflows/test.yml",
     "workflow_digest": "<SHA256 of approved workflow bytes>"}
  ]
}
```

Use actual saved and provider identities. Do not reuse already implemented status
filter examples or documentation-only work. `source_revision` identifies the
deployed factory implementation qualification; the task repository's inspected
base and candidate identities are collected separately from each actual run.
`connection_id` is the selected positive numeric credential ID from the native
account contract. The collector checks its frozen version and each role's model.
Each frozen criterion must remain verbatim in the accepted intake and saved
specification, as a string or an identified criterion's `description`/`text`.
Frozen UI/security surfaces must remain present in intake and trusted routing.

```sh
python -m vibearr_runner.factory_acceptance prepare \
  --state /var/lib/vibearr/factory-acceptance \
  --scenario /run/acceptance/scenario.json --evidence-kind live
```

Preparation freezes the scenario digest, evidence kind and operation UUID.
Repeating it resumes the same identity. Fixture is the default evidence kind and
can never qualify a release; it cannot later be relabeled live. Live preparation
does not authorize or launch paid work. State and reports are private and bounded.

## Observe externally executed runs

Run A through the ordinary authorized controller independently. Supply its actual
run UUID to the observer with a private GitHub read-token JSON containing `secret`.
The runner entrypoint supplies the existing queue and worker credentials.

```sh
python -m vibearr_runner.factory_acceptance observe \
  --state /var/lib/vibearr/factory-acceptance --run-a ACTUAL_RUN_UUID \
  --github-token-file /run/acceptance/github-read.json \
  --stack-report /run/acceptance/compact-stack.json \
  --failure-evidence /run/acceptance/failure-matrix.json \
  --browser-evidence /run/acceptance/browser-evidence.json
```

The run must match the frozen project, issue, repository and configuration. Run
identities cannot be replaced on resume. An active run returns `awaiting_delivery`
without modifying its claim or worker. A delivered A returns
`awaiting_human_merge`. The human inspects and merges A while independent eligible
work continues normally. After that merge, run B through the ordinary controller
and observe its UUID with `--run-b`. Collection verifies B's claim time and
inspected base ancestry against A's exact human merge SHA.

Use `collect` with the same evidence arguments, omitting run UUID flags, to reread
the pinned runs. Each invocation finishes after bounded reads; it creates no
background loop. A closed-unmerged PR, bot merge, changed head, auto-merge,
incomplete current factory chain or unconfirmed termination fails collection.

## Evidence contract

Artifact lists contain summaries, so the collector pages ten entries at a time
and reads each immutable detail. It checks identity, metadata and canonical JSON
SHA256, rejecting redacted presentation documents. It reads the actual saved
contexts and PlanVersions and delegates current candidate readiness to the same
`checked_factory_workspace` authority used for publication. Provider, connection,
CLI, session, worker instance/boot, source, six-field dispatch specification,
aggregate role time and shared repair attempts remain bound across observations.
Bootstrap intake/specification use the explicit null/empty specification pair.

Saved native results and termination receipts must agree, and the collector
rereads original authenticated worker statuses. Old integrated-runner/fleet
receipts cannot qualify compact-worker execution. Required CI binds the exact
head, trusted App/workflow, pinned workflow bytes, current run/attempt, tested
base and completion; skipped, canceled, stale and failed checks do not qualify.
The authenticated termination API also binds every agent and validation manifest
to its original canonical digest and sealed result/boot receipt, including original
deadlines, limits, arguments and registered profile. Normalized result receipt
digests and sealed worker receipt digests have different inputs and are verified
separately.

The engineering input follows `compact_evidence.validate_compact_report`: exact
deployed source revision, four service/container/image identities, current
authenticated worker boundary and role permission proofs, and source-bound actual
validation, dependency, browser, backup/restore, resource, restart and runtime
distribution packaging receipt documents with verified canonical hashes. Its
fixture flags cannot be overridden by a `passed` boolean.

UI tasks additionally need browser evidence mapping run UUID to `report` and
`storage_root`. Worker PNG validation rereads the actual source-bound files and
original terminal operation. Screenshot names or booleans alone do not qualify.

The failure matrix requires actual evidence for failed-check repair,
review-request repair, exhaustion, missing capability, cancellation, controller
and worker restart, and independent work during human review. Missing prerequisites
are listed in `missing_prerequisites`; `qualified` remains false until all release
requirements and both genuine human merges are verified.

The `factory_scenarios` verifier reads actual saved run/context/native/artifact
history for each case. Failed-check and review repair require the original
protected failure, a later shared attempt and a complete ready delivery chain.
Exhaustion requires the configured allowance to be spent, actual failed rounds,
terminal failure and no publication. Cancellation requires protected requested,
confirmed and canceled-operation timing. Missing capability requires trusted
observed capability absence, recomputed routing, terminal failure and no
unauthorized role dispatch. It also checks failed branches have no provider PR.
The missing-capability case covers post-build required-capability absence or
loss, such as an optional security/UI role missing when trusted routing requires
it. Initial core preflight rejection has no saved intake/workspace and requires
separate preflight qualification evidence.

Supply a versioned failure input with the acceptance operation UUID from its
private state, and immutable case identities:

```json
{
  "schema_version": 1,
  "operation_id": "<acceptance operation UUID>",
  "cases": {
    "controller-restart": {
      "run_id": "<actual case run UUID>",
      "context_id": "<actual immutable context UUID>",
      "context_digest": "<context SHA256>",
      "evidence_ids": ["<all actual native evidence operation UUIDs>"],
      "artifact_ids": ["<all actual factory artifact UUIDs>"],
      "checkpoints": []
    }
  }
}
```

Use the same identity fields for every case. Checkpoints are objects returned by
`factory_scenarios.capture_checkpoint(journal_path, worker=read_only_worker(),
operation_id=acceptance_operation_id)`, using `read_only_worker` from
`vibearr_runner.factory_acceptance` inside the trusted runner as UID `10001`. The
finite producer reads the owned private journal, actual controller process and
worker statuses; it acquires no controller lock and starts no work. Save its JSON
privately. Retry capture if the controller changes its journal during the read.

Observe and persist each new checkpoint immediately, while its worker/controller
state still matches the actual runtime. The observer rereads those sources and
requires a fresh observation. Pin the before checkpoint before the external
restart; afterward append a separately captured after checkpoint and observe
again. Original checkpoint digests and run/context identities cannot be replaced.
`--controller-journal PATH` can select another mount alias of the actual registered
controller journal. The producer checks that it is the same file selected by the
running controller's state configuration; a copied or invented journal does not
qualify. Importing both historical snapshots after a restart cannot establish
their original provenance.

Controller restart verifies a changed actual process identity while retaining the
original operation, frozen deadlines and cumulative charges. Worker restart needs
original/new boots and authenticated original-operation reconciliation without
inventing a successful agent result. Independent-work proof derives A's released
claim and human-review interval from collected provider evidence and requires a
different eligible run to complete inside that interval before A's real human
merge. These checks can consume genuine future evidence; the actual paid sessions,
external restarts, cancellation and human merges remain operator-run release
requirements. Case labels, `passed` booleans and termination-only receipts cannot
remove any gate.
