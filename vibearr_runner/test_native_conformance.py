import importlib.util
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.native_conformance import (
    FACTORY_ROLES,
    MAX_RESULT,
    FactoryNativeAdapter,
    executable_probe,
    parse_role_result,
    probe_source_access,
    require_factory_capabilities,
    source_access,
    validate_browser_capture,
    validate_role_binding,
)
from vibearr_runner.native_sessions import (
    NativeBackendRegistry,
    NativeSession,
    NativeSessionError,
)
from vibearr_runner.policy import ExecutionPolicy, default_execution_policy


SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.artifacts import pack, result_manifest
    from vibearr_executor.contracts import Limits, Profile
    from vibearr_executor.integrated_profiles import ARGV, RUNTIME, VERSIONS
    from vibearr_runner.native_cli import NativeCLIAdapter


def transcript(provider, payload=None, *, session=None):
    session = session or (
        "ses_" + uuid4().hex if provider == "opencode" else str(uuid4())
    )
    text = json.dumps(payload or {"verdict": "pass", "findings": []})
    if provider == "codex":
        events = [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        ]
    elif provider == "claude":
        events = [
            {"type": "system", "session_id": session},
            {
                "type": "result",
                "session_id": session,
                "subtype": "success",
                "is_error": False,
                "result": text,
                "usage": {"input_tokens": 10},
            },
        ]
    else:
        events = [
            {"type": "step_start", "sessionID": session},
            {"type": "text", "sessionID": session, "part": {"text": text}},
            {
                "type": "step_finish",
                "sessionID": session,
                "part": {"reason": "stop", "tokens": {"input": 10, "output": 3}},
            },
        ]
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


