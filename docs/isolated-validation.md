# Isolated project validation

The `vibearr-python312-v1` profile validates one exact committed tree in fresh
executor containers. The runner selects the commands from an operator-owned
profile. Candidate changes to `test.sh`, workflows, tool configuration, or executor
modules do not replace those commands. Each check has its own operation UUID,
protected exit status, bounded log, and fresh source snapshot. PostgreSQL checks
receive a new disposable database with no published port or production volume.

The image supplies Python 3.12, Git, pinned tools and application dependencies.
Validation has no model or GitHub credential and no external network. It receives
only its own disposable database connection. It does not invoke Docker, Compose,
or the live installation's `test.sh`.

## Required checks

| ID | Image-owned command |
| --- | --- |
| `black` | Black with the trusted configuration and explicit tracked Python files |
| `ruff` | Ruff with the trusted configuration, explicit files, and Git ignores disabled |
| `mypy` | Mypy with the trusted Django configuration and explicit Python files |
| `migrations` | Apply migrations, check application state, and detect missing migrations |
| `system` | Django system checks |
| `django` | The complete Django-discovered application and module suite |
| `cli` | Standalone canonical and compatibility CLI tests |
| `runner` | Independent portable runner tests |
| `git-acceptance` | Git/workspace, delivery, and recovery acceptance |

The Git acceptance profile excludes the two historical bubblewrap-only methods
`test_actual_sandbox_stops_command_at_remaining_budget` and
`test_real_sandbox_validation_records_actual_exit_code`. The actual OCI boundary
verifier supplies the replacement deployment qualification. These are fixed
profile rules, not candidate-selected skips. Source artifact distribution tests
remain required by the existing `Tests / Run test.sh` workflow; the full suite
also executes them when the selected environment contains setuptools and wheel.

All tracked Python files are passed explicitly to the tools. A candidate's
`.gitignore` containing `*` cannot hide malformed tracked source. The application
test entrypoint uses a clean interpreter, the disposable PostgreSQL environment,
normal test umask 022, and IPv4 container loopback for Django's live HTTP server.
This keeps the actual authentication tests while IPv6 and external egress remain
denied. The supervisor and credentialed agent retain their own private defaults.
The check uses the image-owned interpreter with startup site hooks disabled;
dependency-supplied Python executables and `.pth` startup files do not select or
replace the required test entrypoint.

## Install and select a profile

Build from reviewed source outside the candidate checkout. The pinned tool lock
is `vibearr_executor/validation-requirements.txt`; the image also records the
reviewed project's original `requirements.txt` separately.

```sh
docker build -f vibearr_executor/validation.Dockerfile -t vibearr-validation:reviewed .
docker image inspect vibearr-validation:reviewed --format '{{.Id}}'
docker pull postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f
docker image inspect postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f --format '{{.Id}}'
python -m vibearr_executor.validation_profile \
  --image sha256:<validation-image-id> --database-image sha256:<postgres-image-id> \
  --requirements /reviewed/source/requirements.txt --output /private/validation-profile.json
```

The writer creates a new mode-0600 file and never overwrites an existing choice.
Register its `executor_profiles` and `setup_profile()` alongside the agent
profiles, then run the [actual executor qualification](isolated-executor.md).
Every distinct image needs the complete actual host proof. Changing an image,
recipe, registry, runtime, host, or egress policy revokes the affected proof.

Set `VIBEARR_VALIDATION_PROFILE_FILE` in the supervisor to the private JSON path
and select `execution_policy.validation_profile = "vibearr-python312-v1"`.
`load_validation_profile()` and `validation_diagnostics()` need only the installed
client package and authenticated executor API; they never access Docker.
`runtime_diagnostics()` recognizes the semantic profile and verifies all nine
installed executor profiles. A missing command/image or a changed recipe blocks
readiness before a production claim.

## Exact source and receipts

`vibearr_runner.native_validation.CandidateSource.from_git(checkout, commit_sha)`
reads the exact commit's regular Git blobs and modes. It rejects symbolic links,
submodules, unsafe paths and oversized input. Dirty, ignored and untracked files
from a prior agent session are absent. The archive uses the executor's 32-MiB,
10,000-entry source limit. Git metadata is not sent into the task.

`ValidationRunner.prepare()` accepts a run UUID, a new validation-attempt UUID,
the source, immutable `ValidationProfile`, frozen policy and deadline. It binds
deterministic per-check UUIDs and reduces resources to the policy/profile bounds.
Persist the returned session before dispatch. `start(session)` starts only the
first unstarted check; callers must recheck ownership, policy, pause, schedule and
cancellation before each call. `status(session)` reconciles without launching.
Unknown outcomes retain ownership and never start a replacement operation.

