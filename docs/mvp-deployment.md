# Deploy the MVP

The web app, PostgreSQL and issue importer run in Compose. A persistent user
service owns the native supervisor and a second service owns the local executor.
Only the executor accesses Docker. Task containers receive a source archive and
the selected coding credential; the live checkout, database and control tokens
stay outside them.

Keep deployment state under the ignored, mode0700 `.runtime/` directory. Store
database backups, environment files, tokens, profiles and journals there with
mode0600 files. Use a separate immutable release checkout for the services;
changing the web checkout must not change a running supervisor's Python modules.

Before updating, stop new claims and confirm that active runs have finished or
have protected termination receipts. Stop the importer and take a PostgreSQL
custom-format backup plus a private copy of `.env`. Preserve the existing
Compose project and database volume names. Pull the accepted main revision,
build the app, apply migrations, and start the web app and importer.

Follow [bootstrap](self-development-bootstrap.md) to explicitly select the
repository, coding connection, runner identity and grants. Existing connections
and historical queue data remain intact. Enabling autonomous discovery creates
a genuine current cutoff; do not backdate it to admit historical issues.

Build or reuse the reviewed immutable agent and validation images. Qualify the
actual executor source and **all generated profiles** in the persistent executor
state directory before starting the service. Reuse only image bytes, never
another executor's journal or qualification file. See
[isolated execution](isolated-executor.md) for the qualification command.

Save `.runtime/service.env` with the dedicated runner API token, queue URL,
runner state directory, executor URL/token-file, validation profile path and
Docker connection. Use canonical `VIBEARR_*` names. Do not commit that file.
The executor binds only to loopback; the supervisor uses the web app's published
loopback port. Configure the same generated registry used during qualification.

Render the service units with the installed Python3.12 executable:

```sh
python deploy/render_services.py \
  --release /absolute/path/to/immutable/release \
  --runtime /absolute/path/to/.runtime \
  --python /absolute/path/to/.runtime/venv/bin/python
systemd-analyze --user verify /absolute/path/to/.runtime/units/*.service
systemctl --user link /absolute/path/to/.runtime/units/*.service
systemctl --user enable --now vibearr-executor.service vibearr-supervisor.service
```

Enable user lingering when this host must continue processing after logout.
Check authenticated executor capabilities, the supervisor journal and startup
diagnostics before enabling claims. Configure the automatic-merge profile with
the four required workflow identities, strict checks, squash merge and no
ruleset bypass. AI review statuses are optional; the MVP runs with `reviews: []`.
There is no command, plan or per-task approval step.

Verify the actual two-issue MVP, its screenshots and recorded merge identities.
Restart both services and confirm that they retain the same executor instance
and controller ownership. Finally verify the web app, migrations, discovery and
background service health against the deployed revision. A merged source change
becomes the next task's base; updating these running services is a deployment.

For rollback, stop claims and services, restore the matching source/environment
and database backup, and start the previous release. Do not combine an older
application with a database whose migrations it does not understand.
