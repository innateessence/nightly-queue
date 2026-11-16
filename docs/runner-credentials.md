# Runner credential boundary

Runner authentication is provisioned separately with `provision_runner`; the generic
`NQ_API_TOKEN` cannot retrieve secrets. The runner never receives Django's encryption
key or database credentials. Ordinary configuration and credential pages contain only
names, IDs, provider/type, rotation version, revocation status and timestamps.
Execution remains release-gated.

Credentials supports GitHub/GitLab token, SSH and generic encrypted material; Codex
API token or structured session; Claude Code token; OpenCode structured connection.
A provider/type cannot be changed after creation. Replace credentials to change it.

Codex session material accepts the known `auth.json` ChatGPT subset: `tokens` with
nonempty `access_token`, `refresh_token`, `id_token`, `account_id`; optional
`auth_mode: "chatgpt"`, `OPENAI_API_KEY: null`, and string `last_refresh`. Other
formats fail closed. This validates storage, not login freshness or tool support.
The shape is grounded in the [upstream Codex authentication implementation](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs).

OpenCode connection JSON contains `provider` (`openai`, `anthropic`, `opencode`, or
`ollama`), an `api_key`, and optional `base_url`. Ollama requires an explicit HTTP(S)
URL and permits no API key. URLs cannot contain userinfo, query strings or fragments.
These are application connection fields; a verified adapter must translate them
into its pinned tool's configuration, never copy arbitrary JSON to tool settings.
See [OpenCode provider documentation](https://opencode.ai/docs/providers/).
Claude stored tokens do not establish allowance support or grant permission to
copy tokens into agent environments. Interactive refresh, undocumented session
formats and live adapter compatibility remain outside this storage boundary.

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
before credential rows, matching claim snapshot lock ordering. Global selected
references use PROTECT; project JSON replacement references are protected during
instance and bulk deletion by a pre-delete check under the settings lock.

Apply credential migrations 0005 and 0006 before deployment. It preserves existing encrypted
payloads and initializes version 1 with no revocation. No real credentials or live
backend calls are used by deterministic tests.


Validation: the PostgreSQL suite passes 482 tests, including 26 selected-secret
endpoint cases, credential storage/form/UI tests and schema upgrade preservation.
Black, Ruff, mypy, Django checks and migration drift checks pass. The standalone
CLI/runtime suites also pass. The rendered credential UI is recorded in
[screenshot](screenshots/runner-credentials.png). No live provider login, allowance
probe or backend invocation was performed; execution remains disabled pending the
separate adapter/sandbox/release verification work.