Successful output has the version-2 `validation` shape: profile ID/digest,
image digest, exact Git tree SHA, and every required check's operation UUID,
actual zero exit code, timeout flag, ordered timestamps, log reference and digest.
References use `executor:<operation-uuid>:logs`; read them through the authenticated
bounded log API. Truncated logs, nonzero exits, timeouts, cancellation, missing
termination, changed command coverage, or changed dependencies cannot qualify a
successful delivery. Commit the inspected candidate before extracting it; any
later tree change requires another validation attempt.

`reconcile(session)` and `stop(session)` use the original frozen operation
identities and remain available when the installed recipe changes. They cannot
attest a passing validation or launch another check. The outer task lifecycle
owns delivery, shared repair budgets, claim fencing and completion reporting.

## Candidate dependency images

When candidate `requirements.txt` differs, `DependencyBuilder` prepares a fixed
credential-free setup operation. Package hosts must already be permitted by the
frozen policy. The setup task installs index requirements in a separate venv,
inside the same CPU, memory, PID, storage, network and deadline boundary. URLs,
recursive includes and arbitrary pip options are not supported by this recipe.

After every task descendant stops, trusted init captures a separate dependency
artifact: at most 256 MiB and 50,000 regular files, with verified requirements,
file layout and digest. Links, special files, traversal and writes outside the
dependency prefix are rejected. This artifact does not enlarge the source-upload
limit or carry model/delivery credentials.

The executor creates a stopped container from the exact trusted parent image,
copies the verified layer only into `/opt/candidate`, then commits it. It never
starts that assembly container, runs a candidate Dockerfile, or executes candidate
installation code on the host. The original assembly container ID and parent,
copy/commit intent and unique image identity are journaled. Lost commit responses
reconcile the same image; ambiguous or replacement identities retain the fence.

The derived image undergoes the complete automated host qualification in an
independent private journal. A restart-independent qualification process writes
its actual proof; no success flag substitutes for that proof. Only afterward does
the executor expose its immutable per-check profiles. This is additive: the
static registry and existing native agent profiles remain intact. The derived
profiles are restricted to their original run, including after restart, to keep
dependencies containing project data out of unrelated tasks.

The authenticated control API exposes `POST /v1/dependency-builds/prepare` and
`POST /v1/dependency-builds/<uuid>/{start,status,stop}`. The SDK methods are
`prepare_dependencies(request)` and `dependencies(uuid, action)`.
`DependencyBuilder` constructs policy-bounded requests and preserves their exact
identity on retries. Ready results contain a strict `ValidationProfile` and
protected provenance binding the original run/source, parent image/profile/runtime,
setup operation/log, dependency layer, requirements, derived image and actual
qualification report. `validate_dependency_provenance()` parses this without engine
access for the authenticated server's run-scoped attestation.

Build records, copied layers, logs and qualification reports live beneath the
executor's private `dependency-builds/<uuid>` directory; setup artifacts remain
under `operations/<uuid>`. Assembly/image labels include the executor and build
UUID. Retention must preserve active/unknown builds and any referenced image.
Cancel/recovery acts only on those recorded resources. An unknown build cannot
release the executor fence merely because its heartbeat or deadline expired.

The agent image independently needs the project's common dependencies installed
before credentialed model execution. This validation builder does not grant
package egress to an agent or install packages into a live application.

## Reproduce acceptance

From an installed runner and executor outside the checkout:

```sh
python -I -m vibearr_runner.validation_cli \
  --checkout /private/candidate --commit <exact-commit-sha> \
  --profile /private/validation-profile.json --state /private/validation-acceptance \
  --evidence /private/validation-evidence.json --acceptance
```

This qualifies the image and runs a passing candidate twice, proves that its
database marker does not survive into the next run, rejects a deliberate failing
test, rejects ignore-all/candidate-recipe bypass, and builds/qualifies/validates a
candidate adding an actual dependency. Every case preserves actual exit/log/tree
evidence and only creates run-owned disposable resources. It needs Docker on the
executor host, the pinned database image and package access; it needs no native
coding agent, paid model call, production stack or production database.

The `Vibearr validation / Validate candidate` workflow uses the same installed
profile and driver against the exact PR head. Its command recipe comes from the
immutable base commit, with a literal reviewed bootstrap recipe commit only when
the real base predates this feature. Candidate workflow edits are not themselves
proof of trusted execution; automatic merge must retain the configured workflow
producer and trusted-base profile checks described in the MVP contract.
