"""One-shot, credential-free tools task with a separate trusted task UID."""

from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from vibearr_executor.contracts import Limits, Manifest, Profile, digest
from vibearr_executor.artifacts import unpack
from vibearr_executor.slot_safety import binding
from vibearr_executor.slots import SlotRuntime
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.native_contract import receipt_passed, receipt_summary

from .git_input import ancestry, freeze_worktree, repository


def main():
    os.umask(0o077)
    if Path("/input-source/git-input").exists():
        checkout, selected = repository(
            Path("/input-source/git-input"),
            Path("/tmp/candidate.git"),
            os.environ.get("VIBEARR_CHECK_COMMIT") or None,
        )
        ancestry(
            checkout,
            os.environ.get("VIBEARR_CHECK_BASE"),
            os.environ.get("VIBEARR_CHECK_INTRODUCTION"),
        )
        source = CandidateSource.from_git(checkout, selected).archive
    else:
        source = freeze_worktree(
            Path("/input-source/source"), Path("/tmp/source")
        ).archive
        selected = None
    profile = Profile(
        "offline-distributions",
        1,
        os.environ["VIBEARR_CHECK_IMAGE_ID"],
        "setup",
        (
            "/opt/application-python/bin/python",
            "-I",
            "/opt/executor/vibearr_executor/packaging_checks.py",
        ),
        Limits(2, 2048, 128, 1024),
    )
    runtime = SlotRuntime(Path("/control"), [profile])
    try:
        observed_binding = binding(runtime)
        runtime.qualifying = True
        context = b"{}"
        for kind, content in (("source", source), ("context", context)):
            runtime.upload(kind, content, digest(content))
        operation = str(uuid4())
        manifest = Manifest(
            1,
            str(uuid4()),
            operation,
            profile.profile_id,
            profile.digest,
            digest(source),
            digest(context),
            "setup",
            profile.limits,
            (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            (),
        )
        runtime.prepare(manifest.data())
        runtime.start(operation)
        deadline = time.monotonic() + 910
        while time.monotonic() < deadline:
            result = runtime.status(operation)
            if result["state"] in {"exited", "unknown"}:
                break
            time.sleep(0.2)
        else:
            runtime.stop(operation)
            raise TimeoutError("Offline packaging exceeded its fixed deadline.")
        content = bytearray()
        while len(content) < 1024**2:
            chunk = runtime.logs(operation, len(content), 65536)
            if not chunk:
                break
            content.extend(chunk)
        print(content.decode(errors="replace"), flush=True)
        actual = result.get("result", {})
        passed = (
            result.get("descendants_terminated") is True
            and actual.get("exit_code") == 0
        )
        passed = passed and not any(
            actual.get(key)
            for key in ("timed_out", "canceled", "log_truncated", "export_error")
        )
        receipt = receipt_summary(result)
        passed = (
            passed and receipt.get("schema_version") == 2 and receipt_passed(receipt)
        )
        report = {
            "kind": "offline-container-packaging",
            "commit_sha": selected,
            "source_digest": digest(source),
            "passed": passed,
            "exit_code": actual.get("exit_code"),
            "export_error": actual.get("export_error"),
            "binding": observed_binding,
            "production_qualification": False,
            "native_receipt_version": receipt.get("schema_version"),
            "execution_identity": receipt.get("execution_identity"),
        }
        if not passed:
            print(json.dumps(report), flush=True)
            raise SystemExit(1)
        _inventory, exported = runtime.export(operation)
        artifact_proof = unpack(exported)["packaging-proof.json"][1]
        proof = json.loads(artifact_proof)
        record = runtime.record(operation)
        keys = (
            "manifest",
            "operation_id",
            "state",
            "descendants_terminated",
            "result",
            "worker_boot",
            "execution_identity",
            "receipt_digest",
        )
        payload = {
            "report": report,
            "wheels": proof["wheels"],
            "commands": proof["commands"],
            "artifact_proof": base64.b64encode(artifact_proof).decode(),
            "operation": {key: record[key] for key in keys},
        }
        print(
            json.dumps({"kind": "actual-packaging-proof", "payload": payload}),
            flush=True,
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
