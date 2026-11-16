# Feature 00 independent review, round 3

Reviewed PR: [#49](https://github.com/innateessence/nightly-queue/pull/49),
`feat/vibearr-rename`.
Exact implementation head: `0a1a84627ba9827207fa7ad9279ac94ab5c34e12`.
Compared against main: `e1f285247c502c51f4acf9be2c5d3c6182e341c6`.
Review date: 2026-11-16 UTC.

**Result: no P2-or-higher findings.** This is a fresh review of the integrated
rename/native-policy head, performed in a separate full clone at
`/tmp/vibearr-review00-r3`. No implementation changes were made. Older review
reports were not treated as acceptance evidence for this SHA.

## Scope and findings

Read AGENTS.md, feature 00 of `docs/autonomous-ai-queue-plan.md`,
`docs/vibearr-mvp.md`, `docs/runner-execution-policy.md`, the remaining diff
against current main, and surrounding packaging, configuration, lifecycle,
migration, authentication, journal, Compose, CI, and operational documentation.

- Compared every moved runtime implementation against current main with package
  names normalized. Native `policy.py`, `native_evidence.py`, and lifecycle
  readiness/dispatch guards preserve main's implementation. Differences elsewhere
  are the intended names, environment adapter, verification discovery, controller
  header compatibility, and delivery author name. Native work still cannot enter
  the retired proposal executor or become ready through old release evidence.
- Independently imported 39 compatibility runtime modules and asserted each is
  the canonical module object. Checked client class/exception identity, policy
  class identity, pickle resolution, and a legacy lifecycle patch affecting the
  canonical module. Migration 0009's old default import is the same callable as
  the model's canonical default; migration serialization resolves that callable
  successfully. There are no migration-file changes relative to current main.
- Ran a real database upgrade probe: exported current main into a separate source
  directory, migrated an isolated PostgreSQL database with old package names and
  legacy environment variables, and seeded an encrypted credential, project,
  active queue claim, saved branch, active run/slot, context/plan references,
  runner controller proof, and run evidence. Migrating and reading the same DB
  with this head and the unchanged environment retained every seeded value,
  decrypted the credential, and accepted the existing queue API token.
- Independently saved/reloaded the old journal shape, including controller proof
  and an absolute `/var/lib/coding-runner` workspace path. The compatibility
  class preserved those values, and a second controller could not acquire the
  same journal lock. Rebuilt-image probes also persisted journals over separate
  container starts at both `/var/lib/coding-runner` and `/var/lib/vibearr`, using
  private reviewer-owned named volumes, UID 10001, read-only root, and no Django.
- Both canonical and legacy controller-proof headers authenticated the retained
  run. A canonical empty or wrong proof overrode a valid legacy proof and returned
  409. All three independent environment adapters preserved canonical precedence,
  including explicit empty values, and emitted conflicts naming variables without
  exposing either secret value.
- Compose tests cover fresh canonical names, minimal old environment defaults,
  custom old project prefixes, independent data prefixes, and old/new state
  mounts. Reviewed service DNS, DB/user/password wiring, volume names, app/worker/
  beat commands, runner image contents, backup/restore/rollback instructions,
  independent sdist construction, and the renamed CI commands. Existing minimal
  deployments retain the old DB/user, data prefix, and state path; fresh names are
  selected explicitly by the example environment.

## Commands and results

Application checks used `/tmp/vibearr-tools/bin/python` (Python 3.12), PostgreSQL
at `127.0.0.1:32771`, and reviewer-owned database `vibearr_review00_r3`. The full
suite used Django's separate `test_vibearr_review00_r3` database. No live services
or production data were used.

| Command or probe | Result |
| --- | --- |
| `python manage.py test --noinput` with isolated canonical DB variables | 1304 tests passed in 49.820s; 3 distribution tests skipped because shared app tools lack build tools |
| Separate Python 3.12 venv, `pip install setuptools==80.9.0 wheel==0.48.0`, then `python -m unittest -v vibearr.test_distributions` | All 3 tests passed; canonical runner, legacy runner source path, and CLI each built an sdist, built a wheel from it offline, and installed into a new venv outside the checkout |
| Artifact CLI/import probes performed by those tests | Both old/new console commands worked; legacy/canonical modules shared identity; Django was absent |
| `python -m black --check .` | 275 files unchanged |
| `python -m ruff check .` | Passed |
| `python -m mypy . --config-file mypy.ini` | Passed, 275 source files |
| `python manage.py makemigrations --check --dry-run` | No changes detected |
| `python manage.py check` | No issues |
| `docker --config /tmp/vibearr-review00-r3-docker build -f vibearr_runner/Dockerfile -t vibearr-review00-r3-runner .` | Fresh source copied into image `sha256:1ec84b09814491f0485010b8103ad0c91d12ed0226f1166ccb504c10fdea646f` |
| Image `python -m vibearr_runner.verify`, read-only root, no network/capabilities, private tmpfs state | 649 tests passed in 79.584s, including portable Git/recovery acceptance; unsupported sandbox reported `execution_ready: false` and qualification blocked |
| Additional import, persisted journal, migration-default, old-source DB upgrade, controller-header, environment, and two-path image-volume probes described above | All assertions passed |
| `gh pr checks 49` at reviewed SHA | `Run test.sh` passed in 3m52s: [workflow run](https://github.com/innateessence/nightly-queue/actions/runs/43822616836/job/133148949670) |

## Limits

This review did not run a paid coding agent, publish task changes, exercise a live
delivery/merge, qualify an executor, or start the production stack. Portable test
success does not establish deployment boundary qualification. The local source
upgrade used synthetic data and an isolated PostgreSQL server; the mounted-volume
restart probes used reviewer-owned disposable volumes. Existing-data preservation
was checked through database behavior, journal behavior, resolved Compose tests,
and documentation rather than by attaching production volumes. UI branding was
checked through the passing Django tests and existing rendered screenshot; no new
browser screenshot was captured. This report applies only to the exact reviewed
implementation SHA.
