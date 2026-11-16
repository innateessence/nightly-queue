# Feature 15 independent review, round 2

Reviewed executable source: `86c014c184a178a9e8e79ed03b5068607bc4c7ee`.
Comparison base: `38ee11830d5e3dbf3e5c6f23da133322b71ac198`.
Candidate Git tree: `ef80fd185946a65a72080658ef5966e478c5138d`.
Report branch includes `4421fc59d82e75d1b4c3fb01cb314c7ec929c613`, whose only
change from the reviewed source is the historical round-1 report.

**Outcome: changes required. One P2 finding remains at this historical revision.**
This report does not clear a later dependency-image fix, accepted-main integration,
network-binding integration, or replacement bootstrap recipe/workflow.

## Finding

### P2 — Committed dependency images are omitted from image reconciliation

`vibearr_executor/dependency_builds.py:407`, `committed_image()`, enumerates images
with `docker image ls --no-trunc --quiet --filter label=...` without `--all`.
`assemble()` commits the copy-only assembly container without assigning a tag.
On the actual Docker 29.9.3/API 1.56 executor, the default listing omitted the
new untagged image. Consequently the ordinary dependency build entered `unknown`
with `Image commit response does not match its unique verified image.` It never
started derived-image qualification and retained the executor fence.

This reproduced using the installed runner/executor packages and a normal
candidate that adds `idna==3.10` and a test importing it. Credential-free setup
exited zero and produced a verified 188,108,800-byte, 9,794-file dependency layer.
The stopped-container copy and image commit completed. Build identity:
`8e98eac0-adb6-4f92-8d24-157911acf5ea`; derived image:
`sha256:7dc43b6774baf46bdbf981d9c0e289e05ad828790c58d6fdbc431ee149f1faf8`.

The implementation's exact label-filtered default listing returned no image;
the same command with `--all` returned the derived image. A read-only installed
reproduction adding only `--all` to discovery made `committed_image()` return that
exact image after its existing parent, labels, layer and configuration checks.
The raw image inspection includes the expected `Parent`; it is not an additional
finding. Include all committed images in reconciliation while retaining unique
identity and provenance checks, then rerun ordinary dependency-image acceptance.

## Validation actually performed

The review used a separate full clone, independently built validation image,
independently built and installed wheels, and private executor state. Installed
imports resolved under `/tmp/vibearr-review15-r2-installed`, outside the checkout.
Artifact builds completed before source test discovery. No implementation edit
or merge was performed.

| Check | Observed result |
| --- | --- |
| Full feature diff, plan/MVP/validation docs, fixed recipes, runtime/API/artifact and dependency-build contracts | Inspected |
| Focused validation/dependency/profile tests from source | 48 passed |
| Same focused tests through installed packages with `python -I` outside the checkout | 48 passed |
| Executor suite | 98 passed |
| Changed-file Black/Ruff and diff whitespace checks | Passed; Black checked 38 files |
| Actual independent OCI host qualification | All 11 required boundary checks passed |
| Exact committed candidate through the installed CLI and own image | All nine required checks passed |
| Exact-candidate application suites | Django 1,508; CLI 49; portable runner 542; Git acceptance 136; all passed, with six existing skips in Django/CLI |
| Protected receipts | Recomputed all nine source-export manifests from actual input/output bytes and all nine log digests; all matched |
| Same database-marker candidate, two distinct runs | Both passed all nine checks; each test observed no marker before creating one |
| Hand-authored deliberately failing unittest | Failed at required `django` check; later checks did not qualify success |
| Ordinary incompatible annotated assignment | Failed at required `mypy` check |
| Normal candidate dependency image | Setup/copy/commit succeeded; reconciliation blocked as described above; qualification and derived-profile tests did not run |

The independent image was
`sha256:7ae7422529907bbde965d500124e2b6ef1fc1384dc6c7e300e9a08ccce817edf`;
the pinned database image was
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.

The three historical round-1 issues were separately checked: protected export
failure/missing-artifact cases are rejected by the installed regression tests;
the exact successful run's exports were verified; the image-owned type plugin is
loaded and checked before candidate paths, and an ordinary type error fails;
the corrected clean application interpreter passes the full source suite,
including distribution artifact tests. This round performed ordinary software
review and the existing regression suites, without additional exploit probes.

## Workflow evidence

Independently downloaded and inspected GitHub Actions
[run 43859214136](https://github.com/innateessence/nightly-queue/actions/runs/43859214136).
Its logs select actual base `38ee11830d5e3dbf3e5c6f23da133322b71ac198`, literal
bootstrap recipe `64b9a360a1d5614ef09d2d90bbc04c3860cdab71`, and exact candidate
`86c014c184a178a9e8e79ed03b5068607bc4c7ee`. The artifact records the expected
tree/source digest and actual zero exits for all nine checks. All nine archived
log files match their receipt digests. The qualification artifact contains all
11 required checks and rootful AppArmor/seccomp/cgroup namespace observations.
The existing `Run test.sh` check remains present and passed for this head.

This passing CI run validates unchanged dependencies. It does not exercise the
dependency-image path that failed independently. A later workflow/bootstrap
change must receive its own exact-head assessment.

## Evidence and limits

Local evidence retained for the author and root reviewer:

- `/tmp/vibearr-review15-r2-evidence.json`
- `/tmp/vibearr-review15-r2-state/qualification.json`
- `/tmp/vibearr-review15-r2-state/candidate/` logs and frozen session
- `/tmp/vibearr-review15-r2-state/operations/` protected receipts and exports
- `/tmp/vibearr-review15-r2-ordinary/evidence.json` for the four completed cases
- `/tmp/vibearr-review15-r2-ordinary/dependency-image/build-status.json`
- `/tmp/vibearr-review15-r2-state/dependency-builds/8e98eac0-adb6-4f92-8d24-157911acf5ea/record.json`
- `/tmp/vibearr-review15-r2-dependency-image-raw.json`
- `/tmp/vibearr-review15-r2-parent-repro.log` for successful read-only discovery with `--all`
- `/tmp/vibearr-review15-r2-ci-evidence/` and `/tmp/vibearr-review15-r2-ci.log`

The dependency-image acceptance is incomplete and the exact reviewed revision
is not cleared. Final native-Codex/main and accepted network-binding integration,
the derived qualification view's new authorization-binding hook, and the refreshed
literal bootstrap recipe remain for a fresh review. No native model call,
production application/database operation, or delivery action was performed.
