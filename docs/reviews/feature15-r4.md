# Feature 15 independent review, round 4

Reviewed source: `b8b50d1d4cca9090c08942ac10aef299c484c82f` (PR 82).
Comparison base: `d0501eef9f5b5397dac336df5cedc01686a53384`.
Candidate Git tree: `09048654c4239d1f179dac39b146a5e5e8045cde`.
Candidate source archive SHA-256:
`604bea85e482be591cd34508f7687332d51d5185118ae15c3d9a483b3bfe0f26`.

**Outcome: no P2-or-higher findings in the reviewed final implementation.**
The independent seven-case acceptance matrix passed, including normal candidate
dependency-image assembly, actual qualification, and all nine required checks.
The report branch additionally includes `27df08afcc16659910faa14c2478857381348885`,
whose only change since the reviewed source is the historical round-3 report.

## Scope and method

Read the repository instructions, feature 15 plan, MVP contract, validation and
executor documentation, complete feature diff, fixed command recipes, source
extraction and receipt handling, runtime/profile/API integration, dependency
artifacts and image lifecycle, standalone packaging, CLI, and workflow. This is
an ordinary functional/source and integration review, using the existing unit
and actual OCI acceptance suites; no additional adversarial probes were run.

The review used a separate full clone, independently built wheels installed
outside the checkout, an independently built validation image, a private Docker
configuration, and independent executor state. Artifact builds completed before
source test discovery. No implementation edit or merge into main was performed.
The report is the only addition relative to the target feature branch.

## Independent results

| Check | Observed result |
| --- | --- |
| Focused source validation/dependency/profile tests | 50 passed |
| Same focused tests through installed packages, `python -I` outside the checkout | 50 passed |
| Complete executor unit suite | 121 passed |
| Focused Black, Ruff, and feature diff whitespace checks | Passed |
| Actual independent OCI host qualification | All 11 required checks passed |
| Exact final committed candidate through installed CLI and own image | All nine required checks passed |
| Exact candidate Django / CLI / portable runner / Git acceptance | 1,572 / 49 / 581 / 136 tests passed; six existing Compose skips in Django and CLI |
| Exact candidate protected receipts | All nine export manifests recomputed from input/output bytes; all nine log digests matched |
| Same database-marker candidate in two distinct runs | Both passed all nine checks; each observed no marker before creating one |
| Deliberate failing unittest | Failed at Django with actual exit 1; later checks did not launch |
| Malformed tracked source with edited candidate recipe/ignore files | Failed at Black with actual exit 123; later checks did not launch |
| Ordinary annotated assignment error with same-name plugin fixture | Failed at Mypy with actual exit 1; later checks did not launch |
| Normal candidate adding `idna==3.10` and an importing test | Setup, copy-only image assembly, actual 11-check qualification, and all nine validation checks passed |
| Complete matrix offline audit | All 46 completed validation export manifests, log digests/lengths, identities, timestamps, and context/source bindings matched |
| Run-owned resources after completion | All 84 parent/child operations exited with descendants terminated; zero owned containers/networks remain |

The exact candidate's application/CLI suites executed all four distribution
artifact tests. Their protected exports contain the generated metadata for all
four source builds.
The image-owned application interpreter has pinned build tools available to
fresh subprocesses, and the source exports remain within their fixed limits.

