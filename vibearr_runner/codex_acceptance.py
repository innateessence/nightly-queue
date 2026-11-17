"""Explicit native fixture acceptance through qualified Docker worker APIs."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import time
from uuid import uuid4

from .bootstrap_files import private_json
from .native_codex import NativeCodexAdapter
from .native_contract import (
    receipt_passed,
    receipt_summary,
    validate_execution_identity,
)
from .native_sessions import NativeSession
from .native_workspace import atomic_bytes
from .policy import ExecutionPolicy, default_execution_policy


def fixture(requirements):
    from vibearr_executor.artifacts import pack

    return pack(
        {
            "answer.py": (0o644, b"def answer():\n    return 0\n"),
            "requirements.txt": (0o644, requirements),
            "coding_runner/__init__.py": (0o644, b""),
            "coding_runner/test_answer.py": (
                0o644,
                b"import unittest\nfrom answer import answer\n\nclass AnswerTest(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 42)\n",
            ),
            ".codex/config.toml": (
                0o644,
                b'model = "not-the-selected-model"\n[mcp_servers.untrusted]\ncommand = "touch"\nargs = ["/work/UNTRUSTED_CONFIG_EXECUTED"]\n',
            ),
        }
    )


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def finished(client, operation, *, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = client.status(operation)
        if observed["state"] in {"exited", "unknown"}:
            return observed
        time.sleep(0.2)
    raise TimeoutError(
        "Native fixture deadline expired; retain its original operation for reconciliation."
    )


def inspect_session(path):
    from vibearr_executor.client import Client

    session = NativeSession.from_data(private_json(path))
    observed = Client.from_environment().status(session.manifest["operation_id"])
    require(
        observed.get("operation_id") == session.manifest["operation_id"],
        "Independent observation returned another operation.",
    )
    identity = validate_execution_identity(observed.get("execution_identity"))
    require(
        identity == session.execution_identity,
        "Independent observation changed the prepared worker identity.",
    )
    return {"pid": os.getpid(), "status": observed}


def independent_observation(path):
    command = [
        sys.executable,
        "-m",
        "vibearr_runner.codex_acceptance",
        "--inspect-session",
        str(path),
    ]
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    process = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode:
        atomic_bytes(
            path.parent / "observer-error.txt",
            (process.stdout + process.stderr)[-65536:].encode(),
        )
        raise RuntimeError(
            "Independent controller observation failed; inspect its private diagnostic."
        )
    value = json.loads(process.stdout)
    require(
        type(value.get("pid")) is int and value["pid"] != os.getpid(),
        "Observation did not run in an independent controller process.",
    )
    return value


def wait_for_command(adapter, session, seconds, *, timeout=45):
    deadline = time.monotonic() + timeout
    pattern = rf"time\.sleep\(\s*{seconds}\s*\)|\bsleep\s+{seconds}\b"
    while time.monotonic() < deadline:
        parser, _ = adapter.read_events(session)
        started = any(
            event.get("kind") == "item.started"
            and event.get("item_type") == "command_execution"
            and re.search(pattern, event.get("command", ""))
            for event in parser.events
        )
        if parser.session_id and started:
            return parser.session_id
        require(
            adapter.inspect(session)["state"] == "running",
            "Native fixture exited before its foreground tool started.",
        )
        time.sleep(0.25)
    raise TimeoutError("Native fixture did not start the requested foreground tool.")


def prepare_validation(client, profile, source, run_id, *, operation_id):
    from vibearr_executor.contracts import digest

    context = b"Trusted credential-free fixture validation."
    client.upload("source", source)
    client.upload("context", context)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "operation_id": operation_id,
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest,
        "source_digest": digest(source),
        "context_digest": digest(context),
        "phase": "validation",
        "limits": asdict(profile.limits),
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
        "network_hosts": [],
    }
    client.prepare(manifest)
    client.start(manifest["operation_id"])
    return manifest


def acceptance(
    *,
    state: Path,
    credential: dict,
    model: str,
    validation_profile,
    requirements: bytes,
) -> dict:
    from vibearr_executor.artifacts import unpack
    from vibearr_executor.client import Client
    from vibearr_executor.contracts import digest

    kind = credential["credential"]["kind"]
    require(
        digest(requirements) == validation_profile.dependency_digest,
        "Use the reviewed requirements bound to the validation image.",
    )
    client = Client.from_environment()
    report = client.capabilities()
    require(
        report.get("qualified") is True and report.get("accepting_work") is True,
        "Qualify idle Docker workers and stop the supervisor before fixture acceptance.",
    )
    policy = default_execution_policy()
    adapter = NativeCodexAdapter(client)
    agent = adapter.selected_profile(ExecutionPolicy.from_data(policy), kind)
    validation = next(
        profile
        for profile in validation_profile.executor_profiles()
        if profile.argv[-1] == "runner"
    )
    registered = {value["profile_id"]: value for value in report["profiles"]}
    require(
        registered.get(validation.profile_id, {}).get("profile_digest")
        == validation.digest,
        "The independent fixture check must use the installed validation recipe.",
    )
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    operations = []
    source = fixture(requirements)
    policy.update(
        runtime_profile=agent.profile_id,
        validation_profile=validation_profile.profile_id,
        network_hosts=list(agent.network_hosts),
    )

    def next_operation():
        value = str(uuid4())
        operations.append(value)
        atomic_bytes(state / "operations.json", json.dumps(operations).encode())
        return value

    try:
        require(
            adapter.diagnostics(policy, credential)["auth_available"],
            "Configured native auth diagnostics failed",
        )
        run_id = str(uuid4())
        session = adapter.prepare(
            run_id=run_id,
            operation_id=next_operation(),
            source=source,
            prompt="This is a bounded fixture. First run python -c 'import time; time.sleep(8)' to leave an observation window. Then edit only answer.py so answer() returns 42. Do not modify coding_runner/test_answer.py, requirements.txt or .codex/config.toml. Run python -m unittest discover -s /work/coding_runner -p test_answer.py and finish. Do not install dependencies or access other repositories.",
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            credential=credential,
        )
        identity = validate_execution_identity(session.execution_identity)
        initial = adapter.start(session)
        require(
            initial["state"] == "running",
            "Native session did not enter the running state",
        )
        require(
            initial.get("execution_identity") == identity,
            "Started fixture differs from its prepared worker identity.",
        )
        wait_for_command(adapter, session, 8)
        session_file = state / "session.json"
        atomic_bytes(session_file, json.dumps(session.to_data()).encode())
        observer = independent_observation(session_file)
        recovered = observer["status"]
        require(
            recovered.get("state") == "running"
            and recovered.get("execution_identity") == identity,
            "Independent controller did not observe the original running worker operation.",
        )
        adapter = NativeCodexAdapter(Client.from_environment())
        restored = NativeSession.from_data(private_json(session_file))
        recovered = adapter.reconcile(restored)
        require(
            recovered.get("execution_identity") == identity,
            "Controller reload changed the native worker identity",
        )
        retried = adapter.start(restored)
        require(
            retried.get("execution_identity") == identity,
            "Idempotent start replaced native execution",
        )
        agent_status = finished(client, session.manifest["operation_id"], timeout=300)
        require(
            receipt_passed(receipt_summary(agent_status)),
            "Native fixture did not produce a passing protected receipt.",
        )
        outcome = adapter.outcome(restored, source)
        require(
            outcome["successful"],
            "Real Codex session failed; inspect bounded executor log in private state",
        )
        exported = unpack(outcome["source"])
        original = unpack(source)
        require(
            set(exported) == set(original),
            "Native fixture left generated source or scratch files in its export",
        )
        require(
            all(
                exported.get(name) == value
                for name, value in original.items()
                if name != "answer.py"
            ),
            "Native fixture changed its independent test or protected inputs.",
        )
        require(
            exported.get(".codex/config.toml") == original[".codex/config.toml"],
            "Native fixture changed project configuration",
        )
        require(
            "UNTRUSTED_CONFIG_EXECUTED" not in exported,
            "Candidate project MCP configuration executed",
        )
        require(
            exported.get("answer.py") != original["answer.py"],
            "Native session did not edit the fixture",
        )
        checked = prepare_validation(
            client, validation, outcome["source"], run_id, operation_id=next_operation()
        )
        checked_result = finished(client, checked["operation_id"])
        require(
            receipt_passed(receipt_summary(checked_result)),
            "Independent offline fixture test failed",
        )
        canceled = adapter.prepare(
            run_id=str(uuid4()),
            operation_id=next_operation(),
            source=source,
            prompt="Run python -c 'import time; time.sleep(90)' in the foreground. Wait for it before editing any file. This run will be canceled by the fixture supervisor.",
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
            credential=credential,
        )
        cancel_started = adapter.start(canceled)
        observed_session = wait_for_command(adapter, canceled, 90)
        time.sleep(0.5)
        stopped = adapter.cancel(canceled)
        require(
            stopped["state"] == "exited" and stopped.get("descendants_terminated"),
            "Native cancellation did not confirm descendant termination",
        )
        require(
            adapter.reconcile(canceled).get("execution_identity")
            == validate_execution_identity(cancel_started.get("execution_identity")),
            "Cancellation recovery changed the operation identity",
        )
        probe = adapter.prepare_auth_probe(
            run_id=str(uuid4()),
            operation_id=next_operation(),
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
            credential=credential,
        )
        adapter.start(probe)
        finished(client, probe.manifest["operation_id"])
        auth_proof = adapter.authentication_result(probe)
        diagnostics = adapter.diagnostics(
            policy, credential, model=model, proof=auth_proof
        )
        require(
            diagnostics["authenticated"] and diagnostics["native_agent_ready"],
            "Bound authentication proof did not qualify selected native account",
        )
        result = {
            "schema_version": 2,
            "runtime": "compose-slot-v2",
            "adapter": "codex",
            "agent_version": adapter.version,
            "auth_kind": kind,
            "api_key_live_acceptance": (
                "passed" if kind == "token" else "not-run-no-dedicated-api-key"
            ),
            "image_digest": agent.image,
            "runtime_binding_digest": report["binding_digest"],
            "worker_bindings": {
                phase: value["binding_digest"]
                for phase, value in report["workers"].items()
            },
            "profile_digest": agent.digest,
            "model": model,
            "operation_id": session.manifest["operation_id"],
            "session_id": outcome["session_id"],
            "execution_identity": identity,
            "controller_reload_same_worker": True,
            "duplicate_start_same_worker": True,
            "controller_observation": {
                "original_pid": os.getpid(),
                "observer_pid": observer["pid"],
                "state": observer["status"]["state"],
            },
            "source_digest": digest(source),
            "result_digest": outcome["artifact"]["result_digest"],
            "log_digest": outcome["log_digest"],
            "usage": outcome["usage"],
            "usage_unit": "tokens",
            "quota_supported": False,
            "independent_validation": {
                "operation_id": checked["operation_id"],
                "exit_code": checked_result["result"]["exit_code"],
                "log_digest": checked_result["result"]["log_digest"],
            },
            "cancellation": {
                "operation_id": canceled.manifest["operation_id"],
                "session_id": observed_session,
                "exit_code": stopped.get("result", {}).get("exit_code"),
                "descendants_terminated": True,
                "foreground_command_started": True,
            },
            "credential_source_written": False,
            "refresh_token_supplied_to_task": False,
            "candidate_mcp_disabled": True,
            "authentication_probe": auth_proof,
            "native_task_acceptance": True,
        }
        return result
    finally:
        unconfirmed = []
        for operation in operations:
            try:
                stopped = client.stop(operation)
                if (
                    stopped.get("state") != "exited"
                    or stopped.get("descendants_terminated") is not True
                ):
                    unconfirmed.append(operation)
            except Exception:
                unconfirmed.append(operation)
        atomic_bytes(
            state / "cleanup.json",
            json.dumps({"operations": operations, "unconfirmed": unconfirmed}).encode(),
        )
        require(
            not unconfirmed,
            "Native fixture cleanup is unconfirmed; retain the original operations for reconciliation.",
        )


def private_secret(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and not info.st_mode & 0o077
            and info.st_size <= 16384,
            "Select a bounded, private, owned native credential file.",
        )
        content = stream.read(16385)
    require(0 < len(content) <= 16384, "Native credential file is empty or oversized.")
    try:
        secret = content.decode().strip()
    except UnicodeDecodeError:
        raise RuntimeError("Native credential must be UTF-8 text.") from None
    require(bool(secret), "Native credential file is empty.")
    return secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--inspect-session", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--kind", choices=("token", "session"), default="token")
    parser.add_argument("--model")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("/run/vibearr/runner/validation-profile.json"),
    )
    parser.add_argument(
        "--requirements", type=Path, default=Path("/app/requirements.txt")
    )
    args = parser.parse_args()
    if args.inspect_session:
        if any(
            (args.execute, args.state, args.credential_file, args.model, args.evidence)
        ):
            parser.error(
                "Inspect a saved session without requesting new acceptance work."
            )
        print(json.dumps(inspect_session(args.inspect_session)))
        return
    if not args.execute or any(
        value is None
        for value in (args.state, args.credential_file, args.model, args.evidence)
    ):
        parser.error(
            "Explicitly select --execute, a new --state, --credential-file, --model and --evidence."
        )
    if args.evidence.exists() or args.evidence.is_symlink():
        parser.error("Select a new acceptance evidence path; preserve prior results.")
    from vibearr_executor.validation_profile import ValidationProfile

    credential = {
        "credential": {"id": 1, "provider": "codex", "kind": args.kind, "version": 1},
        "material": {"secret": private_secret(args.credential_file)},
    }
    report = acceptance(
        state=args.state,
        credential=credential,
        model=args.model,
        validation_profile=ValidationProfile.parse(private_json(args.profile)),
        requirements=args.requirements.read_bytes(),
    )
    content = json.dumps(report, sort_keys=True).encode() + b"\n"
    atomic_bytes(args.evidence, content)
    print(
        json.dumps(
            {
                "native_task_acceptance": True,
                "auth_kind": args.kind,
                "evidence_sha256": hashlib.sha256(content).hexdigest(),
                "api_key_live_acceptance": report["api_key_live_acceptance"],
            }
        )
    )


if __name__ == "__main__":
    main()
