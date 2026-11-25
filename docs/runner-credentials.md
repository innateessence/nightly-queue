# Runner credential boundary

Runner authentication is provisioned separately with `provision_runner`; the generic
`VIBEARR_API_TOKEN` cannot retrieve secrets. The runner never receives Django's encryption
key or database credentials. Ordinary configuration and credential pages contain only
names, IDs, provider/type, rotation version, revocation status and timestamps.
Execution remains release-gated.

The trusted runner uses **Runner subscription login** credential references
for Codex/Claude Code. Sign in, reconnect and log out inside the runner using the
[current operations procedure](docker-operations.md#subscription-login). Login
files stay in the private runner volume; the app holds only the selected reference.
OpenCode connections select external Ollama and an installed model, without a
model API key. GitHub/GitLab repository tokens and SSH material remain separate.
A provider/type cannot be changed after creation. Replace credentials to change it.

Historical token/session/cloud connection records are retained for migration and
redaction. They do not enable model API authentication in the integrated native
path. Only managed subscription login and explicit external Ollama pass readiness.
Subscription auth files are copied into the selected agent's temporary home;
supported refresh is collected after descendant termination. That agent's tools
can read their own login. Trusted control/Git credentials remain inaccessible.
Saving any connection is not live login, model, allowance or coding acceptance.

## Selected secret API

`POST /api/v1/runner/secrets/`, runner Bearer authentication, accepts one of:

```json
{"scope":"preflight","backend":"codex","credential_id":1}
```

```json
{"scope":"run","run_id":"<uuid>","claim_token":"<uuid>","purpose":"research"}
```

Preflight requires a `RunnerPreflightGrant` for the authenticated runner and selected credential.
Provision the runner identity with `provision_runner`, create the backend credential
and select it in runner/project settings, then add a Runner preflight grant in Django
admin choosing that runner and credential. Removing the grant blocks future preflight
delivery; granting an unselected credential never permits delivery.

Preflight permits only backend accounts present in the current authenticated
configuration inventory, including project overrides. Issue and repository tokens
are delivered only for owned active work. Run purposes are `research`,
`implementation`, `issue`, `repository`; their IDs are resolved server-side from
both the immutable run snapshot and current project selections. Changing a selected
ID or backend stops old-run retrieval. There is no fallback to import selections
or a project's general repository credential. Foreign ownership returns 404;
stale/requeued/finished claims return 409; unavailable selections return redacted
validation errors. Responses, including authentication failures, must be non-cacheable.

Success returns `schema_version: 1`, `credential: {id, provider, kind, version}`,
and encrypted-at-rest material decrypted for this response as
`material: {secret, passphrase}`. Structured material remains a JSON string in
`secret`. Supervisors must keep this material outside agent prompts, command lines,
environments, saved plans, evidence and logs. Delivery is not authorization to expose
material to a backend process without its separate verified isolation boundary.

Editing a credential rotates it in place and increments `secret_version` only when
the encrypted payload changes. A later retrieval for the same selected ID receives
the latest version, including active runs. Revocation is permanent for that row and
blocks subsequent deliveries; create/select a new credential to restore access.
Previously delivered credentials cannot be recalled: stop existing backend processes
and revoke them at the provider separately. Credential changes lock runner settings
before credential rows, matching claim snapshot lock ordering. Metadata-only saves
preserve a concurrent rotation and its version. Global selected
references use PROTECT; project JSON replacement references are protected during
instance and bulk deletion by a pre-delete check under the settings lock. Effective
credential references in every retained run snapshot are also protected after live
settings are changed, including completed and obsolete runs. This retains the material
needed for safe reporting/recovery and historical readbacks. Admin deletion previews
name blocking projects and runs without decrypting their credential references.

Evidence, progress references, and finish errors redact the owned active run's selected
credential values before storage or response. This includes raw material, SSH passphrases,
Codex access/refresh/ID tokens, and OpenCode API keys, while retaining provider and
endpoint metadata. The helper reads only IDs from the run's immutable effective snapshot;
it never decrypts unrelated credentials. Missing encryption or invalid selected material
fails reporting closed with a redacted error. Completed finish retries compare the saved
input digest and require no secret decryption.

Rotation retains each previous ciphertext, provider/type, and version in the
credentials-owned `CredentialSecretVersion` table. This permits redacting logs from an
older backend process and replaying evidence writes after rotation. Retained versions
are never delivered by the secret endpoint, serialized with runs/configuration, or
registered for admin browsing. They protect their credential from deletion; Django admin
shows the blocking references. Retention is indefinite until an operator removes that
history through trusted maintenance after associated run evidence and backend processes
no longer need it. Backups must preserve the encryption key and these encrypted records.
This protects accidental plaintext log echoes; the privileged supervisor still owns the
boundary against exposing material to agents or external tools.

Apply credential migrations 0005, 0006, and 0007 before deployment. Existing encrypted
payloads are preserved and initialize at version 1 with no revocation; version history
is created by subsequent rotations. No real credentials or live backend calls are used
by deterministic tests.


Validation: the PostgreSQL suite includes selected-secret endpoint cases, credential
storage/form/UI tests, schema upgrade preservation, old-version report redaction and
retry reconciliation, and rotation/revocation races against secret delivery.
Black, Ruff, mypy, Django checks and migration drift checks pass. The standalone
CLI/runtime suites also pass. The rendered credential UI is recorded in
probe or backend invocation was performed; execution remains disabled pending the
separate adapter/sandbox/release verification work.
