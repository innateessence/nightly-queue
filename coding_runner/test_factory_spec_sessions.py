"""Native lifecycle protocol fixtures; these do not qualify a live worker/provider."""

import importlib.util
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from uuid import uuid4

from vibearr_runner.factory_roles import BASE_ROLES, freeze_context, retain_criteria
from vibearr_runner.client import APIError
from vibearr_runner.native_contract import (
    NativeContractError,
    digest,
    validate_factory_paths,
    workspace_record,
)
from vibearr_runner.native_lifecycle import NativeLifecycle
from vibearr_runner.native_sessions import NativeSessionError
from vibearr_runner.recovery import ReconciliationRequired

from . import test_native_lifecycle as native_fixture

SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.artifacts import pack, result_manifest, unpack
    from vibearr_executor.client import ExecutorUnavailable
    from vibearr_executor.contracts import Profile
    from vibearr_executor.integrated_profiles import ARGV


RUNTIME = "compact-worker-v1"


ROLES = (*BASE_ROLES, "reviewer-security", "reviewer-ux", "reviewer-ui")
FROZEN_CRITERION = (
    "Complete the frozen task instructions and pass every configured validation check."
)
CRITERIA = [
    {"id": "frozen-task", "description": FROZEN_CRITERION},
    {"id": "answer-test", "description": "Improve answer.py with a regression test."},
]
INTAKE = {
    "verdict": "accept",
    "scope": "Improve the requested answer behavior.",
    "criteria": CRITERIA,
    "rationale": "The requested change is bounded.",
    "risk": "low",
    "surfaces": [],
    "questions": [],
}
SPECIFICATION = {
    "scope": INTAKE["scope"],
    "criteria": CRITERIA,
    "exclusions": [],
    "verification": ["Run the answer regression test."],
}


class FactoryQueue(native_fixture.Queue):
    def __init__(self, source):
        super().__init__(source)
        self.factory_artifacts = []
        self.plans = []

    def request(self, method, path, payload=None):
        if "/factory/artifacts/" in path:
            artifact = {
                "id": payload["operation_id"],
                "document": deepcopy(payload["document"]),
                "digest": digest(payload["document"]),
                "sequence": len(self.factory_artifacts) + 1,
            }
            self.factory_artifacts.append(artifact)
            return {"artifact": artifact}
        if path.endswith("/secrets/") and payload.get("purpose") == "implementation":
            return {
                "credential": {
                    "id": 1,
                    "provider": "codex",
                    "kind": "login",
                    "version": 2,
                },
                "material": {"secret": "selected-login-reference"},
            }
        if path.endswith("/plans/"):
            self.plans.append(deepcopy(payload))
        return super().request(method, path, payload)


