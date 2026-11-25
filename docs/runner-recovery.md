# Runner recovery, cancellation and retention

The trusted runner keeps its journal in the private `runner-data` volume at
`/var/lib/vibearr`. It persists controller/run/operation identities before external
effects and saves durable checkpoints around dispatch, validation and publication.
The app stores scoped run history and immutable public evidence separately.

A lost response is uncertainty, not permission to repeat work. Reconcile the
original operation UUID, worker instance/boot, manifest and protected receipt.
Unknown or unconfirmed execution retains its fence. Terminal success needs actual
confirmed descendant termination and verified export bytes; disappearance alone
does not establish a successful result.

Pause/resume operates on the same task without extending its frozen deadline.
Cancellation requires confirmed cleanup. Controller or worker restart preserves
the original run and cumulative charges and invalidates boot-bound qualifications.
Requalify the new worker boot before further dispatch. Do not silently convert an
old or unknown task into a new invocation.

Delivery intents retain exact repository, base, candidate commit, verified branch
and PR identities. Reconcile an ambiguous GitHub response by those identities
before publishing again. A delivered PR does not mean it was merged or deployed.
Human review releases the worker slot, while dependent work waits for trusted
merge evidence and exact ancestry.

Use operator Activity/History controls to inspect, pause, cancel or recover an
owned run. Configuration/credential changes apply through the scoped server
gates; direct journal edits, database requeue or deleting state are not recovery.
Removed credentials remain revoked; retained private history is available only
for ownership/recovery and redaction checks.

Retention separates source/exports from private logs. Purge only acknowledged,
terminal resources belonging to the expected run. Active, unknown, unconfirmed
and wrong-run work remains protected. Tombstones retain original identities and
digests so a replay cannot restart removed work. Do not blanket-prune images,
volumes or unrelated installations.

Drain before upgrading or taking a quiescent backup. Restore only into empty
volumes in a separate Docker project, retaining the snapshot's own recipe and
credentials. Restore disables processing and replaces controller/worker authority;
old tokens and reports never establish the restored installation's readiness.
See [Docker operations](docker-operations.md) for the supported commands and
[release acceptance](vibearr-mvp.md) for current proof requirements.
