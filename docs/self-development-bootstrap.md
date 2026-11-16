# Bootstrap self-development

Use one trusted web application, one supervisor with its own private journal,
and one executor on a dedicated Linux host. Tasks receive neither the live
checkout nor the live database. This procedure configures
`innateessence/nightly-queue` by repository ID **1353963957**, owner **17239750**,
and base `main`. A renamed repository retains that identity.

## Select existing connections

Save credentials in the application's credential store first. Use a dedicated
Codex API key by default. Import and runner GitHub credentials are separate
records/selections; saving the import connection never grants runner access.
Runner issue and repository credentials may share an explicitly selected record.
GitHub needs issue reads, repository contents/PR writes, and check/status reads.
Workflow edits need additional scope; bootstrap does not broaden credentials.

An explicitly selected Codex session is an additional, separately proven path:
it uses non-refreshing access material, no host configuration or refresh token,
and only `chatgpt.com`. It cannot substitute for a live API-key acceptance result.
See [native Codex](native-codex.md) for account expiry and residual key exposure.

Save this secret-free selection document as mode0600, replacing example IDs and
model with actual saved selections. Put the dedicated runner bearer token in the
named environment variable outside source control; never use the generic API token.

```json
{
  "schema_version": 1,
  "import_credential_id": 1,
  "issue_credential_id": 2,
  "repository_credential_id": 2,
  "backend": "codex",
  "backend_credential_id": 3,
  "model": "gpt-6.1-sol",
  "runner_name": "self-development",
  "runner_token_env": "VIBEARR_RUNNER_API_TOKEN",
  "execution_policy": {
    "schema_version": 1,
    "execution_kind": "native",
    "workspace_access": "write",
    "runtime_profile": "codex-token-v1",
    "validation_profile": "vibearr-python312-v1",
    "network_hosts": ["api.openai.com", "pypi.org", "files.pythonhosted.org"],
    "cpu_limit": 2,
    "memory_mb": 4096,
    "pids_limit": 128,
    "workspace_mb": 2048,
    "repair_limit": 2,
    "delivery_mode": "pull_request"
  }
}
```

The policy permits package hosts only for the separate credential-free dependency
builder. The fixed agent profile permits only its model host; validation is offline.
For a session credential, choose `codex-session-v1` and replace `api.openai.com`
with `chatgpt.com`. Keep `auth.openai.com` unavailable.

```sh
python manage.py bootstrap_self_development --configuration /private/bootstrap.json
python manage.py bootstrap_self_development --configuration /private/bootstrap.json --apply
```

The default preview makes no database writes. Apply fills missing selections,
creates only the missing identity, and adds explicit preflight grants for selected
runner credentials. It never rotates secrets/tokens/controller proof, changes a
saved project choice, or enables global execution. Conflicts name the saved choice
to resolve explicitly. Existing project identity, name, original `enabled_at` and
backfill setting survive repeated setup. Historical open issues remain visible
without becoming eligible. Do not backdate the cutoff to admit the plan backlog.

Ordinary apply preserves every existing global value, including values equal to
framework defaults. Add `--mvp-defaults` to explicitly preview/apply only the
MVP's30-second poll,30-minute aggregate agent budget and20-minute validation budget.
The same values initialize a truly absent settings record. Existing schedule,
pause, enablement and reserve choices remain unchanged.

## Install the reviewed images and profiles

Use [isolated validation](isolated-validation.md) to build/pin its image and a
PostgreSQL image, and generate the private validation profile. The bare Codex
fixture image lacks application dependencies. Build the common-dependency agent
once, from a reviewed immutable Codex image and the reviewed pinned dependency
lock, without any coding/GitHub/queue secret:

```sh
docker build -f vibearr_executor/CodexProject.Dockerfile \
  --build-arg CODEX_IMAGE=sha256:<reviewed-codex-image-id> \
  -t vibearr-self-agent:reviewed .
docker image inspect vibearr-self-agent:reviewed --format '{{.Id}}'
python -m vibearr_runner.bootstrap_profiles \
  --agent-image sha256:<common-agent-image-id> \
  --validation-profile /private/validation-profile.json --kind token
python -m vibearr_runner.bootstrap_profiles \
  --agent-image sha256:<common-agent-image-id> \
  --validation-profile /private/validation-profile.json --kind token \
  --apply --output /private/operator-profiles.json
```

The profile writer creates a mode0600 file, preserves existing different files,
and is idempotent for identical contents. `--registry` additively adopts an existing
operator additions file; conflicting IDs are rejected. It does not accept the
executor's generated smoke registry as an additions file. The resulting eleven
task profiles are one agent with its own disposable DB, nine fixed validation
checks, and one credential-free setup profile. Reuse an existing reviewed image
when its exact requirements, wrapper, immutable ID and current qualification match.

On the executor host, stop its control service and run the normal
[boundary verifier](isolated-executor.md) with these additions:

```sh
python -m vibearr_runner.verify --boundary \
  --executor-image sha256:<common-agent-image-id> \
  --database-image sha256:<postgres-image-id> \
  --profiles /private/operator-profiles.json --executor-state /private/executor-state
```