class RecordedExecutor:
    """Protocol fixtures do not produce a persisted worker qualification."""

    integrated = True
    runtime = "compact-worker-v1"

    def __init__(self):
        self.instance, self.boot = str(uuid4()), str(uuid4())
        self.binding, self.image = "b" * 64, "sha256:" + "a" * 64
        self.profiles = {
            role: Profile(
                "factory-" + role,
                1,
                self.image,
                "agent",
                ARGV,
                Limits(2, 4096, 128, 2048),
                ("chatgpt.com",),
                "VIBEARR_NATIVE_CONNECTION",
                self.image,
            )
            for role in FACTORY_ROLES
        }
        self.prepared, self.content, self.uploads = {}, b"", {}
        self.state, self.starts, self.receipt = "not-started", 0, None
        self.source = pack({"answer.py": (0o644, b"answer = 1\n")})
        self.exported = self.source

    def capabilities(self):
        roles = {}
        for role, profile in self.profiles.items():
            access = source_access(role)
            checks = {
                "control_paths_denied": True,
                "credentials_scoped": True,
                "descendants_terminated": True,
            }
            checks[
                "candidate_write_only" if role == "build" else "source_write_denied"
            ] = True
            roles[role] = {
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_access": access,
                "qualification": {
                    "schema_version": 1,
                    "kind": "worker-boundary",
                    "role": role,
                    "source_access": access,
                    "profile_digest": profile.digest,
                    "image_digest": self.image,
                    "runtime_binding_digest": self.binding,
                    "instance_id": self.instance,
                    "worker_boot": self.boot,
                    "passed": True,
                    "proof_digest": "f" * 64,
                    "checks": checks,
                },
            }
        return {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": 1,
            "runtime": self.runtime,
            "qualified": True,
            "accepting_work": True,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": self.binding,
            "factory_roles": roles,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in self.profiles.values()
            ],
        }

    def identity(self):
        return {
            "runtime": self.runtime,
            "protocol_version": 2,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": self.binding,
        }

    def request(self, _path):
        return {"available": True, "login_digest": "d" * 64}

    def upload(self, kind, content):
        self.uploads[kind] = content
        return {"sha256": hashlib.sha256(content).hexdigest()}

    def prepare(self, manifest):
        operation = manifest["operation_id"]
        if operation in self.prepared and self.prepared[operation] != manifest:
            raise NativeSessionError("Manifest replay changed")
        self.prepared[operation] = copy.deepcopy(manifest)
        return self.status(operation)

    def supply_secret(self, _operation, _secret):
        return None

    def start(self, operation):
        if self.state == "not-started":
            self.starts += 1
            self.state = "running"
        return self.status(operation)

    def status(self, operation):
        value = {
            "operation_id": operation,
            "state": self.state,
            "execution_identity": self.identity(),
            "worker_boot": self.boot,
        }
        if self.state == "exited":
            value.update(descendants_terminated=True, result=self.receipt)
        return value

    def stop(self, operation):
        self.complete(operation, exit_code=-9)
        self.receipt["canceled"] = True
        return self.status(operation)

    def logs(self, _operation, offset=0, limit=65536):
        return self.content[offset : offset + limit]

    def export(self, _operation, source):
        return result_manifest(source, self.exported), self.exported

    def complete(self, operation, *, exit_code=0):
        self.state = "exited"
        self.receipt = {
            "run_id": self.prepared[operation]["run_id"],
            "operation_id": operation,
            "execution_identity": self.identity(),
            "exit_code": exit_code,
            "terminated": True,
            "timed_out": False,
            "canceled": False,
            "log_truncated": False,
            "started_at": 1000.0,
            "ended_at": 1001.0,
            "log_digest": hashlib.sha256(self.content).hexdigest(),
            "artifact": result_manifest(self.source, self.exported),
        }


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class RoleCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = RecordedExecutor().capabilities()

    def test_all_native_roles_use_the_same_permission_contract(self):
        selected = require_factory_capabilities(self.capabilities, FACTORY_ROLES)
        self.assertEqual(
            {
                role
                for role, value in selected.items()
                if value["source_access"] == "read_write"
            },
            {"build"},
        )

    def test_advertised_read_only_flag_cannot_replace_boundary_proof(self):
        del self.capabilities["factory_roles"]["reviewer-code"]["qualification"]
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_missing_runtime_cannot_qualify_a_role(self):
        del self.capabilities["runtime"]
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_unknown_runtime_cannot_qualify_a_role(self):
        self.capabilities["runtime"] = "advertised-worker"
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_integrated_runtime_cannot_qualify_active_factory_roles(self):
        self.capabilities["runtime"] = RUNTIME
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_malformed_runtime_cannot_qualify_a_role(self):
        self.capabilities["runtime"] = ["compact-worker-v1"]
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_unknown_capability_schema_cannot_qualify_a_role(self):
        self.capabilities["schema_version"] = 2
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_unknown_worker_protocol_cannot_qualify_a_role(self):
        self.capabilities["protocol_version"] = 3
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_boolean_qualification_version_cannot_qualify_a_role(self):
        self.capabilities["qualification_version"] = True
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["reviewer-code"])

    def test_fixture_evidence_cannot_qualify_a_worker(self):
        self.capabilities["factory_roles"]["build"]["qualification"]["kind"] = "fixture"
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["build"])

    def test_role_proof_for_another_boot_is_rejected(self):
        self.capabilities["worker_boot"] = str(uuid4())
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["build"])

    def test_changed_profile_recipe_cannot_reuse_a_permission_proof(self):
        self.capabilities["profiles"][0]["limits"]["memory_mb"] = 2048
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["intake"])

    def test_failed_credential_scope_probe_blocks_a_role(self):
        proof = self.capabilities["factory_roles"]["build"]["qualification"]
        proof["checks"]["credentials_scoped"] = False
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(self.capabilities, ["build"])

    def test_missing_browser_proof_cannot_silently_skip_visual_review(self):
        with self.assertRaises(NativeSessionError):
            require_factory_capabilities(
                self.capabilities, ["reviewer-ui"], browser=True
            )

    def test_frozen_role_binding_requires_the_observed_worker(self):
        entry = self.capabilities["factory_roles"]["decision"]
        identity = {
            "instance_id": str(uuid4()),
            "worker_boot": self.capabilities["worker_boot"],
            "binding_digest": self.capabilities["binding_digest"],
        }
        with self.assertRaises(NativeSessionError):
            validate_role_binding(
                entry,
                "decision",
                runtime_binding_digest=self.capabilities["binding_digest"],
                execution_identity=identity,
            )

    def test_compact_clients_use_existing_native_adapters_for_every_provider(self):
        executor = RecordedExecutor()
        executor.integrated, executor.compact = False, True
        for provider in VERSIONS:
            with self.subTest(provider=provider):
                adapter = NativeBackendRegistry().create(provider, executor)
                self.assertIsInstance(adapter, NativeCLIAdapter)
                self.assertEqual(adapter.provider, provider)

    def test_truthy_marker_is_not_a_registered_compact_client(self):
        executor = RecordedExecutor()
        executor.integrated, executor.compact = False, "untrusted"
        with self.assertRaises(NativeSessionError):
            NativeBackendRegistry().create("claude", executor)


