# Coding runner configuration (schema 1)

Settings now contains independent issue-import and Coding runner forms. Execution
starts disabled. Saving enabled does not authorize live execution: API readiness
remains false until controlled release verification and runtime gates exist.
Backend credential provider support and secret delivery arrive separately.

Processing windows use IANA timezones and a JSON list such as
`[{"days": [0,1,2,3,4], "start": "22:00", "end": "06:00"}]`. Weekdays use
Monday=0. An overnight window belongs to its starting weekday; equal endpoints
are invalid (choose continuous for all-day operation). Schedule evaluation,
overlap merging, DST behavior and next-opening display are execution-gate work;
no backend is invoked by configuration changes. Task-window counting and paused
time accounting are likewise enforced by those future gates.

Each project field has an explicit source: Inherit global, Use project value, or
Clear value. A blank input with Inherit global inherits. Clear removes an inherited
instruction/command/credential/model/backend; clearing a required value produces
structural errors. Overrides cannot change global pause, schedule or reserve.
Project create/edit shows saved effective values and missing configuration.
Setup/validation commands are lists of strings, never executed by these forms.
A saved command does not bypass approval for commands from untrusted sources.

`GET /api/v1/runner/configuration/` requires a runner identity bearer token, not the
ordinary queue token. It returns schema_version, revision, global, projects,
accounts, selected credential metadata, and execution_ready=false. Project entries
contain effective values, sources, structural errors and required capabilities.
No secret ciphertext/plaintext is serialized. The account inventory includes
stage defaults and every project override, so a reserve gate cannot omit accounts
introduced only by project overrides. There is no import-token fallback.

A canonical SHA-256 revision changes with execution settings, project definitions,
overrides and selected credential metadata/rotation timestamps. Import interval
changes do not alter it. Claim preflight supplies that revision. Inside the claim
transaction, `locked_configuration_snapshot(project, expected_revision)` locks
the singleton and credentials, rejects stale/structurally incomplete configuration,
and returns `(revision, snapshot)`. The run retains that snapshot as settings
change. Later gates read current global pause/schedule/reserve; backend, model,
instruction and command edits affect future attempts. Configuration/project saves
share the singleton lock. Direct SQL/bulk updates must coordinate this lock.
Credential deletion from an override fails closed as missing configuration; scoped
credential delivery must validate references again and handle revocation.

Apply the runner_control migrations before serving these pages. No environment
variables replace routine web configuration. Bootstrap identity credentials remain
infrastructure configuration.