class RoleExecutor(native_fixture.Executor):
    integrated = True

    def __init__(self, profile):
        super().__init__(profile)
        self.boot = str(uuid4())
        self.role_profiles = {
            role: Profile(
                "factory-" + role,
                1,
                self.agent.image,
                "agent",
                ARGV,
                self.agent.limits,
                self.agent.network_hosts,
                "VIBEARR_NATIVE_CONNECTION",
                self.agent.image,
            )
            for role in ROLES
        }
        self.profiles.extend(self.role_profiles.values())
        self.results = {
            "intake": deepcopy(INTAKE),
            "specification": deepcopy(SPECIFICATION),
        }
        self.payloads = {}
        self.lose_prepare = False
        self.lose_start = False
        self.mutate_source = False

    def identity(self):
        return {
            "runtime": RUNTIME,
            "protocol_version": 2,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": "e" * 64,
        }

    def capabilities(self):
        value = super().capabilities()
        value.update(runtime=RUNTIME, worker_boot=self.boot)
        value["factory_roles"] = {}
        for role, profile in self.role_profiles.items():
            access = "read_write" if role == "build" else "read_only"
            checks = {
                "control_paths_denied": True,
                "credentials_scoped": True,
                "descendants_terminated": True,
            }
            checks[
                "candidate_write_only" if role == "build" else "source_write_denied"
            ] = True
            value["factory_roles"][role] = {
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_access": access,
                "qualification": {
                    "schema_version": 1,
                    "kind": "worker-boundary",
                    "role": role,
                    "source_access": access,
                    "profile_digest": profile.digest,
                    "image_digest": profile.image,
                    "runtime_binding_digest": "e" * 64,
                    "instance_id": self.instance,
                    "worker_boot": self.boot,
                    "passed": True,
                    "proof_digest": "f" * 64,
                    "checks": checks,
                },
            }
        return value

    def request(self, _path):
        return {"available": True, "login_digest": "d" * 64}

    def prepare(self, manifest):
        result = super().prepare(manifest)
        if self.lose_prepare:
            self.lose_prepare = False
            raise ExecutorUnavailable("Fixture lost the prepare response.")
        return result

    def status(self, identity):
        value = super().status(identity)
        value.pop("container_id", None)
        value.update(execution_identity=self.identity(), worker_boot=self.boot)
        if "result" in value:
            value["result"]["execution_identity"] = self.identity()
        return value

    def start(self, identity):
        if self.states[identity]["state"] != "not-started":
            return self.status(identity)
        manifest = self.manifests[identity]
        role = manifest["profile_id"].removeprefix("factory-")
        self.no_change = True
        selected = self.agent
        self.agent = self.role_profiles[role]
        try:
            super().start(identity)
        finally:
            self.agent = selected
        source = self.artifacts[manifest["source_digest"]]
        if self.mutate_source:
            files = unpack(source)
            files["unexpected.py"] = (0o644, b"unexpected = True\n")
            self.outputs[identity] = pack(files)
            self.states[identity]["result"]["artifact"] = result_manifest(
                source, self.outputs[identity]
            )
        events = [
            {"type": "thread.started", "thread_id": str(uuid4())},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(self.results[role]),
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ]
        self.logfiles[identity] = (
            b"\n".join(json.dumps(entry).encode() for entry in events) + b"\n"
        )
        self.states[identity]["result"]["log_digest"] = hashlib.sha256(
            self.logfiles[identity]
        ).hexdigest()
        if self.lose_start:
            self.lose_start = False
            raise ExecutorUnavailable("Fixture lost the start response.")
        return self.status(identity)


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class FactorySpecSessionTests(unittest.TestCase):
    def setUp(self):
        native_fixture.NativeLifecycleTests.setUp(self)
        self.executor = RoleExecutor(self.profile)
        self.queue = FactoryQueue(self.queue.source)
        self.host.client = self.queue
        configuration = self.state["run"]["run"]["configuration_snapshot"]
        configuration["global"]["research_timeout_seconds"] = 1200
        configuration["effective"]["factory"] = {
            "schema_version": 1,
            "enabled": True,
            "roles": {
                role: {"model": "gpt-6.1-sol", "instructions": ""} for role in ROLES
            },
        }

    def engine(self):
        return NativeLifecycle(
            self.host, executor=self.executor, validation_profile=self.profile
        )

    def step(self):
        return self.engine().step(self.state)

    def advance(self, predicate):
        for _ in range(12):
            self.step()
            if predicate():
                return
        self.fail("Factory fixture did not reach its expected stage.")

    def test_native_context_delegates_before_creating_a_generic_plan(self):
        self.step()
        self.assertEqual(self.state["native"]["phase"], "factory")
        self.assertNotIn("plan", self.state["native"])
        self.assertIn("factory", self.state["native"]["context"]["document"])
        self.assertEqual(self.executor.started, [])

    def test_manual_task_dispatches_distinct_roles_and_saves_actual_criteria(self):
        self.advance(
            lambda: self.state["native"].get("factory", {}).get("stage") == "build"
        )
        self.assertEqual(len(self.executor.started), 2)
        self.assertEqual(
            [value["document"]["role"] for value in self.queue.factory_artifacts],
            ["intake", "specification"],
        )
        self.assertEqual(
            self.state["native"]["plan"]["document"]["acceptance_criteria"],
            [entry["description"] for entry in CRITERIA],
        )
        self.assertEqual(
            self.state["native"]["plan"]["document"]["factory_specification_id"],
            self.queue.factory_artifacts[-1]["id"],
        )

    def test_needs_information_finishes_distinctly_without_a_spec_or_retry(self):
        self.executor.results["intake"].update(
            verdict="needs_info", questions=["Which answer is required?"]
        )
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.queue.finishes[-1]["outcome"], "needs_info")
        self.assertEqual(len(self.executor.started), 1)
        self.assertEqual(self.queue.plans, [])

    def test_rejected_intake_finishes_without_build_or_publication(self):
        self.executor.results["intake"]["verdict"] = "reject"
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.queue.finishes[-1]["outcome"], "rejected")
        self.assertEqual(len(self.executor.started), 1)
        self.assertFalse(
            any(
                entry.get("kind") == "native_delivery" for entry in self.queue.artifacts
            )
        )

    def test_specification_cannot_remove_an_accepted_criterion(self):
        self.executor.results["specification"]["criteria"] = [
            "A different smaller task."
        ]
        self.advance(lambda: bool(self.queue.finishes))
        self.assertIn("accepted criteria", self.queue.finishes[-1]["error_summary"])
        self.assertEqual(self.queue.plans, [])

    def test_missing_read_only_proof_prevents_any_native_dispatch(self):
        original = self.executor.capabilities

        def capabilities():
            value = original()
            value["factory_roles"]["intake"]["qualification"]["kind"] = "fixture"
            return value

        self.executor.capabilities = capabilities
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.executor.started, [])

    def test_reload_after_lost_prepare_uses_the_original_operation(self):
        self.step()
        self.executor.lose_prepare = True
        with self.assertRaises(ReconciliationRequired):
            self.step()
        identity = self.state["native"]["factory"]["roles"]["intake"]["request"][
            "operation_id"
        ]
        self.advance(
            lambda: self.state["native"]["factory"]["stage"] == "specification"
        )
        self.assertEqual(self.executor.started, [identity])

    def test_reload_after_lost_start_does_not_start_a_second_session(self):
        self.step()
        self.executor.lose_start = True
        with self.assertRaises(ReconciliationRequired):
            self.step()
        identity = self.state["native"]["factory"]["roles"]["intake"]["request"][
            "operation_id"
        ]
        self.advance(
            lambda: self.state["native"]["factory"]["stage"] == "specification"
        )
        self.assertEqual(self.executor.started, [identity])

    def test_read_only_source_change_never_produces_a_factory_pass(self):
        self.executor.mutate_source = True
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.queue.factory_artifacts, [])
        self.assertEqual(self.queue.plans, [])

    def test_frozen_role_registration_cannot_move_to_another_boot(self):
        self.step()
        self.executor.boot = str(uuid4())
        with self.assertRaises(ReconciliationRequired):
            self.step()
        self.assertEqual(self.executor.started, [])

    def test_saved_prompt_is_reused_after_a_lost_prepare_response(self):
        self.step()
        self.executor.lose_prepare = True
        with self.assertRaises(ReconciliationRequired):
            self.step()
        frozen = self.state["native"]["factory"]["roles"]["intake"]["request"]["prompt"]
        self.state["run"]["run"]["configuration_snapshot"]["effective"]["factory"][
            "roles"
        ]["intake"]["instructions"] = "Changed after the crash."
        self.advance(
            lambda: self.state["native"]["factory"]["stage"] == "specification"
        )
        self.assertEqual(
            self.state["native"]["factory"]["roles"]["intake"]["request"]["prompt"],
            frozen,
        )

    def test_malformed_role_result_finishes_with_confirmed_cleanup(self):
        self.executor.results["intake"] = {"verdict": "accept"}
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.queue.finishes[-1]["outcome"], "failed")
        self.assertEqual(self.queue.factory_artifacts, [])
        self.assertEqual(self.queue.plans, [])
        self.assertTrue(
            any(
                entry.get("kind") == "native_termination"
                for entry in self.queue.artifacts
            )
        )

    def test_accepted_intake_cannot_drop_a_frozen_requested_criterion(self):
        self.executor.results["intake"]["criteria"] = [CRITERIA[-1]]
        self.advance(lambda: bool(self.queue.finishes))
        self.assertIn(
            "frozen requested criteria", self.queue.finishes[-1]["error_summary"]
        )
        self.assertEqual(len(self.executor.started), 1)
        self.assertEqual(self.queue.plans, [])

    def test_bootstrap_dispatches_freeze_explicit_empty_specification_pair(self):
        self.advance(
            lambda: self.state["native"].get("factory", {}).get("stage") == "build"
        )
        intents = [
            entry["data"]
            for entry in self.queue.artifacts
            if entry.get("kind") == "native_intent"
        ]
        self.assertEqual(
            [value["data"]["factory"]["role"] for value in intents],
            ["intake", "specification"],
        )
        for intent in intents:
            descriptor = intent["data"]["factory"]
            self.assertIsNone(descriptor["specification_id"])
            self.assertEqual(descriptor["specification_digest"], "")
            self.assertEqual(
                descriptor["input_digest"],
                intent["data"]["session"]["manifest"]["context_digest"],
            )

    def test_lost_result_response_reconciles_saved_operation_without_another_start(
        self,
    ):
        original = self.queue.request
        saved = {}

        def request(method, path, payload=None):
            if method == "GET" and "/operations/" in path:
                return {"response": saved["response"]}
            response = original(method, path, payload)
            if payload and payload.get("kind") == "native_agent" and not saved:
                saved["response"] = response
                raise APIError("Fixture lost the saved result response.")
            return response

        self.queue.request = request
        self.advance(
            lambda: self.state["native"].get("factory", {}).get("stage") == "build"
        )
        self.assertEqual(len(self.executor.started), 2)
        self.assertEqual(
            len(
                [
                    entry
                    for entry in self.queue.artifacts
                    if entry.get("kind") == "native_agent"
                ]
            ),
            2,
        )

    def test_cancel_after_intake_dispatch_finishes_without_a_specification(self):
        self.step()
        self.step()
        self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.queue.finishes[-1]["outcome"], "canceled")
        self.assertEqual(len(self.executor.started), 1)
        self.assertEqual(self.queue.plans, [])

    def test_cancel_with_unknown_intake_retains_original_operation_and_claim(self):
        self.step()
        self.step()
        identity = self.executor.started[0]
        self.executor.unknown.add(identity)
        self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        with self.assertRaises(ReconciliationRequired):
            self.step()
        self.assertEqual(self.executor.started, [identity])
        self.assertEqual(self.queue.finishes, [])

    def test_prepared_merge_mode_role_cannot_start_after_restart(self):
        self.state["run"]["run"]["configuration_snapshot"]["execution_policy"][
            "delivery_mode"
        ] = "merge"
        self.step()
        engine = self.engine()
        engine.require_human_dispatch = (
            lambda: None
        )  # Emulate preparation by the prior release.
        intent = engine.intent

        def saved_intent(payload, key):
            intent(payload, key)
            raise ExecutorUnavailable("Fixture crashed after intent acknowledgement.")

        engine.intent = saved_intent
        with self.assertRaises(ReconciliationRequired):
            engine.step(self.state)
        self.advance(lambda: bool(self.queue.finishes))
        self.assertEqual(self.executor.started, [])
        self.assertIn("human must merge", self.queue.finishes[-1]["error_summary"])