class NativeRoleResultTests(unittest.TestCase):
    def test_every_provider_preserves_a_complete_result_beyond_display_preview(self):
        payload = {"summary": "x" * 8000}
        for provider in ("codex", "claude", "opencode"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    parse_role_result(provider, transcript(provider, payload))[
                        "payload"
                    ],
                    payload,
                )

    def test_duplicate_terminal_events_are_rejected_for_all_providers(self):
        for provider in ("codex", "claude", "opencode"):
            content = transcript(provider)
            with self.subTest(provider=provider), self.assertRaises(NativeSessionError):
                parse_role_result(provider, content + content.splitlines()[-1] + b"\n")

    def test_changed_session_is_rejected_for_all_providers(self):
        for provider in ("codex", "claude", "opencode"):
            events = [json.loads(line) for line in transcript(provider).splitlines()]
            field = {
                "codex": "thread_id",
                "claude": "session_id",
                "opencode": "sessionID",
            }[provider]
            if provider == "codex":
                events.insert(1, {"type": "thread.started", field: str(uuid4())})
            else:
                events[-1][field] = (
                    "ses_" + uuid4().hex if provider == "opencode" else str(uuid4())
                )
            content = b"\n".join(json.dumps(event).encode() for event in events)
            with self.subTest(provider=provider), self.assertRaises(NativeSessionError):
                parse_role_result(provider, content)

    def test_truncated_terminal_json_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            parse_role_result("claude", transcript("claude")[:-10])

    def test_success_without_a_final_event_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            parse_role_result(
                "opencode", b"\n".join(transcript("opencode").splitlines()[:-1])
            )

    def test_nonfinite_role_json_is_rejected(self):
        content = transcript("claude")
        events = [json.loads(line) for line in content.splitlines()]
        events[-1]["result"] = '{"value": NaN}'
        with self.assertRaises(NativeSessionError):
            parse_role_result(
                "claude", b"\n".join(json.dumps(event).encode() for event in events)
            )

    def test_overflowing_role_json_float_is_rejected(self):
        events = [json.loads(line) for line in transcript("claude").splitlines()]
        events[-1]["result"] = '{"value": 1e9999}'
        with self.assertRaises(NativeSessionError):
            parse_role_result(
                "claude", b"\n".join(json.dumps(event).encode() for event in events)
            )

    def test_provider_metadata_is_not_reported_as_token_usage(self):
        events = [json.loads(line) for line in transcript("claude").splitlines()]
        events[-1]["usage"] = {
            "input_tokens": 10,
            "server_tool_use": {"web_search_requests": 2},
            "service_tier": "standard",
        }
        result = parse_role_result(
            "claude", b"\n".join(json.dumps(event).encode() for event in events)
        )
        self.assertEqual(result["usage"], {"input_tokens": 10})

    def test_duplicate_role_json_keys_are_rejected(self):
        events = [json.loads(line) for line in transcript("claude").splitlines()]
        events[-1]["result"] = '{"verdict":"pass","verdict":"changes"}'
        with self.assertRaises(NativeSessionError):
            parse_role_result(
                "claude", b"\n".join(json.dumps(event).encode() for event in events)
            )

    def test_boolean_usage_is_not_a_token_count(self):
        events = [json.loads(line) for line in transcript("claude").splitlines()]
        events[-1]["usage"] = {"input_tokens": True}
        with self.assertRaises(NativeSessionError):
            parse_role_result(
                "claude", b"\n".join(json.dumps(event).encode() for event in events)
            )

    def test_role_result_size_is_bounded(self):
        with self.assertRaises(NativeSessionError):
            parse_role_result("codex", transcript("codex", {"text": "x" * MAX_RESULT}))


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class FactoryNativeAdapterTests(unittest.TestCase):
    def prepare(self, provider="codex", role="reviewer-code", browser_capture=None):
        self.executor = RecordedExecutor()
        delegate = NativeCLIAdapter(self.executor, provider)
        self.adapter = FactoryNativeAdapter(
            delegate, role, self.executor.capabilities()
        )
        reference = {
            "provider": provider,
            "kind": "connection" if provider == "opencode" else "login",
            "id": 1,
            "version": 2,
        }
        material = {
            "secret": json.dumps(
                {"provider": "ollama", "base_url": "https://ollama.example.com"}
            )
        }
        request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "source": self.executor.source,
            "prompt": "Return only a JSON review.",
            "model": "test-model",
            "policy": default_execution_policy(),
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "credential": {"credential": reference, "material": material},
            "browser_capture": browser_capture,
        }
        return self.adapter.prepare(**request)

    def complete(self, session, *, exit_code=0):
        self.executor.content = transcript(session.adapter)
        self.executor.complete(session.manifest["operation_id"], exit_code=exit_code)

    def test_every_existing_native_provider_passes_the_same_role_outcome_checks(self):
        for provider in VERSIONS:
            for role in FACTORY_ROLES:
                with self.subTest(provider=provider, role=role):
                    session = self.prepare(provider, role=role)
                    self.complete(session)
                    outcome = self.adapter.outcome(session, self.executor.source)
                    self.assertEqual(outcome["role_result"]["verdict"], "pass")

    def test_repeated_start_reuses_the_original_operation(self):
        session = self.prepare()
        self.adapter.start(session)
        self.adapter.start(session)
        self.assertEqual(self.executor.starts, 1)

    def test_unknown_dispatch_never_starts_a_replacement(self):
        session = self.prepare()
        self.executor.state = "unknown"
        with self.assertRaises(NativeSessionError):
            self.adapter.start(session)
        self.assertEqual(self.executor.starts, 0)

    def test_malformed_observation_never_starts_a_replacement(self):
        session = self.prepare()
        with patch.object(self.adapter.adapter, "inspect", return_value=[]):
            with self.assertRaises(NativeSessionError):
                self.adapter.start(session)
        self.assertEqual(self.executor.starts, 0)

    def test_changed_credential_version_cannot_rebind_a_prepared_operation(self):
        session = self.prepare()
        changed = session.to_data()
        changed["account"]["credential_version"] = 3
        with self.assertRaises(NativeSessionError):
            self.adapter.start(NativeSession.from_data(changed))

    def test_nonzero_exit_overrides_a_successful_final_message(self):
        session = self.prepare()
        self.complete(session, exit_code=1)
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.executor.source)

    def test_read_only_role_cannot_export_a_changed_source(self):
        session = self.prepare()
        self.executor.exported = pack({"answer.py": (0o644, b"answer = 2\n")})
        self.complete(session)
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.executor.source)

    def test_builder_can_export_a_changed_candidate(self):
        session = self.prepare(role="build")
        self.executor.exported = pack({"answer.py": (0o644, b"answer = 2\n")})
        self.complete(session)
        self.assertTrue(
            self.adapter.outcome(session, self.executor.source)["successful"]
        )

    def test_changed_worker_boot_cannot_supply_an_outcome(self):
        session = self.prepare()
        self.complete(session)
        self.executor.boot = str(uuid4())
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.executor.source)

    def test_cancel_retains_actual_descendant_and_exit_results(self):
        session = self.prepare()
        self.adapter.start(session)
        observed = self.adapter.cancel(session)
        self.assertTrue(observed["descendants_terminated"])
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.executor.source)

    def test_role_profile_is_bound_in_the_actual_uploaded_context(self):
        self.prepare(role="decision")
        self.assertEqual(
            json.loads(self.executor.uploads["context"])["factory_role"], "decision"
        )

    def test_selected_credential_is_bound_in_the_actual_uploaded_context(self):
        session = self.prepare(role="decision")
        context = json.loads(self.executor.uploads["context"])
        self.assertEqual(context["credential_id"], session.account["credential_id"])
        self.assertNotIn("files", context)
        self.assertNotIn("login_digest", context)

    def test_actual_native_adapter_accepts_a_qualified_compact_profile(self):
        executor = RecordedExecutor()
        executor.runtime = "compact-worker-v1"
        adapter = NativeCLIAdapter(executor, "codex")
        policy = default_execution_policy()
        policy["runtime_profile"] = "factory-build"
        profile = adapter.selected_profile(
            ExecutionPolicy.from_data(policy),
            "login",
        )
        self.assertEqual(profile.profile_id, "factory-build")

    def test_actual_native_adapter_cannot_run_a_browser_as_a_model_role(self):
        adapter = NativeCLIAdapter(None, "codex")
        account = {"model": "test-model", "credential_id": 1}
        with self.assertRaises(NativeSessionError):
            adapter.context(account, "Inspect source", "login", factory_role="browser")

    def test_ordinary_integrated_native_selection_stays_available(self):
        executor = RecordedExecutor()
        executor.runtime = RUNTIME
        adapter = NativeCLIAdapter(executor, "codex")
        policy = default_execution_policy()
        policy["runtime_profile"] = "factory-build"
        profile = adapter.selected_profile(ExecutionPolicy.from_data(policy), "login")
        self.assertEqual(profile.profile_id, "factory-build")

    def test_actual_native_adapter_rejects_truthy_worker_qualification(self):
        executor = RecordedExecutor()
        capabilities = executor.capabilities()
        capabilities["qualified"] = 1
        adapter = NativeCLIAdapter(executor, "codex")
        policy = default_execution_policy()
        policy["runtime_profile"] = "factory-build"
        with patch.object(executor, "capabilities", return_value=capabilities):
            with self.assertRaises(NativeSessionError):
                adapter.selected_profile(ExecutionPolicy.from_data(policy), "login")

    def test_actual_native_adapter_rejects_a_forged_profile_digest(self):
        executor = RecordedExecutor()
        capabilities = executor.capabilities()
        for profile in capabilities["profiles"]:
            if profile["profile_id"] == "factory-build":
                profile["profile_digest"] = "f" * 64
        adapter = NativeCLIAdapter(executor, "codex")
        policy = default_execution_policy()
        policy["runtime_profile"] = "factory-build"
        with patch.object(executor, "capabilities", return_value=capabilities):
            with self.assertRaises(NativeSessionError):
                adapter.selected_profile(ExecutionPolicy.from_data(policy), "login")

    def test_visual_review_binds_a_capture_in_the_actual_uploaded_context(self):
        capture = {"operation_id": str(uuid4()), "archive_digest": "d" * 64}
        self.prepare(role="reviewer-ui", browser_capture=capture)
        self.assertEqual(
            json.loads(self.executor.uploads["context"])["browser_capture"], capture
        )


