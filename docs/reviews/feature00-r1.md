# Feature 00 independent review, round 1

Reviewed PR: https://github.com/innateessence/nightly-queue/pull/49

Reviewed exact implementation head: `44febf8836ed107ec772615b7cc1883eb7dd277d`.
Comparison base: `63df77d75c3e5b1f7cfda6c0f98020deb0d48f12` (`main` at clone time).
Review date: 2026-11-15.

The review used an independent full clone at `/tmp/vibearr-review00-r1`, Python
3.12, a clean standalone-install virtual environment, and the distinct disposable
PostgreSQL database `vibearr_review00_r1`. No live application services, production
data, or other working clones were changed. This report concerns the exact head
above; later implementation fixes require another independent review.

## Decision

**Changes required: two P2 findings.** No P0 or P1 findings were identified.
Passing suites do not clear the release-packaging and operator-documentation
failures below.

## Findings

### P2: Runner source distributions omit the sibling package they require

Locations: `vibearr_runner/pyproject.toml:18` and
`coding_runner/pyproject.toml:18`.

Both independent manifests resolve one required package outside the project
directory (`../coding_runner` or `../vibearr_runner`). A wheel built directly
from the full Git checkout works because both directories exist. A source
distribution build reports success, but the resulting archive omits the sibling
directory. Rebuilding a wheel or installing that archive fails during metadata
generation. This breaks the renamed runner's source release/install path and is
a regression from the pre-rename runner source distribution, which successfully
rebuilds a wheel.

Reproduction from the reviewed checkout, using a Python environment with `build`:

```sh
python -m build --sdist --outdir /tmp/review-sdists ./vibearr_runner
python -m pip wheel --no-deps --wheel-dir /tmp/review-wheels \
  /tmp/review-sdists/vibearr_runner-0.1.0.tar.gz
```

The second command exits 1 with:

```text
error: package directory '../coding_runner' does not exist
```

Repeating the two steps for `./coding_runner` fails with:

```text
error: package directory '../vibearr_runner' does not exist
```

Package both canonical and compatibility modules inside a self-contained source
artifact, and verify installation from that artifact outside the Git checkout.
The existing direct-checkout package smoke checks cannot detect this failure.

### P2: Published runner setup and deployment examples target removed names

Primary location: `docs/runner-service.md:20` (the command block at lines 20–22).
Related locations: `docs/runner-service.md:86` and
`docs/runner-workspaces-approvals.md:150`.

The setup guide still tells operators to build, check, and start the
`coding-runner` Compose service. The reviewed Compose definition provides only
`vibearr-runner`, so these instructions stop before runner startup. Independently
executing the build command fails immediately without building or changing any
service:

```sh
docker compose --env-file /dev/null --profile runner build coding-runner
```

Result: exit 1, `no such service: coding-runner`.

The same guide's image-validation example still uses
`nightly-queue-coding-runner`, while the build now tags `vibearr-runner`. The
operator-owned sandbox-policy overlay example still selects `coding-runner`;
it no longer configures the actual runner service. These are executable setup
and deployment instructions, so the rename needs to update them together with
the Compose definition. Legacy Python import names remain valid and need not
be removed from historical API/module references.

## Independent validation

- Read repository instructions, feature 00 in
  `docs/autonomous-ai-queue-plan.md`, `docs/vibearr-mvp.md`, the upgrade guide,
  deployment scripts/manifests, environment resolvers, legacy shims, packaging,
  authentication/encryption changes, and runtime/recovery changes. Compared
  moved runner modules against their pre-rename implementations, accounting for
  namespace changes, rather than relying only on test results.
- `python -m unittest nq.test_cli vibearr.test_environment vibearr.test_compose`:
  **45 tests passed**. Compose checks resolve configuration only; they do not
  start the live stack. Their cases cover minimal legacy defaults, fresh names,
  custom volume prefixes, and old/fresh runner paths.
- `python manage.py test --noinput` against the disposable PostgreSQL service:
  **1,261 tests passed**, including database/API/settings/credential behavior.
- `black --check .`, `ruff check .`, and `mypy . --config-file mypy.ini`:
  **passed**. `python manage.py makemigrations --check --dry-run` reported no
  changes; `python manage.py check` reported no issues.
- `python -m vibearr_runner.verify` from the source checkout:
  **631 tests passed**, including portable Git and recovery acceptance.
- `python -m vibearr_runner.verify --boundary` on this host:
  **153 tests passed**. This is host evidence, not proof about the hardened
  deployed container or authorization to enable execution.
- Created a clean Python virtual environment with no Django, installed the CLI
  and runner from the full checkout, then invoked `vibearr`, `nq`,
  `vibearr-runner`, and `coding-runner` help outside the checkout: all passed.
  Imported each existing non-test legacy runtime module outside the checkout
  and verified it is the same object as its canonical module. Ran installed
  `python -m vibearr_runner.verify`: **495 tests passed**. The installed wheel
  does not contain the explicit acceptance directory, so that count must not be
  confused with the source checkout's full acceptance count.
- Built source distributions from both new runner manifest paths and attempted
  to rebuild wheels outside the checkout: **both failed as described above**.
  Repeated the source-distribution-to-wheel check against the pre-rename
  `coding_runner` extracted from the comparison base: **passed**.
- Migrated a fresh disposable database with pre-rename code, created a project,
  encrypted credential, succeeded queue item, completed run with a saved
  configuration snapshot, and runner identity/token digest. Switched to the
  reviewed code and ran migrations. Verified the same ciphertext decrypts,
  identifiers/history/snapshot survive, the runner token digest is unchanged,
  and the existing general API token lists the project. Repeated readback after
  changing only the environment variable names to canonical key/token names:
  **passed**. No model migration is introduced by this feature.

## Limits

No paid provider calls, real GitHub delivery, live runner takeover, production
volume relocation, repository rename, or deployment qualification was attempted.
Runner path preservation was assessed through resolved Compose mounts, the
unchanged journal/recovery contracts, and deterministic recovery acceptance;
this review did not restore a real production runner volume. Existing tokens
and encryption were verified with synthetic pre-rename data, not production
secrets. Future native-execution features are outside feature 00's readiness
scope.
