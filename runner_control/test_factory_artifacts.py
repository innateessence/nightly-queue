import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from credentials.test_models import TEST_KEY
from vibearr_runner import factory_contract as contract
from vibearr_runner import native_contract as native
from work_queue.models import QueueItem
from work_queue.services import requeue_items

from .factory_models import FactoryArtifact
from .factory_services import save_artifact
from .models import Run, RunnerIdentity
from .services import RunConflict, RunNotFound
from .test_native_protocol import NativeProtocolMixin
from .test_support import CONTROLLER_SECRET


class FactoryFixture(NativeProtocolMixin):
    def setUp(self):
        super().setUp()
        self.worker_identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "e" * 64,
        }
        self.complete_agent()
        self.agent_intent.payload["data"]["factory"] = {
            "role": "build",
            "round": 1,
            "prompt_digest": "f" * 64,
            "input_digest": self.session.manifest["context_digest"],
        }
        self.agent_intent.save(update_fields=["payload"])
        self.run.refresh_from_db()

    def document(self, *, role="build", round=1):
        plan = self.run.plans.last()
        result = self.run.evidence.get(kind="native_agent")
        receipt = result.payload["receipt"]
        value = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "role": role,
            "round": round,
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": str(plan.pk),
            "specification_digest": plan.digest,
            "source_digest": self.workspace_entry.payload["source_digest"],
            "tree_sha": self.workspace["tree_sha"],
            "base_sha": self.workspace["base_revision"],
            "head_sha": self.workspace["commit_sha"],
            "configuration_digest": native.digest(self.run.configuration_snapshot),
            "payload": {
                "summary": "Implement the requested change",
                "criteria": {"criterion-1": "Updated file.py"},
            },
            "execution": {
                "intent_id": str(self.agent_intent.operation_id),
                "result_id": str(result.operation_id),
                "adapter": self.session.adapter,
                "model": self.session.account["model"],
                "agent_version": result.payload["agent_version"],
                "session_id": result.payload["session_id"],
                "operation_id": self.session.manifest["operation_id"],
                "execution_identity": receipt["execution_identity"],
                "input_source_digest": self.session.manifest["source_digest"],
                "input_digest": self.session.manifest["context_digest"],
                "prompt_digest": "f" * 64,
                "started_at": receipt["started_at"],
                "ended_at": receipt["ended_at"],
            },
        }
        if role == "routing":
            value.pop("execution")
            value["payload"] = {
                "required_reviewers": ["reviewer-code"],
                "risk": "low",
                "surfaces": [],
                "rule_digest": "a" * 64,
            }
        return value

    def save(
        self, document=None, *, reason="Initial role evidence", runner=None, claim=None
    ):
        document = document or self.document()
        return save_artifact(
            runner=runner or self.runner,
            run_id=self.run.pk,
            claim_token=claim or self.claim,
            operation_id=document["operation_id"],
            document=document,
            reason=reason,
        )

    def post_document(self, document=None, *, claim=None):
        document = document or self.document()
        return self.client.post(
            reverse("runner_factory_artifacts", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(claim or self.claim),
                    "operation_id": document["operation_id"],
                    "document": document,
                    "reason": "Role completed",
                }
            ),
            content_type="application/json",
        )


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class FactoryArtifactTests(FactoryFixture, TestCase):
    def test_save_binds_actual_native_result_and_preserves_immutable_document(self):
        document = self.document()
        entry = self.save(document)
        self.assertEqual(entry.document, document)
        self.assertEqual(entry.digest, native.digest(document))
        self.assertEqual(entry.sequence, 1)
        self.assertEqual(entry.specification_id, self.run.plans.last().pk)

    def test_exact_operation_retry_returns_original_artifact(self):
        document = self.document()
        self.assertEqual(self.save(document).pk, self.save(document).pk)
        self.assertEqual(FactoryArtifact.objects.count(), 1)

    def test_changed_operation_payload_conflicts(self):
        document = self.document()
        self.save(document)
        document["payload"]["summary"] = "Different outcome"
        with self.assertRaises(RunConflict):
            self.save(document)

    def test_different_operation_cannot_overwrite_same_role_round(self):
        self.save()
        with self.assertRaises(RunConflict):
            self.save()

    def test_factory_model_cannot_be_updated_or_deleted(self):
        entry = self.save()
        for operation in (
            lambda: entry.save(),
            lambda: entry.delete(),
            lambda: FactoryArtifact.objects.update(role="routing"),
            lambda: FactoryArtifact.objects.all().delete(),
        ):
            with self.assertRaises(ValidationError):
                operation()

    def test_foreign_runner_cannot_save_owned_artifact(self):
        stranger = RunnerIdentity.objects.create(name="Other", token_digest="a" * 64)
        with self.assertRaises(RunNotFound):
            self.save(runner=stranger)
        self.assertFalse(FactoryArtifact.objects.exists())

    def test_requeued_claim_cannot_save_or_replay(self):
        document = self.document()
        self.save(document)
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        with self.assertRaises(RunConflict):
            self.save(document)

    def test_completed_run_retains_read_access_without_write_scope(self):
        entry = self.save()
        Run.objects.filter(pk=self.run.pk).update(state="completed", active_slot=None)
        with self.assertRaises(RunConflict):
            self.save(self.document(role="routing"))
        response = self.client.get(
            reverse("runner_factory_artifact", args=[self.run.pk, entry.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["artifact"]["id"], str(entry.pk))

    def test_wrong_context_or_specification_is_rejected(self):
        for field in ("context_id", "specification_id"):
            document = self.document()
            document[field] = str(uuid4())
            with self.subTest(field=field), self.assertRaises(RunConflict):
                self.save(document)

    def test_wrong_source_tree_or_frozen_configuration_is_rejected(self):
        for field in (
            "source_digest",
            "tree_sha",
            "base_sha",
            "head_sha",
            "configuration_digest",
        ):
            document = self.document()
            document[field] = "b" * len(document[field])
            with self.subTest(field=field), self.assertRaises(RunConflict):
                self.save(document)

    def test_missing_native_result_cannot_attest_a_build(self):
        document = self.document()
        document["execution"]["result_id"] = str(uuid4())
        with self.assertRaises(RunConflict):
            self.save(document)

    def test_untyped_legacy_agent_cannot_be_relabelled_as_a_factory_role(self):
        self.agent_intent.payload["data"].pop("factory")
        self.agent_intent.save(update_fields=["payload"])
        with self.assertRaises(RunConflict):
            self.save()

    def test_native_worker_or_session_cannot_be_changed(self):
        for field in ("model", "session_id", "agent_version", "input_source_digest"):
            document = self.document()
            document["execution"][field] = "b" * 64
            with self.subTest(field=field), self.assertRaises(RunConflict):
                self.save(document)

    def test_secret_material_is_rejected_before_storage(self):
        for secret in (
            str(self.claim),
            self.runner_token,
            "github-native-secret",
            "backend-native-secret",
        ):
            document = self.document()
            document["payload"]["summary"] = "Leak " + secret
            with self.subTest(secret=secret):
                self.assertEqual(self.post_document(document).status_code, 400)
        self.assertFalse(FactoryArtifact.objects.exists())

    def test_current_controller_header_secret_is_rejected(self):
        self.client.defaults.pop("HTTP_X_NQ_CONTROLLER_SECRET")
        self.client.defaults["HTTP_X_VIBEARR_CONTROLLER_SECRET"] = CONTROLLER_SECRET
        document = self.document()
        document["payload"]["summary"] = "Leaked " + CONTROLLER_SECRET
        self.assertEqual(self.post_document(document).status_code, 400)
        self.assertFalse(FactoryArtifact.objects.exists())

    def test_an_older_workspace_cannot_attest_current_factory_work(self):
        document = self.document(role="routing")
        newer = copy.deepcopy(self.workspace_entry.payload)
        newer["workspace"]["tree_sha"] = "6" * 40
        self.run.evidence.create(
            operation_id=uuid4(), kind="native_workspace", payload=newer
        )
        with self.assertRaises(RunConflict):
            self.save(document)

    def test_artifact_envelope_cannot_claim_another_operation(self):
        document = self.document()
        with self.assertRaises(RunConflict):
            save_artifact(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.claim,
                operation_id=uuid4(),
                document=document,
            )

    def test_http_save_detail_and_bounded_pagination(self):
        response = self.post_document()
        self.assertEqual(response.status_code, 200, response.content)
        entry = response.json()["artifact"]
        for round in range(1, 13):
            self.save(self.document(role="routing", round=round))
        url = reverse("runner_factory_artifacts", args=[self.run.pk])
        page = self.client.get(url).json()
        self.assertEqual(len(page["artifacts"]), 10)
        self.assertTrue(page["has_more"])
        self.assertNotIn("document", page["artifacts"][0])
        following = self.client.get(url, {"after": page["next_after"]}).json()
        self.assertEqual(len(following["artifacts"]), 3)
        detail = self.client.get(
            reverse("runner_factory_artifact", args=[self.run.pk, entry["id"]])
        ).json()
        self.assertEqual(detail["artifact"]["document"]["role"], "build")

    def test_invalid_cursors_and_unauthenticated_reads_are_rejected(self):
        url = reverse("runner_factory_artifacts", args=[self.run.pk])
        for cursor in ("-1", "invalid", str(2**32)):
            self.assertEqual(self.client.get(url, {"after": cursor}).status_code, 400)
        self.client.defaults.clear()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_nonfinite_or_oversized_http_artifact_is_rejected(self):
        document = self.document()
        document["payload"]["number"] = float("nan")
        self.assertEqual(self.post_document(document).status_code, 400)
        document = self.document()
        document["payload"]["summary"] = "x" * (contract.MAX_ARTIFACT_BYTES + 1)
        self.assertEqual(self.post_document(document).status_code, 400)

    def test_deep_and_invalid_round_artifacts_are_rejected(self):
        for round in (True, 0, -1, 101):
            with self.subTest(round=round), self.assertRaises(ValidationError):
                self.save(self.document(round=round))
        document = self.document()
        nested = []
        document["payload"]["nested"] = nested
        for _ in range(25):
            child = []
            nested.append(child)
            nested = child
        with self.assertRaises(ValidationError):
            self.save(document)


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class FactoryArtifactConcurrencyTests(FactoryFixture, TransactionTestCase):
    def test_concurrent_exact_retry_creates_one_sequence(self):
        document = self.document()
        barrier = Barrier(2)

        def store():
            close_old_connections()
            runner = RunnerIdentity.objects.get(pk=self.runner.pk)
            runner.controller_proven = True
            barrier.wait(timeout=10)
            try:
                return self.save(copy.deepcopy(document), runner=runner).pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: store(), range(2)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(FactoryArtifact.objects.count(), 1)


class FactoryArtifactMigrationTests(TransactionTestCase):
    def test_legacy_run_upgrades_without_fabricated_factory_history(self):
        previous = ("runner_control", "0016_integrated_runner_policy")
        current = ("runner_control", "0017_factory_artifacts")
        executor = MigrationExecutor(connection)
        executor.migrate([previous])
        apps = executor.loader.project_state([previous]).apps
        runner = apps.get_model("runner_control", "RunnerIdentity").objects.create(
            name="Legacy", token_digest="f" * 64
        )
        identity = uuid4()
        apps.get_model("runner_control", "Run").objects.create(
            id=identity, runner=runner, state="empty", stage="finished"
        )
        executor = MigrationExecutor(connection)
        executor.migrate([current])
        self.assertTrue(Run.objects.filter(pk=identity, stage="finished").exists())
        self.assertFalse(FactoryArtifact.objects.filter(run_id=identity).exists())