class PermissionAndExecutableTests(unittest.TestCase):
    def test_task_owned_read_only_mode_cannot_qualify_an_immutable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            source.write_text("answer = 1\n")
            source.chmod(0o444)
            observed = probe_source_access(
                directory, source, [source], role="reviewer-code"
            )
            self.assertFalse(observed["passed"])

    def test_missing_private_sentinel_cannot_be_reported_as_denied_access(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            source.write_text("answer = 1\n")
            observed = probe_source_access(
                directory, source, [Path(directory) / "missing"], role="build"
            )
            self.assertFalse(observed["checks"]["control_paths_denied"])

    def test_browser_capture_reference_is_only_available_to_visual_reviewers(self):
        capture = {"operation_id": str(uuid4()), "archive_digest": "d" * 64}
        with self.assertRaises(NativeSessionError):
            validate_browser_capture("build", capture)

    def test_malformed_browser_role_is_rejected(self):
        capture = {"operation_id": str(uuid4()), "archive_digest": "d" * 64}
        with self.assertRaises(NativeSessionError):
            validate_browser_capture(["reviewer-ui"], capture)

    @unittest.skipUnless(
        SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
    )
    def test_version_probe_does_not_claim_provider_or_worker_qualification(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binary = Path(directory) / "codex"
            binary.write_text("#!/bin/sh\nprintf 'codex-cli 0.169.0\\n'\n")
            binary.chmod(0o755)
            result = executable_probe("codex", directory=Path(directory))
            self.assertFalse(result["live_provider_verified"])
            self.assertFalse(result["worker_boundary_verified"])
            self.assertFalse(result["authenticated"])

    @unittest.skipUnless(
        SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
    )
    def test_version_probe_rejects_a_different_installed_cli(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binary = Path(directory) / "opencode"
            binary.write_text("#!/bin/sh\nprintf '99.0.0\\n'\n")
            binary.chmod(0o755)
            with self.assertRaisesRegex(
                NativeSessionError, "differs from its pinned version"
            ):
                executable_probe("opencode", directory=Path(directory))

    @unittest.skipUnless(
        SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
    )
    def test_version_probe_uses_its_clean_home_as_working_directory(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binary = Path(directory) / "codex"
            binary.write_text(
                '#!/bin/sh\n[ "$(pwd)" = "$HOME" ] || exit 1\n'
                "printf 'codex-cli 0.169.0\\n'\n"
            )
            binary.chmod(0o755)
            result = executable_probe("codex", directory=Path(directory))
            self.assertEqual(result["agent_version"], "0.169.0")

    @unittest.skipUnless(
        SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
    )
    def test_version_probe_bounds_output_before_reading_all_of_it(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binary = Path(directory) / "codex"
            binary.write_text("#!/bin/sh\nhead -c 70000 /dev/zero\n")
            binary.chmod(0o755)
            with self.assertRaisesRegex(NativeSessionError, "output exceeds its bound"):
                executable_probe("codex", directory=Path(directory))

    @unittest.skipUnless(
        SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
    )
    def test_version_probe_terminates_a_hanging_process_group(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            binary = Path(directory) / "codex"
            binary.write_text("#!/bin/sh\nsleep 20\n")
            binary.chmod(0o755)
            with self.assertRaisesRegex(NativeSessionError, "timed out"):
                executable_probe("codex", directory=Path(directory), timeout=1)
