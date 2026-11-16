# Feature 00 independent review, round 2

Reviewed PR: https://github.com/innateessence/nightly-queue/pull/49

Reviewed head: `5369ebffece70491a66114496b3d63854e7f2c6b` (`feat/vibearr-rename`).
Baseline: `63df77d75c3e5b1f7cfda6c0f98020deb0d48f12` (`origin/main` at clone time).
The remote feature head was checked again during review and still matched this SHA.
This report does not clear later commits.

## Result

**No P2 or higher findings.** No implementation files were changed by this reviewer.

The review used an independent full clone at `/tmp/vibearr-review00-r2`, read its
`AGENTS.md`, feature 00 in `docs/autonomous-ai-queue-plan.md`, the upgrade guide,
and the MVP contract. It inspected the complete feature diff, compared moved
runtime implementations against the baseline, and independently exercised the
packaging changes that addressed the previous review's artifact failure.

The canonical and legacy runner source roots both now produce self-contained
source archives. The staged configuration points at the vendored companion
package without changing the checkout configuration. Inspection found no
absolute/traversing archive member names, symbolic links, hard links, duplicate
wheel members, or runtime dependency declarations. Each runner wheel contained
100 distinct members. The CLI wheel contained 16 distinct members and did not
include the application or runner packages. Clean virtual environments installed
the artifacts and exercised both command aliases, same-object compatibility
imports, and absence of Django imports outside the checkout.

Operational setup/qualification/backup examples now select the canonical Compose
service and image. Compatibility paths, historical planning descriptions, and the
unchanged remote repository identity remain intentional. The deployment keeps
legacy storage defaults and makes fresh canonical identities explicit in
`.env.example`.

## Validation

Commands used Python 3.12.12. Application checks used
`/tmp/vibearr-tools/bin/python`; artifact checks used a separate reviewer venv
with `setuptools==80.9.0` and `wheel==0.48.0`.

| Check | Result |
| --- | --- |
| `python -m unittest vibearr.test_distributions` in the reviewer build-tools venv | 3 passed: canonical runner, legacy runner, CLI sdist-to-wheel installation in clean environments |
| `python -m unittest vibearr.test_distributions vibearr.test_compose vibearr.test_environment nq.test_cli` in the shared tools venv | 48 tests, 3 artifact checks skipped because this environment lacks build tools; those checks passed separately above |
| Independent sdist/tar and wheel/metadata inspection for `.`, `vibearr_runner`, `coding_runner` | All three archives safe by the member checks described above; wheels have unique members and no `Requires-Dist` |
| `python -m vibearr_runner.verify` from the source clone | 631 passed |
| Installed runtime import/patch probe under `python -I` from `/tmp` | 29 legacy/canonical modules share identity; old patch target affects canonical module; Django absent |
| Direct canonical runner wheel build/install, followed by `python -I -m vibearr_runner.verify` from `/tmp` in the reviewer venv | 495 passed; no Django installed and no checkout import path |
| `python manage.py test --noinput` using isolated PostgreSQL | 1,264 tests passed, 3 artifact checks skipped and independently passed above |
| `black --check .` | Passed, 265 files unchanged |
| `ruff check .` | Passed |
| `mypy . --config-file mypy.ini` | Passed, 265 files; existing untyped-function note only |
| `python manage.py makemigrations --check --dry-run` | No changes detected |
| `python manage.py check` | No issues |
| `docker --config /tmp/vibearr-review00-r2-docker build -f vibearr_runner/Dockerfile -t vibearr-review00-r2-runner .` | Passed |
| Built-image `python -m vibearr_runner.verify`, with read-only root, all capabilities dropped, `no-new-privileges`, network disabled, private tmpfs state | 631 passed in 73 seconds; sandbox probe correctly reported unsupported and release qualification blocked |

The PostgreSQL connection was restricted to `127.0.0.1:32771`, role
`vibearr_dev`, database `vibearr_review00_r2` and Django's corresponding disposable
test database. No live application database was used.

An additional upgrade probe exported the baseline source into a separate
reviewer directory, migrated the disposable database with the old settings,
then seeded an encrypted credential, project, completed queue item with result
URL, completed run, controller digest, and a private legacy journal containing
controller proof and an absolute `/var/lib/coding-runner/...` workspace path.
The feature head required no further migrations and preserved every checked
value. Credential decryption, project rendering, and authenticated project API
access with the legacy `NQ_API_TOKEN` all passed. Initial reviewer probe attempts
used nonexistent API URLs; the final probe used `/api/v1/projects/` and passed.

Compose tests independently covered minimal old environment storage identities,
the fresh environment example, custom old Compose project prefixes, explicit
storage prefixes independent of the service project, and both state mount paths.
Environment tests covered canonical precedence, explicit empty values, equal
aliases, and warnings containing variable names without secret values.

## Limits

This is rename/compatibility validation, not deployment release qualification.
No production stack, production volumes, paid coding agents, remote repository
rename, or merge operation was used. The database/journal upgrade probe exercised
real persisted data but did not restore a production snapshot or resume a live
agent process. Portable verification outside the checkout does not include the
source-only acceptance namespace; it therefore has fewer tests than the source
suite. The full supported-host sandbox boundary and controlled live release
scenarios remain separately gated. This report makes no claim that those gates
have been satisfied.