Independent image:
`sha256:4f16c795bfa46749f34b297b22c8e090a79e2e63aa5dc16861007cecf2f48ee2`.
Pinned PostgreSQL image:
`sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
Independent validation profile digest:
`5a093f90d4fab8d7b2f164e7b5f9f70433c0023ee5093d60b665da8dcc70d3d7`.
Recipe digest:
`9733065334d8d9474f15b50f00041c55337ecd188f8f5032d27a944c0bc0cbb4`.
The local host reports Docker 29.9.3, cgroup v2, rootless mode, seccomp and cgroup
namespaces. The qualification report contains all 11 actual boundary checks.

## Normal dependency-image evidence

Build `a095a193-2d6e-4974-a6ba-f91b0b72ef01` reached `ready` through the actual
installed API. The verified layer contains 9,794 regular files in 188,108,800
bytes, with digest
`3619916c7b8c83eaf7bdb2c2a29d8afd4d353e00d060fd0af214657ba01b2634`.
Derived image:
`sha256:15cb1a29ea48b8a178aed1904a7a8c9b70fa2b71f931d51075742c7d15539e49`.

Independent raw image inspection confirmed its exact trusted parent, one added
layer, unchanged protected runtime configuration, matching build labels, and
unique discovery through `image ls --all`. The stopped assembly container was
removed. The derived image then passed all 11 actual boundary checks. The audit
parsed protected provenance and recomputed its setup log, dependency layer,
requirements, qualification report and binding digests, tying them to the
original source/run and parent image/profile.

All nine derived-image candidate checks passed. The derived Django/CLI/runner/Git
suites ran 1,573 / 49 / 581 / 136 tests. Django and CLI each had ten documented
skips: the existing six Compose skips plus the four conditional distribution
tests because the selected candidate dependency environment omits their build
tools. The exact candidate ran all four artifact tests, and the separate required
`Tests / Run test.sh` workflow retains that coverage. Runner and Git acceptance
had no skips. All nine successful derived validation exports and logs were
independently verified from their actual bytes.

## Earlier findings and integrated behavior

The final source requires successful verified exports before attesting success,
loads the trusted Mypy/Django plugin before candidate paths, supplies build tools
to the application interpreter, and discovers untagged committed images with
`image ls --all` while retaining unique parent/layer/configuration checks.
The native-Codex test fixture explicitly clears the disposable-database marker
for its ambient-database negative case; production database forwarding remains
intact. The complete PostgreSQL-backed Django suite passed in the real executor
environment carrying that marker.

Recovered-session validation recomputes the canonical tree/source/profile context
and compares every operation manifest before action or success. Both new
functional regressions pass: changing only a completed session's tree label is
rejected, and consistently replacing all manifest context labels cannot launch a
check. Existing tests also cover missing/changed exports, missing command
coverage, nonzero exits, truncation, unknown outcomes, changed dependencies,
resource bounds, cancellation, run-scoped derived profiles, replacement assembly
identities, lost/late commit responses, and ambiguous image matches.

The derived qualification view supplies `authorization_denied_ips`. Source review
confirmed that ephemeral private bridge changes do not alter authorization
binding, while configured/public-host address changes still revoke proof; the
existing network and runtime regression suite passed. Actual network enforcement
and the requirement to qualify each derived image remain in place.

## Exact-head CI evidence

Independently downloaded and inspected GitHub Actions
[run 43865326744](https://github.com/innateessence/nightly-queue/actions/runs/43865326744).
Its logs select actual base `d0501eef9f5b5397dac336df5cedc01686a53384`, literal
bootstrap recipe `8082b3241907f91f363b8905d8963136d2211f4e`, and exact candidate
`b8b50d1d4cca9090c08942ac10aef299c484c82f`. The base is an ancestor of that
bootstrap commit. The only file difference between bootstrap and final source is
the validation workflow itself; the executable runner/executor recipe bytes and
requirements match exactly.

The downloaded candidate evidence passed all nine checks and matches the reviewed
tree/source digest. Its profile parses against the independently installed final
SDK, its frozen session passes the context-binding checks, and all nine archived
logs match their receipt digests. CI image:
`sha256:2c24351dd35f75c791779a8503e0e187f3b35479a6f2abe9792addf92568edf5`.
The CI qualification report contains all 11 required checks and records Docker
28.0.4 with AppArmor, seccomp, cgroup v2 and cgroup namespaces on the rootful host.

The existing `Tests / Run test.sh` check remains present and passed on this head
in [run 43865326450](https://github.com/innateessence/nightly-queue/actions/runs/43865326450).
The `Executor boundary` and `Native Codex image` checks also passed. These are
downloaded CI observations, separate from this reviewer's own actual acceptance.

## Evidence and limits

Independent evidence remains available locally:

- `/tmp/vibearr-review15-r4-evidence.json`
- `/tmp/vibearr-review15-r4-state/qualification.json`
- `/tmp/vibearr-review15-r4-state/candidate/` frozen sessions and logs
- `/tmp/vibearr-review15-r4-state/operations/` protected receipts and source exports
- `/tmp/vibearr-review15-r4-state/dependency-builds/` build/provenance/qualification records
- `/tmp/vibearr-review15-r4-verify.py` and receipt summary JSON
- `/tmp/vibearr-review15-r4-final-audit.json` and derived-image inspection JSON
- `/tmp/vibearr-review15-r4-ci-evidence/` and `/tmp/vibearr-review15-r4-ci.log`
- `/tmp/vibearr-review15-r4-*-focused.log`, executor/style/build/install logs

No native model call, production app/database operation, or task delivery was
performed. This review does not claim completion of feature 04's lifecycle,
feature 07's merge policy, or the later full MVP release scenario. No author-run
acceptance result is substituted for independent local or downloaded CI evidence.
Any later executable source change requires a fresh assessment.
