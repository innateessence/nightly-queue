# Native runtime controls

The supervisor controls an existing executor operation by its recorded UUID.
`Client.pause(id)` freezes its original container; `Client.resume(id)` unfreezes
that container only when its launch binding, registered profile and current
qualification still match. Neither call creates an invocation or extends the
manifest deadline. Retry the same UUID after a lost response. `stop(id)` remains
available after profile or qualification changes. Unknown or replaced identities
retain their execution fence until termination is confirmed.

The operation state remains `not-started`, `running`, `exited` or `unknown`.
`paused` identifies a frozen running container; it does not imply termination.
Status reads avoid executing commands inside a frozen container. The host monitor
checks the original UTC deadline and a persisted same-boot monotonic bound. It
force-removes expired paused tasks because their in-container deadline timer is
also frozen. This outcome has no verified workspace export. Prepared tasks that
never launch also expire, erase their selected secret and become terminal without
creating a container. A host reboot conservatively expires retained deadlines.
The executor service must remain running to enforce host deadlines; a stopped
service reconciles expiry when it restarts.

Capabilities expose `instance_id`, the private execution-state UUID. It survives
a same-state service restart and changes for an empty replacement state directory.
The supervisor must persist it before prepare and compare it during recovery;
matching host/image qualification alone cannot establish operation continuity.

`Client.pause_dependencies(build_id)` records an independent, durable phase gate.
It pauses active setup through the operation API and prevents entry into the next
assembly or qualification phase. A fixed copy/commit already in progress, or an
existing trusted qualification and cleanup, can settle at a checkpoint. During
that interval `pause_state` reports `pausing-at-checkpoint`; it reports `paused`
only once settled. No replacement build or image commit is created. Resume uses
`Client.resume_dependencies(build_id)`, verifies the parent binding and current
qualification, and cannot overwrite a newer pause intent. The original build
deadline continues throughout the pause. Stop remains available at every phase.

Ready and safely canceled build responses include `resources_terminated: true`,
`receipt_digest`, and a protected `termination_receipt`. The receipt binds the
executor instance, run/build identities, original setup receipt, assembly absence
and qualification cleanup. A state label alone does not prove termination.
Failed or unknown builds require successful stop/reconciliation before producing
this receipt. Its digest remains available after retention.

Stopping an ID that was never prepared returns HTTP 404 (`ExecutorNotFound`).
Ordinary operation status keeps its existing `not-started` sentinel. A supervisor
may use 404 as absence evidence only for an undispatched prepared intent after
matching the persisted `instance_id`. Dispatched work still requires confirmed
termination; 404 cannot release its fence.

## Retention

Only an authenticated supervisor acting on an acknowledged retention candidate
should request removal. Both APIs require the expected owning run UUID:

```python
client.purge(operation_id, run_id=run_id, resources=("source",))
client.purge_dependencies(build_id, run_id=run_id, resources=("source", "logs"))
```

The default is both resources. Resource lists must be nonempty, unique, and contain
only `source` and `logs`. `source` covers inputs, context, workspace exports,
dependency payloads and a build's own unreferenced derived image; `logs` covers
protected log payloads. Their retention windows can differ. Running, unknown,
unconfirmed, and wrong-run resources are rejected. Shared content stays until its
last retained source reference is removed. Derived images registered by an
operator or referenced by another retained operation/build cannot be deleted.
There is no general image-prune endpoint.

Removal intent and terminal identity persist across interrupted requests.
Readback preserves the original manifest and receipt/artifact digests, plus
`resources_removed`, `removed_at` and a full-removal `tombstone`. Logs or exports
that were removed return HTTP 410, represented by `ExecutorArtifactRemoved` in the
SDK. Replaying prepare/start for a tombstoned UUID returns the terminal record;
it never starts new work. Full removal releases the live journal admission count
(1,000 operations or 32 builds); partial removal and unknown work still consume
capacity. Compact tombstones remain bounded by the journal's storage limit.

Qualification version 3 preserves the previous eleven boundary checks and adds
`pause-resume-deadline` and `terminal-retention`. These exercise real OCI tasks,
service restart, same PID/container identity, frozen writes, forced expiry,
never-started secret erasure, and selective/shared-reference retention. Rerun host
qualification after upgrading the executor; protocol version 1 remains unchanged.

## Implementation evidence

[Recorded host evidence](evidence/native-runtime-controls.json) comes from actual
rootless Docker 29.9.3 on cgroup v2, using owned disposable executor state and
PostgreSQL containers. All thirteen boundary probes passed. An additional real
operation retained its original identity after discarded successful pause/resume
responses and service downtime. A replacement container with the same name was
left untouched; status, stop, resume and purge retained the original fence until
the fixture replacement was removed and original termination reconciled.

The installed wheel also qualified the registered validation image and built an
actual `idna==3.10` dependency layer. It paused before setup, during setup and
between phases; restarted with setup frozen; then restarted while an existing
qualification settled under pause. The ready image stayed unavailable until
resume. Selective source/log purge removed that exact derived image, retained its
terminal identity, and preserved the operator parent image. No coding-model call,
production stack, production database or external repository delivery was used.
An additional never-launched paused build expired on its original deadline,
confirmed setup/resource termination and returned stable cancellation. Actual
never-prepared stop requests returned 404 under the same persisted instance ID.

Validation included 161 executor unit tests, 1,612 Django tests (four expected
skips), four distribution artifact tests, repository-wide Black/Ruff/mypy,
migration and system checks, and an installed executor wheel outside the checkout. Mocked cases additionally cover
engine uncertainty, binding changes, independent phase gates, resume generation
races, shared references and admission after full purge at both live-record caps.