Qualification includes every distinct agent/validation image and the actual
ordinary profiles, currently qualification version3 with thirteen checks per
image. Start the matching executor using **the generated exact
`/private/executor-state/profiles.json` registry**, not the eleven-profile additions
file. Generated smoke profiles are part of the binding. Changing that registry,
image, runtime source, host or enforcement invalidates qualification. Do not copy
another controller's executor journal or proof.

Set `VIBEARR_VALIDATION_PROFILE_FILE` in the supervisor to the private profile.
Configure its own authenticated executor URL/token and private0700 state directory.
See [runner operations](runner-operations.md) for service configuration. One active
run is enforced by the controller; new settings use continuous processing and
usage reserves off. Existing nondefault choices remain supported and appear in
diagnostics. An enabled reserve without usable telemetry visibly blocks normal
execution; bootstrap does not disable it implicitly. No legacy production-run
release proof is needed.

## Preview and adopt the backlog

The local [seed manifest](self-development-seed.json) captures all sixteen reviewed
feature sections, branch names, dependencies, acceptance criteria and tests.
Exporting performs no provider calls or writes:

```sh
python manage.py seed_self_development > /private/reviewed-seed.json
```

Publishing is an explicit setup action authorized by the operator:

```sh
python manage.py seed_self_development --publish --apply \
  --credential-id <selected-github-id> --state /private/seed-publication
```

All sixteen real plan issues already exist in this repository. The tool scans
every issue page and adopts owner-authored `vibearr-plan-feature` markers; it does
not duplicate, reopen or edit adopted issues. Missing issues are created in
dependency order with real issue references and priority labels. Duplicate markers
fail visibly. An uncertain create response retains a private intent and requires
marker reconciliation; it never blindly sends a second create request. Existing
issue text is task data, never setup commands or completion authority.

Seed completed prerequisites from an explicitly reviewed mapping of feature key,
issue number, actual implementation PR and expected merge SHA:

```json
[{"feature":"05","issue":69,"pull":55,"merge_sha":"<actual-40-character-provider-merge-sha>"}]
```

```sh
python manage.py seed_self_development --record-merges /private/merged.json --credential-id <github-id>
python manage.py seed_self_development --record-merges /private/merged.json --credential-id <github-id> --apply
```

The first command only verifies. Each mapping must identify an owner-authored
feature issue and an actual provider-confirmed same-repository merge into `main`.
Closed issues and body claims are insufficient. Existing identical merge evidence
keeps its original provenance; conflicts fail. This records dependency evidence,
never fabricated runs, approvals or task success. Preserve the genuine project
cutoff before publishing new backlog work.

## Diagnose and start

```sh
python manage.py diagnose_self_development --configuration /private/bootstrap.json \
  --profile /private/validation-profile.json --state /private/runner-state --online
```

One JSON view identifies configuration, selected credential/grant, API identity,
controller proof, source eligibility, provider reads, runtime qualification,
validation image/database/check coverage, backend/model/auth proof, capacity,
schedule and processing gates with specific fixes. Diagnostics never claim work,
rotate credentials, or make paid model calls. Read access is not proof of token
write scope; a finite real delivery proves that. Missing/expired selected-account
proof remains visibly unverified until ordinary eligible-work preflight runs its
bounded probe. Idle polling makes no paid probes. Enable processing once this
one-time setup and the finite acceptance are ready; pause still permits discovery.

## Repeat the finite acceptance

Export exact bounded issue bodies without publishing or making paid calls:

```sh
python -m vibearr_runner.self_acceptance --export-scenarios
```

Use a dedicated disposable app/DB/controller/executor. Before claiming, explicitly
select `repair_limit=0` on that acceptance project's policy; retain all nine fixed
checks. Publish the test-only missing-`--version` regression first, then the
independent `--instructions-file` production improvement. The first must fail a
real validation check with no push/PR, and the improvement must execute afterward.
Hold unrelated already-manually-implemented work explicitly during this bounded
rehearsal. Do not run two workers against the same issue/branch.

```sh
python -m vibearr_runner.self_acceptance --execute --scenario delivery \
  --state /private/acceptance-controller --project-id <id> \
  --failure-issue <failure-id> --first-issue <instructions-id> \
  --evidence /private/delivery-report.json
```

This first phase produces a partial report, preserving real receipts. After fresh
independent/root review and CI, the authorized operator merges the first real PR
and publishes the separate `--version` improvement. Repeat the same command with
`--second-issue <version-id>` to resume; completed tasks are reconciled, not run
again. This publication sequencing does not add a confirmation inside a task.

Each ordinary controller cycle runs in a separate OS process with the same private
journal. The report requires the same agent/container and validation operation
across restarts, all delivery/reporting checkpoints, actual nonzero failure/no PR,
two distinct successful source branches/PRs, genuine sessions, exact validated tree
and pushed head, all nine checks, no legacy approvals, completion acknowledgement,
and observed issue-to-start/PR times. Missing evidence fails. Preserve the journal
and original operation IDs on interruption; never clear state to retry an unknown
publication. Public reports contain identities/digests/receipts, not credentials.

The separate exported merge scenario reserves the MVP's queue status filter and
dependent link/clear behavior for feature07, including real rendered screenshots.
Feature06 proves PR-only delivery; it does not claim automatic merging, deployed
updates, a live API-key proof when only a session was used, or the complete MVP.
