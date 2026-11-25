from copy import deepcopy
from uuid import UUID, uuid4

from django.test import TestCase, override_settings

from credentials.test_models import TEST_KEY
from vibearr_runner.test_factory_delivery import review_chain
from vibearr_runner.factory_routing import default_configuration, route
from vibearr_runner.native_contract import digest

from .factory_delivery import validate_factory_workspace
from .factory_models import FactoryArtifact
from .models import Run
from .plan_models import PlanVersion
from .services import RunConflict
from .test_native_protocol import NativeProtocolMixin


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class ServerFactoryPublicationTests(NativeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.complete_delivery()
        self.run.refresh_from_db()
        self.configuration = {**default_configuration(), "enabled": True}
        snapshot = deepcopy(self.run.configuration_snapshot)
        snapshot["effective"]["factory"] = self.configuration
        Run.objects.filter(pk=self.run.pk).update(configuration_snapshot=snapshot)
        self.run.refresh_from_db()
        self.intake = {"verdict": "accept", "risk": "low", "surfaces": []}
        self.save_document({"role": "intake", "round": 1, "payload": self.intake})
        specification = self.save_document(
            {
                "role": "specification",
                "round": 1,
                "context_id": str(self.context.pk),
                "payload": {
                    "criteria": [
                        {"id": "criterion-1", "description": "Implement useful work"}
                    ]
                },
            }
        )
        previous = self.run.plans.last()
        document = {
            **previous.document,
            "factory_specification_id": str(specification.pk),
            "factory_specification_digest": specification.digest,
        }
        self.plan = PlanVersion.objects.create(
            run=self.run,
            context=self.context,
            version=2,
            supersedes=previous,
            operation_id=uuid4(),
            document=document,
            digest=digest(document),
            reason="Bound publication fixture",
        )
        Run.objects.filter(pk=self.run.pk).update(plan_reference=str(self.plan.pk))
        self.run.refresh_from_db()
        records, _ = review_chain()
        source = self.run.evidence.get(kind="native_workspace")
        source.payload["factory_paths"] = ["file.py"]
        source.save(update_fields=["payload"])
        self.source = source
        binding = {
            "run_id": str(self.run.pk),
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": str(self.plan.pk),
            "specification_digest": self.plan.digest,
            "source_digest": source.payload["source_digest"],
            "tree_sha": self.workspace["tree_sha"],
            "base_sha": self.workspace["base_revision"],
            "head_sha": self.workspace["commit_sha"],
            "configuration_digest": digest(snapshot),
        }
        validation = self.run.evidence.get(kind="native_validation")
        records[1]["document"]["payload"]["validation_id"] = str(
            validation.operation_id
        )
        records[2]["document"]["payload"] = route(
            self.configuration, self.intake, ["file.py"]
        )
        self.records = []
        for entry in records:
            entry["document"].update(binding)
            saved = self.save_document(
                entry["document"], identity=entry["id"], specification=self.plan
            )
            self.records.append(saved)

    def save_document(self, document, identity=None, specification=None):
        document = {"context_id": str(self.context.pk), **document}
        return FactoryArtifact.objects.create(
            id=UUID(identity) if identity else uuid4(),
            run=self.run,
            context=self.context,
            specification=specification,
            sequence=self.run.factory_artifacts.count() + 1,
            role=document["role"],
            round=document["round"],
            operation_id=uuid4(),
            document=document,
            digest=digest(document),
            reason="Saved immutable prerequisite fixture",
        )

    def check(self):
        return validate_factory_workspace(self.run, self.workspace, self.context)

    def test_server_accepts_saved_chain_with_real_independent_check_reference(self):
        self.assertEqual(self.check()["decision_id"], str(self.records[-1].pk))

    def test_server_rejects_checks_that_only_declare_success(self):
        document = deepcopy(self.records[1].document)
        document["payload"]["validation_id"] = str(uuid4())
        self.replace_fixture(self.records[1], document)
        with self.assertRaisesMessage(RunConflict, "independent validation receipt"):
            self.check()

    def test_later_failed_validation_cannot_reuse_older_passing_checks(self):
        validation = self.run.evidence.get(kind="native_validation")
        self.run.evidence.create(
            operation_id=uuid4(),
            kind="native_validation",
            payload={**validation.payload, "state": "failed"},
        )
        with self.assertRaisesMessage(RunConflict, "independent validation receipt"):
            self.check()

    def replace_fixture(self, saved, document):
        from django.db import models

        models.QuerySet.update(
            FactoryArtifact.objects.filter(pk=saved.pk),
            document=document,
            digest=digest(document),
        )

    def test_server_recomputes_security_routes_from_attested_paths(self):
        self.source.payload["factory_paths"] = ["credentials/token.py"]
        self.source.save(update_fields=["payload"])
        with self.assertRaisesMessage(
            RunConflict, "trusted configuration and inspected paths"
        ):
            self.check()

    def test_server_cannot_publish_without_trusted_path_inventory(self):
        self.source.payload.pop("factory_paths")
        self.source.save(update_fields=["payload"])
        with self.assertRaisesMessage(RunConflict, "attested changed-path inventory"):
            self.check()

    def test_server_rejects_path_attestation_from_another_context(self):
        self.source.payload["context_id"] = str(uuid4())
        self.source.save(update_fields=["payload"])
        with self.assertRaises(RunConflict):
            self.check()

    def test_server_rejects_candidate_from_previous_specification(self):
        current = self.run.plans.first()
        Run.objects.filter(pk=self.run.pk).update(plan_reference=str(current.pk))
        self.run.refresh_from_db()
        with self.assertRaises(RunConflict):
            self.check()

    def test_server_rejects_review_with_unresolved_findings(self):
        document = deepcopy(self.records[-2].document)
        document["payload"]["findings"] = [{"id": "finding-1"}]
        self.replace_fixture(self.records[-2], document)
        with self.assertRaises(RunConflict):
            self.check()
