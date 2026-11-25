# Coding runner configuration (schema 1)

Open **Configuration → Coding Runners** to add, edit, or delete named runner
configurations. Each selects one reusable schedule and has its own task-window
limit, polling interval, pause controls, backend, credentials, execution policy,
and retention. There is no configured limit on the number of runners or schedules. Issue import credentials
and intervals remain on **Settings** and are independent of execution schedules.

Open **Configuration → Schedules**, directly below Coding Runners, to create,
edit, and delete named schedules. A schedule owns its processing mode, timezone,
and weekday windows. Choose it in the coding runner's Schedule field; multiple
runners may use the same schedule. Editing it updates every assigned runner at
its next processing gate. Task-window limits are counted separately per runner.
An assigned schedule cannot be deleted until its runners use another schedule.
The default **Always on** schedule remains available for new runners.

The built-in **Default runner** is the fallback configuration. Provision a
dedicated identity using the app image entrypoint and a private runner token:
`docker compose run --rm app python manage.py provision_runner <name>`.
An identity can be assigned to one configuration. Identities without an assignment
use Default runner.
New configurations start disabled and may be saved before an identity is
provisioned. Saving a configuration does not start a supervisor or qualify its
runtime; native execution still requires the registered isolated runtime and
verified adapter described in [Docker operations](docker-operations.md).

Run one independent supervisor deployment per identity, each with its own token
and persistent state directory. The authenticated identity automatically selects
its configuration on polling; no supervisor configuration changes are needed to
select a saved schedule. The queue still permits one active task across all
runners. Overlapping schedules compete for that slot. Assignments cannot change
while an affected identity owns active work. To delete an additional configuration,
first unassign its identity after finishing or canceling work. Unassignment returns
that identity to the default configuration; disable the machine identity first if
it should stop polling. The default configuration cannot be deleted.

Processing windows use IANA timezones and a JSON list such as
`[{"days": [0,1,2,3,4], "start": "22:00", "end": "06:00"}]`. Weekdays use
Monday=0. An overnight window belongs to its starting weekday; equal endpoints
are invalid (choose continuous for all-day operation). The supervisor evaluates
windows, overlapping intervals, DST, task-window counts, and usage reserves using
its selected configuration. No backend is invoked by configuration changes.

Project fields can inherit runner settings, use a project value, or clear a value.
A blank input with inheritance selected inherits from whichever runner claims the
task. Clearing a required value produces structural errors. Overrides cannot
change runner pause, schedule, or reserve controls. The project editor previews
the default runner; each runner's editor shows its own effective project
configuration. Setup/validation commands are lists of strings, executed only
inside the configured isolated task policy.

`GET /api/v1/runner/configuration/` requires a runner identity bearer token, not
the ordinary queue token. It returns `runner_configuration_id`, schema_version,
revision, global, projects, accounts, selected credential metadata, and readiness.
The `global` object contains the authenticated runner's settings. Project entries
contain effective values, sources, structural errors and required capabilities.
No secret ciphertext/plaintext is serialized. The account inventory includes
stage defaults and every project override. There is no import-token fallback.

A canonical SHA-256 revision changes with the selected runner configuration and schedule,
project definitions, overrides and selected credential metadata/rotation timestamps.
Edits to an unassigned schedule, another runner, or the import interval do not alter it. Claim preflight
supplies that revision. Inside the claim transaction,
`locked_configuration_snapshot(project, expected_revision, runner)` locks settings
and credentials, rejects stale/structurally incomplete configuration, and returns
`(revision, snapshot)`. The run retains the selected configuration ID and settings
snapshot. Later gates read that identity's current pause/schedule/reserve controls;
backend, model, instruction and command edits affect future attempts. Native
execution rejects a changed configuration assignment. Configuration/project saves
share the default settings row lock. Direct SQL/bulk updates must coordinate this
lock. Credential delivery validates the selected runner's references and grants.
