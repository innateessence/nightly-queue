# Native factory conformance

Factory execution wraps the existing native adapter; it does not replace the
backend or turn protocol fixtures into provider qualification. Only the builder
gets a writable candidate. Intake, specification, reviews and decision require
read-only worker profiles; visual review additionally requires qualified browser
execution and a source-bound capture.

`require_factory_capabilities(capabilities, roles, browser=False)` accepts only
authenticated `compact-worker-v1` capability responses with capability schema 1,
worker protocol 2 and qualification version 1. The integrated runtime remains
available to ordinary native sessions and historical readback, but cannot qualify
active factory roles. Every selected role names a registered profile and protected
`worker-boundary` qualification with the exact
profile/image, binding, instance and worker boot. The qualification references its
protected probe digest and successful source/control/credential/cleanup checks.
Old boots, fixtures, advertised permission strings and incomplete probes fail.
`validate_role_binding` checks the frozen subset when validating saved server
context; the initial registration check remains mandatory before freezing it.

`FactoryNativeAdapter` uses those profiles with the existing
prepare/start/inspect/events/outcome/cancel/reconcile interface. A start first
reconciles the original operation. An unknown result retains its fence rather
than starting another session. Read-only roles must also export unchanged source.
Terminal success requires the original protected result, actual zero exit,
confirmed termination, untruncated logs, matching session/boot/source and verified
export bytes. A model's final message cannot substitute for that evidence.

Structured role JSON is read from the bounded complete transcript, independently
of the 4,000-character progress preview. Malformed/duplicate/nonfinite JSON,
changed session IDs, missing/duplicate terminal events and invalid token counts
fail. Provider metadata is not labeled token usage, quota or billed cost.

Select the immutable worker image ID for an explicit version-only probe with no
account volumes, network or model execution:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
  --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m --entrypoint python \
  "$VIBEARR_WORKER_IMAGE_ID" -I -m vibearr_runner.native_conformance \
  --provider codex --version-probe
```

The result is labeled `executable-version` and explicitly reports authentication,
worker-boundary and live-provider verification as false. Run this probe in an
isolated qualification job with no provider egress. Its clean HOME/configuration,
deadline, output bound and process-group cleanup are separate from the later
actual worker permission probes and selected-account live acceptance. Passing
offline tests or a version probe must never enable processing or automatic merge.

Implementation plan: [PR #146](https://github.com/innateessence/nightly-queue/pull/146).
Humans review and merge every implementation PR.
