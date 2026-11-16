# Native review protocol v1

The background review coordinator runs inside the original native supervisor/run.
It is a state-machine stage, not an untracked daemon with separate claims. Parent
owns `vibearr_runner` pure contracts, coordinator, GitHub and runner integration;
server child owns `runner_control` authorization, persistence and API tests.

## Profile and bounds

`merge_profile` adds required `review_timeout_seconds` (60..3600, initial1200)
and explicit `control_base` (`vibearr/reviews`). The control base must already
exist in the same repository and differ from the app base/task branch. ROOT
created its initial same-repository ref at accepted9dd; code never assumes main
as the control target or creates a branch as a retry side effect.

`reviews` contains exactly the built-in roles `independent` and `root`, optionally
plus `external_root`. Built-in reports use one repository/prefix and the selected
GitHub actor; the optional external role is an additional trusted AI goal gate,
not a required human workflow. Ordinary installed profiles use two roles and
progress without an external coordinator. The finite goal can require the third
role after its own fresh and primary AI review/all four CI checks. The background
native coordinator never manufactures that external attestation.

The original aggregate review deadline is saved at first review preparation:
minimum of original task deadline, original CI deadline, and now plus the frozen
review timeout. Every review role and repaired candidate uses that same deadline.
There is at most one paid operation per `(workspace_id, role)`, and at most
`2*(min(2, repair_limit)+1)` review operations per run. Review sessions do not
increment coding attempts or reset implementation/validation/CI/task deadlines.
They run serially after prior coding/validation operations are terminal.

## Review intent and result

`native_intent.kind = review` retains the existing context/policy/attempt envelope.
Its data fields are:

```text
workspace_id, workspace, pull_request, role, session, diff_digest
```

Workspace and PR are exact original native publication identities. Role is one of
the two built-in roles. Session is the ordinary `NativeSession`, with the selected
original account, registered runtime/profile, original run UUID, distinct operation
UUID and fixed review deadline. Its source digest equals the latest committed
candidate source digest. The frozen trusted prompt binds the diff digest, exact
base/head/tree/run/context and criteria; issue/repository/diff text is data and
cannot change commands, credentials, permissions or reporting policy. Full patch
input is bounded to512KiB; unsupported larger input fails before a paid start.

`intent_operations` returns the review's executor operation. Register it in the
original `NativeResources`, so pause/cancel/ownership loss, resource limits,
purge and terminal proof include it. An unknown paid outcome reconciles the same
session; there is no replacement operation or blind retry.

`native_review` fields:

```text
schema_version:1, intent_id, role, receipt, successful, session_id,
agent_version, usage, report, report_digest
```

Receipt is the protected ordinary executor summary. Usage has the same finite
numeric name/value/unit entries as native_agent. `successful` means a genuine
completed, untruncated native review with unchanged input/export source and a
valid explicit finite review result. It does not mean no findings. Missing,
invalid, truncated or failed output records no report (`report:null`, empty
report_digest), never an inferred pass. Report digest is canonical SHA256.

The model returns one explicit JSON object with exactly `complete:true` and
`findings:[{priority,description}]` (at most8, bounded descriptions). The trusted
supervisor fills the report's original run/context/base/head/tree/PR and actual
native session identity. Public report format is `merge_contract.review_document`:
the existing immutable report fields, priorities P0..P4, and verdict derived from
the findings (`changes_requested` for any P0..P2; otherwise `no_p2_or_higher`).
`review_report` remains the success-only validator. Successful review session IDs
must differ from each other and from all original coding sessions.

Server result binding checks the exact registered session/receipt/run/source,
selected versioned account and current owned workspace, unchanged source/export,
report binding and digest. Both well-formed review facts are terminal operations.
An incomplete/canceled review is covered by original termination receipts before
release. P2+ findings route through the existing shared published repair allowance.

## Control report publication

After both built-in reports pass, one private trusted checkout writes only JSON
reports/protected receipt sidecars and optional PNG screenshots under the configured
prefix. It creates one new commit and one PR targeting `control_base`. It never
changes the candidate branch. ROOT merges report PRs serially after all four CI
checks and its review; the app never merges control PRs.

`native_intent.kind = review_publication` data fields:

```text
publication_id, workspace_id, workspace, pull_request, review_ids,
repository_id, repository, control_base, base_revision, branch, commit_sha,
files:[{path,digest}], report_paths:{independent:path,root:path}, stage, role
```

`review_ids` references exactly the two native_review metadata operation UUIDs.
`stage` is push/create_pr/status; role is null for push/create and the exact
built-in role for status. Publication UUID, control branch, base, committed files
and review IDs stay immutable. Branch is the owned bounded
`vibearr/review-run/<run UUID>/<candidate SHA prefix>`; files are only `.json` or
`.png` under `<configured prefix>/<run UUID>/<full candidate SHA>/`.

`native_review_publication` factual readback fields:

```text
schema_version:1, intent_id, publication_id, stage, pushed_sha,
pull_request, status
```

PR uses the normal numeric-repository normalized shape for the control branch and
control base. Status is null except status facts, whose shape is
`{id,role,context,creator_id,target_url,state:'success'}`. Target URL is the exact
immutable control report commit/file. Server independently reads the provider
commit/files/PR/status and checks actor, context, source head, role and report.
Merge authorization requires both successful owned native_review facts and their
published provider statuses, plus every optional external gate and required CI.

Each external mutation is journaled before dispatch. Control push uses existing
tracked `push_once` and a real launch callback; unknown push/PR/status outcomes
retain the original claim and reconcile the same identity. Every review publication
intent must have its matching fact before termination, or an explicit
`native_review_publication_terminal` fact with
`{schema_version:1,intent_id,state:'not_started'}` for a definitely unstarted
operation. Dispatched uncertain publications cannot be marked unstarted or erased.
Known report facts remain historical even if ROOT later merges/deletes its branch.

Original native_termination covers every review executor operation and all intent
IDs; server additionally requires review publication terminal coverage. No new
agent, repaired candidate, merge or finish can ignore an unknown review resource.
