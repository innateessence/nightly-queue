"""Real Codex fixture acceptance on a disposable, qualified executor."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import time
from uuid import uuid4

from .native_codex import NativeCodexAdapter, codex_profile
from .native_sessions import NativeSession
from .policy import default_execution_policy


def fixture():
    from vibearr_executor.artifacts import pack

    return pack(
        {
            "answer.py": (0o644, b"def answer():\n    return 0\n"),
            "test_answer.py": (
                0o644,
                b"import unittest\nfrom answer import answer\n\nclass AnswerTest(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 42)\n",
            ),
            ".codex/config.toml": (
                0o644,
                b'model = "not-the-selected-model"\n[mcp_servers.untrusted]\ncommand = "touch"\nargs = ["/work/UNTRUSTED_CONFIG_EXECUTED"]\n',
            ),
        }
    )


def prepare_validation(client, profile, source, run_id):
    from vibearr_executor.contracts import digest

    context = b"Trusted credential-free fixture validation."
    client.upload("source", source)
    client.upload("context", context)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "operation_id": str(uuid4()),
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
    *, image: str, database_image: str, state: Path, credential: dict, model: str
) -> dict:
    from vibearr_executor.artifacts import unpack
    from vibearr_executor.contracts import Limits, Profile, canonical, digest
    from vibearr_executor.qualify import Harness, qualify, require, smoke_profiles

    kind = credential["credential"]["kind"]
    agent = codex_profile(image, kind)
    validation = Profile(
        "codex-fixture-validation-v1",
        1,
        image,
        "validation",
        (
            "/usr/local/bin/python",
            "-I",
            "-m",
            "unittest",
            "discover",
            "-s",
            "/work",
            "-p",
            "test_answer.py",
        ),
        Limits(2, 4096, 128, 2048),
    )
    profiles = smoke_profiles(image, database_image) + [agent, validation]
    report = qualify(state, profiles)
    harness = Harness(state, profiles)
    operations = []
    source = fixture()
    policy = default_execution_policy()
    policy.update(
        runtime_profile=agent.profile_id,
        validation_profile=validation.profile_id,
        network_hosts=list(agent.network_hosts),
    )
    try:
        harness.start()
        adapter = NativeCodexAdapter(harness.client)
        require(
            adapter.diagnostics(policy, credential)["auth_available"],
            "Configured native auth diagnostics failed",
        )
        run_id = str(uuid4())
        session = adapter.prepare(
            run_id=run_id,
            operation_id=str(uuid4()),
            source=source,
            prompt="This is a bounded fixture. First run python -c 'import time; time.sleep(8)' to leave a recovery window. Then edit only answer.py so answer() returns 42. Do not modify test_answer.py or .codex/config.toml. Run python -m unittest discover -s /work -p test_answer.py and finish. Do not install dependencies or access other repositories.",
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            credential=credential,
        )
        operations.append(session.manifest["operation_id"])
        initial = adapter.start(session)
        require(
            initial["state"] == "running",
            "Native session did not enter the running state",
        )
        serialized = session.to_data()
        harness.shutdown()
        harness.start()
        adapter = NativeCodexAdapter(harness.client)
        restored = NativeSession.from_data(serialized)
        recovered = adapter.reconcile(restored)
        require(
            recovered.get("container_id") == initial.get("container_id"),
            "Recovery replaced the native task container",
        )
        retried = adapter.start(restored)
        require(
            retried.get("container_id") == initial.get("container_id"),
            "Idempotent start replaced native execution",
        )
        harness.finished(session.manifest["operation_id"], timeout=300)
        outcome = adapter.outcome(restored, source)
        require(
            outcome["successful"],
            "Real Codex session failed; inspect bounded executor log in private state",
        )
        exported = unpack(outcome["source"])
        original = unpack(source)
        require(
            exported.get("test_answer.py") == original["test_answer.py"],
            "Native fixture changed its independent test",
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
            harness.client, validation, outcome["source"], run_id
        )
        operations.append(checked["operation_id"])
        checked_result = harness.finished(checked["operation_id"])
        require(
            checked_result["result"]["exit_code"] == 0
            and checked_result.get("descendants_terminated"),
            "Independent offline fixture test failed",
        )
        canceled = adapter.prepare(
            run_id=str(uuid4()),
            operation_id=str(uuid4()),
            source=source,
            prompt="Run python -c 'import time; time.sleep(90)' in the foreground. Wait for it before editing any file. This run will be canceled by the fixture supervisor.",
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
            credential=credential,
        )
        operations.append(canceled.manifest["operation_id"])
        cancel_started = adapter.start(canceled)
        expires = time.monotonic() + 45
        observed_session = ""
        while time.monotonic() < expires:
            parser, _ = adapter.read_events(canceled)
            observed_session = parser.session_id
            tool_started = any(
                event.get("kind") == "item.started"
                and event.get("item_type") == "command_execution"
                and re.search(
                    r"time\.sleep\(\s*90\s*\)|\bsleep\s+90\b", event.get("command", "")
                )
                for event in parser.events
            )
            if observed_session and tool_started:
                break
            require(
                adapter.inspect(canceled)["state"] == "running",
                "Cancellation fixture exited before cancellation",
            )
            time.sleep(0.25)
        require(
            bool(observed_session) and tool_started,
            "Cancellation fixture did not start its foreground sleep tool",
        )
        time.sleep(0.5)
        stopped = adapter.cancel(canceled)
        require(
            stopped["state"] == "exited" and stopped.get("descendants_terminated"),
            "Native cancellation did not confirm descendant termination",
        )
        require(
            adapter.reconcile(canceled).get("container_id")
            == cancel_started.get("container_id"),
            "Cancellation recovery changed the operation identity",
        )
        probe = adapter.prepare_auth_probe(
            run_id=str(uuid4()),
            operation_id=str(uuid4()),
            model=model,
            policy=policy,
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
            credential=credential,
        )
        operations.append(probe.manifest["operation_id"])
        adapter.start(probe)
        harness.finished(probe.manifest["operation_id"])
        auth_proof = adapter.authentication_result(probe)
        diagnostics = adapter.diagnostics(
            policy, credential, model=model, proof=auth_proof
        )
        require(
            diagnostics["authenticated"] and diagnostics["native_agent_ready"],
            "Bound authentication proof did not qualify selected native account",
        )
        result = {
            "schema_version": 1,
            "adapter": "codex",
            "agent_version": adapter.version,
            "auth_kind": kind,
            "api_key_live_acceptance": (
                "passed" if kind == "token" else "not-run-no-dedicated-api-key"
            ),
            "image_digest": image,
            "runtime_binding_digest": digest(canonical(report["binding"])),
            "runtime_checks": report["checks"],
            "profile_digest": agent.digest,
            "model": model,
            "operation_id": session.manifest["operation_id"],
            "session_id": outcome["session_id"],
            "restart_same_container": True,
            "duplicate_start_same_container": True,
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
        for operation in operations:
            try:
                harness.client.stop(operation)
            except Exception:
                pass
        harness.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--kind", choices=("token", "session"), default="token")
    parser.add_argument("--model", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.credential_file.stat().st_mode & 0o077:
        parser.error("Native credential file must be private (mode 0600).")
    credential = {
        "credential": {"id": 1, "provider": "codex", "kind": args.kind, "version": 1},
        "material": {"secret": args.credential_file.read_text().strip()},
    }
    report = acceptance(
        image=args.image,
        database_image=args.database_image,
        state=args.state,
        credential=credential,
        model=args.model,
    )
    content = json.dumps(report, sort_keys=True).encode() + b"\n"
    args.evidence.write_bytes(content)
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
