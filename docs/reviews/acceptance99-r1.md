# Issue 99 independent review, round 1

Reviewed on 2026-11-16 in the independent full clone
`/tmp/vibearr-review99-r1`.

- Source issue: [#99](https://github.com/innateessence/nightly-queue/issues/99).
- Source PR: [#104](https://github.com/innateessence/nightly-queue/pull/104).
- Reviewed source: `a44468bf797fd3d0dba0b7e39378ac9df4abf878`.
- Reviewed tree: `268c088c788109c9daebef22cc887c7324b9ab8b`.
- Main comparison: `c56dfc756c8f157043cd87128fdc6f891081abc8`.
- Review branch: `review/acceptance99-r1`, targeting `vibearr/issue-99`.

## Findings

No unresolved P0, P1, or P2 findings. This review changes only this report;
the reviewer made no source changes and merged no PR.

Read the repository guidelines, actual issue and PR, complete two-file diff,
surrounding CLI parser/request/error paths, compatibility imports and module
entrypoints, package metadata, project API serializer, and relevant tests.

The new flag is confined to the projects subcommand and serializes the complete
API response as one JSON object followed by a newline. Numeric IDs, additional
response fields, booleans, nulls, nested values, and Unicode survive a JSON
round trip. Empty projects produce JSON before the human empty-list branch.
The default table and empty-list strings are unchanged. Both console scripts
share the canonical function; both module entrypoints reach that same code.
HTTP, invalid JSON, unexpected response, and configuration failures retain the
existing status/stderr behavior without stdout output. The implementation adds
no dependency or language/library requirement beyond the declared Python 3.10
minimum. Workflows, deployment, UI, and reserved status-filter behavior are
untouched.

## Verification

Python 3.12.12 was used. Checks ran only in this independent clone and private
`/tmp` build/install environments. No application service, executor service,
database, supervisor state, model session, or provider write was used.

| Check | Result |
| --- | --- |
| `python -m unittest nq.test_cli vibearr.test_environment -v` | 43 passed |
| `black --check vibearr/cli.py nq/test_cli.py` | Both unchanged |
| `ruff check vibearr/cli.py nq/test_cli.py` | Passed |
| `mypy vibearr/cli.py nq/test_cli.py --config-file mypy.ini` | Passed |
| `git diff --check` | Passed |
| Independently built wheel installed without dependencies into a fresh venv | Passed |
| Installed CLI subprocess checks against a local HTTP fixture | 56 passed across four entrypoints; 48 HTTP requests |
| Installed module identity and dependency/import boundary | Passed |

The wheel was built using private setuptools 80.9.0 and wheel 0.48.0 with
`pip wheel --no-build-isolation --no-deps --no-index .`, then installed using
`pip install --no-index --no-deps` from `/tmp`. The installed commands ran from
`/tmp`, outside the source checkout; module commands used Python's `-I` option.
The fixture script is `/tmp/vibearr-review99-r1-http.py` and is deliberately
outside this docs-only report change.

The independent subprocess checks exercised installed `vibearr`, installed `nq`,
`python -I -m vibearr`, and `python -I -m nq`. Each exercised populated and empty
JSON; unchanged populated and empty human output; HTTP 403, invalid JSON,
non-object JSON and missing-project-key responses in both formats; missing
token; and invalid server URL. Populated data included Unicode, embedded
newline/tab characters, a numeric ID larger than JavaScript's exact integer
range, booleans, nulls and nested extra fields. All requests used the expected
API path and review-only fixture bearer value. The installed distribution has
no runtime requirements, canonical and compatibility imports resolve to the
same module, and CLI imports did not load Django, runner or database modules.

## Limits and merge gates

Python 3.10 compatibility was inspected; no Python 3.10 interpreter run is
claimed. Full application/runner suites and actual unattended delivery
lifecycle acceptance belong to the separate CI and acceptance evidence; this
report does not claim they were rerun here or that the supervisor's finish/ack
readback is complete. Source CI and this report PR's four required checks must
be evaluated at their exact final heads before the root operator merges them.
The source SHA above remains the reviewed implementation SHA even if this
report later becomes part of that branch.
