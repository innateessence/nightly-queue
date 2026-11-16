# Feature 03 independent final review, round 2

Reviewed on 2026-11-16 in an independent full clone at
`/tmp/vibearr-review03-r2`.

- Feature PR: [#79](https://github.com/innateessence/nightly-queue/pull/79).
- Reviewed source: `dd8a8cb58771468dcbdc63f90e66a4c63deb307f`.
- Accepted main comparison: `38ee11830d5e3dbf3e5c6f23da133322b71ac198`.
- Review branch: `review/feature03-r2`; this branch changes only this report.

## Findings

No unresolved P0, P1, or P2 findings in the reviewed source. No implementation
change or merge was made by this reviewer.

The review initially audited `58584bb9461485730157b77ff7a0cc0998e1eda2`.
Its actual boundary run reproduced the already reported mismatch between the
worker's credential-export rejection message and the qualifier's expected text,
before any native model call. The author supplied `dd8a8cb`, a one-line message
correction with no redaction behavior change. I inspected that complete delta,
rebuilt the image, and repeated qualification and native acceptance against the
final source above. The final real qualification passed.

## Scope and contract review

Read the repository guidelines, feature 03's accepted plan, the MVP contract,
`docs/native-codex.md`, the complete feature diff, and surrounding executor,
artifact, client, profile, qualification, credential, policy, packaging, and
verification contracts.

The review covered the separate native adapter/registry, immutable session and
operation records, profile digest and runtime qualification checks, configured
resource ceilings, retry/start/reconcile/cancel behavior, bounded JSONL events,
protected receipt/log/source readback, dedicated key and explicit session paths,
fresh configuration, candidate MCP/plugin restrictions, the image recipe and
native code-mode executable, CI, and installed distributions.

Specific previously raised concerns were rechecked:

- Session transport uses sorted compact JSON. The cross-process hash-seed
  regression reopens the real runtime and resupplies the identical operation's
  credential without changing its immutable digest.
- Authentication proofs derive their verification and expiry times from the
  protected `ended_at` value. Retained results do not renew their validity;
  stale/future/malformed completion times fail closed.
- The adapter uses the installed SDK's qualification version. Protocol and
  schema remain 1; this runtime qualifies under version 2 with all 11 checks.
- The current runtime fences unqualified/revoked first launch and its launch
  gate. Executable work/tmp/home mounts and protected noexec state/secrets are
  the accepted runtime contract, rather than outstanding feature 03 findings.
- The child environment derives PostgreSQL/Django fields solely from the
  executor's disposable connection marker. Ambient database/control/GitHub
  credentials are discarded. Proxy exclusions contain only localhost and
  127.0.0.1; temporary project files use bounded `/work`.
- Worker log/export protection includes the selected structured token values
  and the disposable database URL/password. Metadata constants alone do not
  reject source that implements this adapter.

## Verification

Python 3.12.12 was used. Application tests used only the separately created
`vibearr_review03_r2` database and its test database on the disposable development
PostgreSQL service. No live Compose stack or production database was used.

| Check | Result |
| --- | --- |
| Focused native adapter, wrapper, and structured-secret tests | 57 passed |
| Runner discovery with package root specified | 559 passed |
| Executor discovery | 90 passed |
| `python manage.py test --noinput` | 1,519 passed, 4 artifact-tool skips |
| `python -m vibearr_runner.verify` | 695 passed, including portable Git/recovery acceptance |
| Black | 318 files unchanged |
| Ruff | Passed |
| mypy | Passed, 318 source files |
| Migration drift and Django system checks | No changes; no issues |
| Separate actual sdist/wheel installation tests | 4 passed, including installed runner-only/no-SDK verification |

The shared application environment lacks distribution build tools, accounting
for the four explicit skips above. Actual artifact tests were therefore run
separately in a private venv containing pinned setuptools 80.9.0 and wheel 0.48.0.
Full test discovery and artifact builds were kept sequential. An initial runner
discovery command omitted `-t .` and produced relative-import collection errors;
the correctly rooted discovery passed as shown above.

## Independent genuine native acceptance

Built the reviewed `vibearr_executor/Codex.Dockerfile` independently. Its pinned
official CLI and code-mode assets were installed and its actual help/flags were
checked without credentials or network. The build reused content-addressed
upstream installation layers and copied this clone's final executor source.

- CLI: `codex-cli 0.169.0`, model `gpt-6.1-sol`.
- Agent image: `sha256:fb74ec15cca6bdd12f659fea4b1af202dcd2f5e8d4f6a7580759d811ea2816f1`.
- Database image: `sha256:81edcaa5ff2923f7be9c1f43d3e5076f29c6c9c216c86964b5fd038959404429`.
- Profile digest: `0eac2685f4eefb8ca0f21e21bd94e7927c6cb8b424f67753bd63c18595436a0c`.
- Runtime binding: `04cfc2d01edf11b0de542ab444a0bc76436767a586cd578e9cbd2c2a6e756bc5`.
- Host: rootless Docker 29.9.3, cgroup v2, Linux amd64.
- Redacted evidence: `/tmp/vibearr-review03-r2-native-final-evidence.json`.
- Evidence SHA-256: `34746ddc1dc2bf76ba2f662fea0fb3e6d6500530d4b55aee86a6d3370ee5179c`.

Ran `python -m vibearr_runner.codex_acceptance` with those exact image IDs,
private run-owned executor state, and an explicitly selected session copy.
The command passed the real qualification version 2 checks for boundary/egress,
offline validation, resource enforcement, memory exhaustion, restart/descendant
stop, retry/auth, archives/rebinding, disposable database isolation,
credential-free setup, credential/receipt isolation, and PID exhaustion.

The genuine editing operation was `5849fe70-d506-4d94-b577-a6d0762041a9`,
session `01a1e41b-9fc5-7fd2-b278-6c537783e565`. Codex changed `answer.py`, ran
the fixture test, and returned a protected successful result. Its original test
and hostile `.codex/config.toml` remained unchanged; the MCP marker was absent.
The executor restart and repeated start retained the original container.
Independent offline validation operation
`24227e06-6886-418a-a87b-7b8b4ba70c4e` exited 0. The exported result digest
was `d5fa766702cdf3efda7ef8c75ab327c13a1806b4f54bfd7e32e1188e1bc13fc9`.

Cancellation operation `37802ea3-5b1f-489b-b0a7-a102395816d7`, session
`01a1e41c-022b-72b0-9e4b-d17ef7fedbe3`, emitted an actual foreground sleep
tool-start event before cancellation. It exited -9 and descendant termination
was confirmed. This was a real tool invocation, not fixture parser output.

Authentication operation `6ab56ac6-68e7-4b8c-af64-a4886f727336` produced a
genuine bound proof and ready diagnostics. After the acceptance process stopped,
I reopened the same executor through authenticated REST and reread that result;
its original verification and expiry timestamps were unchanged. A separate
prepare-only REST operation confirmed the stored runtime secret was mode 0600,
used `chatgptAuthTokens`, had an empty refresh token, omitted optional metadata,
and contained none of the nonsecret selected-input refresh placeholder. It was
stopped before launch and its secret file was verified empty. This check made no
additional model call. All review-owned operations were confirmed exited.

The selected control-side file contained only necessary access/id/account fields
plus a nonsecret refresh placeholder. No host refresh token was copied into it.
The adapter removed that placeholder before task transport. The host login file
was read-only and its byte digest was unchanged after acceptance. No host
configuration, plugin/MCP state, or credential writeback was used.

## Limits

No dedicated API key was supplied, so this report does not claim live API-key
acceptance. That path was reviewed and covered by portable tests; the real native
proof used explicit nonrefreshing session authentication. No unattended queue
lifecycle, PR delivery, merge loop, application deployment, ARM64 image execution,
or provider-side dollar cap is claimed by this feature 03 fixture. The bare
Codex image intentionally lacks the application's Django requirements; real
repository work requires the separately reviewed derived image documented in
`docs/native-codex.md`.