class FactoryRoleContractTests(unittest.TestCase):
    def test_specification_can_add_but_not_rewrite_accepted_criteria(self):
        with self.assertRaises(NativeSessionError):
            retain_criteria(INTAKE, {**SPECIFICATION, "criteria": ["other"]})

    def test_factory_registration_requires_every_core_role(self):
        with self.assertRaises(NativeSessionError):
            freeze_context({"enabled": True}, {"factory_roles": {}})

    def test_factory_paths_reject_noncanonical_or_unbounded_inventory(self):
        for paths in (
            [],
            ["../secret"],
            ["/secret"],
            ["a//b"],
            ["a/./b"],
            ["a\\b"],
            ["b", "a"],
            ["a", "a"],
            ["x" * 4097],
            ["a"] * 10001,
        ):
            with (
                self.subTest(paths=str(paths)[:80]),
                self.assertRaises(NativeContractError),
            ):
                validate_factory_paths(paths)

    def test_workspace_preserves_historical_schema_and_seals_optional_paths(self):
        values = {
            "context_id": str(uuid4()),
            "context_digest": "a" * 64,
            "attempt": 0,
            "agent_intent_id": str(uuid4()),
            "source_digest": "b" * 64,
            "export_digest": "c" * 64,
            "requirements_digest": "d" * 64,
            "workspace": {
                "repository_id": 1,
                "repository": "owner/repo",
                "branch": "task/change",
                "base_branch": "main",
                "base_revision": "a" * 40,
                "tree_sha": "b" * 40,
                "commit_sha": "c" * 40,
            },
        }
        self.assertNotIn("factory_paths", workspace_record(**values))
        self.assertEqual(
            workspace_record(**values, factory_paths=[" a.py", "removed.py"])[
                "factory_paths"
            ],
            [" a.py", "removed.py"],
        )
