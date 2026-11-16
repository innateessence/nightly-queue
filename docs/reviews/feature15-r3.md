# Feature 15 independent review, round 3

Reviewed source: `54edc5bb3deb6346d09f1f5ad317862481b8ad1d`.
Comparison base: `d0501eef9f5b5397dac336df5cedc01686a53384`.
Candidate Git tree: `4c9aa713a496d8fa704dae8b923c1c7fcf0af722`.

**Outcome: changes required. One confirmed P2 integration failure remains at this
historical revision. This report does not clear a subsequent fix.**

## Finding

### P2 — The integrated exact candidate fails the required Django check

`vibearr_executor/test_codex_task.py:226–243`,
`test_ambient_database_without_executor_marker_is_not_inherited`, patches
`DATABASE_URL` and `POSTGRES_PASSWORD` but leaves the surrounding environment's
`VIBEARR_DISPOSABLE_DATABASE_URL` intact. The validation worker intentionally
supplies that marker for its disposable PostgreSQL database. `codex_task` selects
the marked disposable connection, so the test's assertion that `DATABASE_URL` is
absent fails even though the runtime preserves its intended database contract.

This makes the unchanged integrated candidate fail its required validation
profile. The independent local OCI run discovered 1,570 Django tests and reported
one failure and six skips, with protected exit code 1. The exact-head GitHub
Actions run independently reported the identical failing test and outcome:
[Validate candidate, run 43863094450](https://github.com/innateessence/nightly-queue/actions/runs/43863094450/job/133284640805).

Make this ambient-only test fixture explicitly remove the executor marker while
preserving the production marker/connection behavior. Rerun the complete exact
candidate profile and dependency-image acceptance on the corrected source. The
author acknowledged this finding; no implementation fix is included here.

## Review and validation performed

This review used its own full clone, independently built final image, and clean
executor/runner wheels installed outside the checkout. Imports were verified to
come from that installation. Artifact builds and test discovery ran sequentially.

The full base-to-final diff and surrounding source, profile, worker, artifact,
receipt, runtime, qualification, dependency-build, API, CLI, distribution, and
workflow contracts were inspected. The final workflow selects the real immutable
base, or literal bootstrap commit
`a1eb816c38b8b301ef1041ddd3c78a57a079b11f` after checking ancestry when the base
predates feature 15. Its executor/runner recipe matches the reviewed final source.
Candidate extraction reads regular tracked Git blobs from the exact commit;
validation commands/configuration and clean application entrypoints remain owned
by the reviewed image. The dependency path retains bounded regular-file layers,
copy-only stopped-container assembly, parent/config/layer checks, single commit
intent, run-scoped profiles, and actual qualification. The round-2 `image ls
--all` correction is present and covered by the installed regression suite.

| Check | Observed result |
| --- | --- |
| Build and install independent executor/runner wheels | Passed |
| Build final validation image | Passed |
| Installed executor unit suite | 121 passed |
| Installed native-validation unit suite | 20 passed |
| Actual host OCI qualification | All 11 required checks passed |
| Exact candidate Black, Ruff, mypy, migrations, system | Protected exit 0; source exports and log digests verified |
| Exact candidate Django suite | 1,570 tests; one failure and six skips; protected exit 1 |
| Exact candidate CLI, portable runner, Git acceptance | Not launched after Django failed; prepared operations stopped |
| Repeated database marker, intentional failures, dependency-image acceptance | Not reached because exact candidate failed first |
| Exact-head remote CI | Tests, OCI boundary, and native image passed; Validate candidate failed as above |

The local `--acceptance` driver returned failure for `exact-candidate`; it did not
claim later acceptance cases had passed. All nine prepared operation records ended
in `exited` with confirmed descendant termination. Six actually executed checks
had protected artifacts; their exported bytes/inventories and retained log hashes
were independently checked against the receipts. No production application,
production database, native model call, or merge was involved.

## Evidence identities

- Validation image: `sha256:7791a8cd6a4fb0556cd7274a22451df2c93bf07d49749a022007c7328911f339`.
- Database image: `sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
- Validation profile digest: `2ea9313686384f52b61e55956398bb9d9bd17712e616cf44088e0cf9b361b54f`.
- Recipe digest: `9733065334d8d9474f15b50f00041c55337ecd188f8f5032d27a944c0bc0cbb4`.
- Local qualification report SHA-256: `3997b85887c9fc1dff7a18f282a0a311b4f0f1f8d206e1850628e51fc44de64e`.
- Local Django operation: `e942e217-64dd-5312-a272-c670801f6eb6`.
- Local Django protected log digest: `87584487c1f73b5c9bcd8f8dd5af0d3f759d7932a18cc12de5bf9ed1dde1f031`.
- Downloaded CI Django protected log digest: `f987d08c6022519d5fe0006b799bcbaf6dd46b24ccd93f3944e1875173afae8b`.

Local evidence remains under `/tmp/vibearr-review15-r3-state`, with the aggregate
report `/tmp/vibearr-review15-r3-evidence.json`, build/install/test logs using the
same prefix, and independently downloaded CI evidence under
`/tmp/vibearr-review15-r3-ci-evidence`. Downloaded CI artifacts were inspected as
evidence and were not executed. Historical round-1/round-2 proofs were context,
not substitutes for the final-source checks performed here.

## Limits

This is a functional/integration review, not additional adversarial security
testing. No further exploit probes from the interrupted round-1 review were run.
The full nine-check passing result, repeated database and failure cases, and
normal candidate dependency-image build/qualification remain required on a
corrected source revision. The documented conditional artifact-test rule for a
dependency-derived application environment does not replace the separately
required `Tests / Run test.sh` distribution checks.
