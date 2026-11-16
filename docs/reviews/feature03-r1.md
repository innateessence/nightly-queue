# Feature 03 independent review R1

Reviewed PR #79 through exact source commit
`ecead17a7846d2dff32b2c9e814a43460eefab99`, against accepted main
`a679ecb19dd18ab4b2f1c765c4bbf6c7de816351`. Review used the independent full
clone `/tmp/vibearr-review03-r1`. This report changes no implementation files.

**One P2 finding remains. This source is not clear to merge.**

## P2: canonicalize transformed session credentials before retry-stable delivery

Location: `vibearr_executor/codex_task.py:67` and `:89–96`; consumed by
`NativeCodexAdapter.prepare()` and `Runtime.supply_secret()`.

`auth_material()` builds the transported token dictionary by iterating a set,
then serializes it with `json.dumps()` without sorting keys. Python randomizes
set order across processes. The same selected session credential therefore
produces different transport bytes after a supervisor restart, despite identical
account, token values, context, operation and manifest.

The executor correctly binds the first supplied secret's byte digest to its
operation. A retry after a lost preparation response or a crash before the
returned `NativeSession` is persisted then conflicts with that binding. This can
leave a prepared, unstarted run unable to resume through its intended idempotent
preparation path.

An independent fake-credential probe reproduced the problem across separate
Python processes with `PYTHONHASHSEED` values 1, 2 and 3. The token field order and
transport SHA-256 changed in each process. Representative output:

```text
seed 1: id_token, access_token, account_id, refresh_token
seed 2: id_token, account_id, access_token, refresh_token
seed 3: account_id, id_token, access_token, refresh_token
RETRY_FAILED Credential material cannot change within an operation.
```

The final line came from the actual executor `Runtime`: prepare one operation,
supply the first process's transformed credential, then supply the same logical
credential transformed by the second process. It raises `Conflict`. This probe
used a fixed fake JWT expiry and run-owned temporary storage; no real account,
container, provider call or host credential was involved.

Make this transformation deterministic, including nested token ordering, and
add a cross-process same-operation retry regression. Preserve the executor's
immutable secret comparison rather than weakening that fence. Probe and log:
`/tmp/vibearr-review03-r1-secret-order-probe.py` and its matching `.log` file.
The finding was sent to the author and root before publishing this report.

## Scope and corrections assessed

Audited the full native session registry/adapter contract, fixed CLI and official
image pins, selected authentication and structured-secret handling, immutable
manifest/context/account/profile bindings, bounded JSONL events and token usage,
restart/reconciliation/cancellation, protected logs/exports, authentication
proofs, package resources/compatibility imports, application configuration,
workflow and acceptance driver.

The authentication-proof correction in `ae594ada` now anchors verification and
expiry to the protected task completion timestamp. Independent probes advance
the supervisor clock ten and sixteen minutes: rereading preserves original
expiry, and an expired probe cannot mint a new proof. Malformed completion
values are rejected. That correction does not resolve the serialization finding.

Two pending upstream/integration matters are distinct from this introduced P2:

- Accepted runtime 02's combined intentional-OOM fixture can lose its trusted
  init. The normal unknown fence is correct; its qualification fix was being
  developed separately and was not part of this reviewed source.
- A minimal runner-only installation's portable verifier already had one
  positive runtime fixture failure without the optional executor SDK. This
  feature originally added another unconditional SDK test import. The reviewed
  follow-up now guards those imports and skips only SDK-backed cases, retaining
  portable event tests. Its new installed-verifier assertion still needs the
  accepted upstream fixture correction. Final integration must rerun that path.

## Independent validation

- Core `b1489858`: full Python 3.12 application suite on a separate disposable
  PostgreSQL database passed **1,491 tests**, with four expected artifact-environment
  skips. Black, Ruff, mypy (317 files), migration drift and system checks passed.
- All four actual sdist/wheel/clean-virtualenv distribution tests passed at that
  core, before the later runner-only verifier assertion was added.
- Latest reviewed `ecead17a`: **49 focused adapter/wrapper/secret tests passed**.
  Two additional independent authentication freshness/malformed-value probes
  passed after the completion-time correction.
- Built independent pinned core image
  `sha256:6a63dc0be84be5c882efd810512342608a995928947f6d7adffcd85390dc2a11`.
  With no network or credentials, an unprivileged read-only container reported
  actual Codex 0.169.0, its code-mode host help succeeded, and all **13 installed
  wrapper/structured-secret tests passed**. Those containers were removed.
- A clean runner-only wheel installed outside the checkout reproduced the
  optional-SDK verifier gap described above: 521 tests, one existing failure and
  one then-unconditional import error. No passing minimal-install claim is made.

Logs and probes are under `/tmp/vibearr-review03-r1-*.log`. The complete application
and artifact suites were not repeated after every subsequent source update;
focused checks and source review cover the stated later deltas.

## Limits

No live model call, host-login read or mutation, production service/database,
external issue import, delivery or merge was performed by this reviewer. Root
instructed that real native acceptance wait for a fresh review after this P2 and
the upstream runtime fix are integrated. Author acceptance is not substituted for
independent final proof. A subscription-mode pass also cannot claim live API-key
acceptance when no dedicated key was supplied. Later source requires fresh review.
