# System logs

Configuration → Logs opens the System Logs category. Runner Logs is reserved for
future work and has no active viewer or collector yet.

System Logs records application events at Info, Warning, Error, and Critical
levels. It includes issue synchronization results, discovery settings changes,
credential creation/update/revocation, and Django request/security warnings and
errors. It does not collect independent runner output, model transcripts, Docker
logs, or HTTP access logs.

Search messages or exception details, combine level and source filters, and use
Newer/Older to browse 50 entries per page. Refresh reloads the current filters.
Times are UTC. Empty and filtered-empty results explain what to do next.

Records live in PostgreSQL and survive application restarts. Expired entries are
hidden after seven days. Each captured event removes expired records and trims
storage to the newest 10,000 entries. Retention applies to all sources together.
System logs are included in the ordinary database backup/restore.

The collector removes configured application/database secrets, current and retained
credential versions, and common token/password patterns before saving. This also
covers revoked credentials and retained material from deleted credentials. It
withholds the entire message if credential material cannot be decrypted safely.
Request logs store only status and the matched route pattern; URLs, query strings,
headers, bodies, exception messages, and source-code lines are not saved. Exception
details contain the exception class and stack frame locations. Log content is
escaped when rendered and responses are not cached. Log access follows the same
operator UI boundary as Settings and Credentials.

The collector preserves existing console logging. Storage failures cannot fail
the caller; records cannot be recovered while the database is unavailable, before
the initial migration, or after the caller's transaction rolls back. Historical
console output is not imported.

Rebuild the application/tools images and apply the new migration before serving:

```sh
docker compose --profile tools run --rm tools python manage.py migrate --noinput
```

The migration adds `system_logs.0001_initial`. No credential, volume, worker,
schedule, or execution-policy changes are required. Keep live execution disabled
until the separate Docker-only qualification work is complete.
