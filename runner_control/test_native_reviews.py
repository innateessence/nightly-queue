import copy
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import quote
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from credentials.test_models import TEST_KEY
from vibearr_runner import native_contract as contract

from .models import RepositoryMergeFence
from .review_services import control_files, provider_publication
from .services import RunConflict, finish_run
from .settings_models import RunnerSettings
from .test_automatic_merge import MergeProtocolMixin


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeReviewTests(MergeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.complete_delivery()

    def result(self, intent, *, findings=None):
        role = intent.payload["data"]["role"]
        report = copy.deepcopy(
            next(
                entry["report"]
                for entry in self.receipt()["reviews"]
                if entry["role"] == role
            )
        )
        report["findings"] = findings or []
        if any(entry["priority"] in {"P0", "P1", "P2"} for entry in report["findings"]):
            report["verdict"] = "changes_requested"
        observed = self.observed(
            intent.payload["data"]["session"]["manifest"]["operation_id"],
            self.changed,
            self.changed,
        )
        return {
            "schema_version": 1,
            "intent_id": str(intent.operation_id),
            "role": role,
            "receipt": contract.receipt_summary(observed),
            "successful": True,
            "session_id": report["session_id"],
            "agent_version": self.session.agent_version,
            "usage": [],
            "report": report,
            "report_digest": contract.digest(report),
        }

    def publication_pair(self):
        facts = {role: self.complete_review(role) for role in ("independent", "root")}
        return facts, self.review_publication(facts)

    def publication_intent(self, data):
        return contract.intent_document(
            "review_publication", **self.common(), data=data
        )

    def register_control_push(self):
        facts, data = self.publication_pair()
        with patch("runner_control.review_services.provider_control_base"):
            intent = self.write("native_intent", self.publication_intent(data))
        self.intent_ids.append(str(intent.operation_id))
        return facts, data, intent

    def provider_fixture(self, data, reviews):
        client = Mock()
        client.repository.return_value = ({"repository": data["repository"]}, {})
        prefix = f"/repos/{data['repository']}"
        responses = {
            "/user": {"id": 456},
            f"{prefix}/git/commits/{data['commit_sha']}": {
                "sha": data["commit_sha"],
                "parents": [{"sha": data["base_revision"]}],
            },
            f"{prefix}/compare/{data['base_revision']}...{data['commit_sha']}": {
                "files": [
                    {"filename": entry["path"], "status": "added"}
                    for entry in data["files"]
                ]
            },
            f"{prefix}/git/ref/heads/{quote(data['branch'], safe='')}": {
                "ref": "refs/heads/" + data["branch"],
                "object": {"sha": data["commit_sha"]},
            },
        }
        client.request.side_effect = lambda method, path: responses[path]
        profile = self.run.configuration_snapshot["effective"]["merge_profile"]
        content = control_files(self.run, data, reviews, profile)
        client.content.side_effect = lambda repository, path, revision: content[path]
        value = {
            "schema_version": 1,
            "intent_id": str(uuid4()),
            "publication_id": data["publication_id"],
            "stage": data["stage"],
            "pushed_sha": data["commit_sha"],
            "pull_request": None,
            "status": None,
        }
        return client, responses, value, profile

    def status_fixture(self):
        self.complete_owned_reviews()
        fact = self.run.evidence.filter(
            kind="native_review_publication", payload__stage="status"
        ).first()
        data = self.run.evidence.get(operation_id=fact.payload["intent_id"]).payload[
            "data"
        ]
        client, _, _, profile = self.provider_fixture(data, self.review_facts)
        client.pull.return_value = {
            "number": 19,
            "html_url": "https://github.com/example/configured/pull/19",
            "user": {"id": 456},
            "head": {
                "repo": {"id": 123, "full_name": "example/configured"},
                "ref": data["branch"],
                "sha": data["commit_sha"],
            },
            "base": {
                "repo": {"id": 123, "full_name": "example/configured"},
                "ref": data["control_base"],
            },
        }
        status = fact.payload["status"]
        client.pages.return_value = [
            {
                "id": status["id"],
                "context": status["context"],
                "state": "success",
                "target_url": status["target_url"],
                "creator": {"id": 456},
            }
        ]
        return client, data, fact.payload, profile

    def test_review_is_original_run_owned_and_does_not_consume_coding_attempt(self):
        fact = self.complete_review("independent")
        self.assertEqual(fact.payload["receipt"]["run_id"], str(self.run.pk))
        self.assertEqual(
            self.run.evidence.filter(
                kind="native_intent", payload__kind="agent"
            ).count(),
            1,
        )
        self.assertFalse(self.run.approvals.exists())

    def test_unknown_review_blocks_next_paid_review(self):
        self.write("native_intent", self.review_intent("independent"))
        with self.assertRaisesMessage(RunConflict, "active or uncertain"):
            self.write("native_intent", self.review_intent("root"))

    def test_duplicate_role_cannot_start_another_paid_session(self):
        self.complete_review("independent")
        with self.assertRaisesMessage(RunConflict, "existing paid review"):
            self.write("native_intent", self.review_intent("independent"))

    def test_review_cannot_reuse_coding_operation(self):
        value = self.review_intent("independent")
        value["data"]["session"]["manifest"]["operation_id"] = self.session.manifest[
            "operation_id"
        ]
        with self.assertRaisesMessage(RunConflict, "existing executor operation"):
            self.write("native_intent", value)

    def test_review_requires_exact_committed_source(self):
        value = self.review_intent("independent")
        value["data"]["session"]["manifest"]["source_digest"] = "b" * 64
        with self.assertRaisesMessage(RunConflict, "exact committed candidate source"):
            self.write("native_intent", value)

    def test_second_role_cannot_extend_aggregate_review_deadline(self):
        self.complete_review("independent")
        value = self.review_intent("root")
        value["data"]["session"]["manifest"]["deadline"] = (
            datetime.fromisoformat(self.deadline) + timedelta(seconds=1)
        ).isoformat()
        with self.assertRaisesMessage(
            RunConflict, "original aggregate review deadline"
        ):
            self.write("native_intent", value)

    def test_repaired_pr_fact_cannot_restart_original_ci_clock(self):
        first = (
            self.run.evidence.filter(kind="native_delivery")
            .exclude(payload__pull_request=None)
            .first()
        )
        type(first).objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(seconds=1801)
        )
        self.write("native_delivery", first.payload)
        with self.assertRaisesMessage(RunConflict, "task, CI or review deadline"):
            self.write("native_intent", self.review_intent("independent"))

    def test_review_findings_are_a_valid_terminal_result_but_do_not_pass_gate(self):
        intent = self.write("native_intent", self.review_intent("independent"))
        value = self.result(
            intent,
            findings=[{"priority": "P2", "description": "A regression remains."}],
        )
        fact = self.write("native_review", value)
        root = self.complete_review("root")
        data = self.review_publication({"independent": fact, "root": root})
        with self.assertRaisesMessage(ValidationError, "unresolved P2"):
            self.write("native_intent", self.publication_intent(data))

    def test_successful_review_cannot_reuse_coding_session(self):
        intent = self.write("native_intent", self.review_intent("independent"))
        value = self.result(intent)
        value["session_id"] = "ses_provider-identity"
        value["report"]["session_id"] = value["session_id"]
        value["report_digest"] = contract.digest(value["report"])
        with self.assertRaisesMessage(RunConflict, "independent of coding"):
            self.write("native_review", value)

    def test_mutated_source_export_cannot_be_a_successful_review(self):
        intent = self.write("native_intent", self.review_intent("independent"))
        value = self.result(intent)
        value["receipt"]["export_digest"] = "c" * 64
        with self.assertRaisesMessage(ValidationError, "unchanged source/export"):
            self.write("native_review", value)

    def test_review_result_is_recordable_after_cancellation(self):
        intent = self.write("native_intent", self.review_intent("independent"))
        self.run.cancellation_requested_at = timezone.now()
        self.run.save(update_fields=["cancellation_requested_at"])
        fact = self.write("native_review", self.result(intent))
        self.assertTrue(fact.payload["successful"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)

    def test_termination_cannot_replace_a_recorded_review_receipt(self):
        self.complete_review("independent")
        self.operations[-1]["result"]["log_digest"] = "a" * 64
        with self.assertRaisesMessage(RunConflict, "protected result already reported"):
            self.terminate()

    def test_external_success_statuses_cannot_replace_native_review_operations(self):
        payload = self.merge_intent()
        with self.assertRaisesMessage(RunConflict, "owned native AI review"):
            self.write("native_intent", payload)
        self.assertFalse(RepositoryMergeFence.objects.exists())

    def test_unknown_control_publication_retains_claim_at_termination(self):
        self.register_control_push()
        with self.assertRaisesMessage(
            RunConflict, "Review publication outcome remains uncertain"
        ):
            self.terminate()
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)

    def test_definitely_unstarted_control_push_can_be_terminated(self):
        _, _, intent = self.register_control_push()
        self.write(
            "native_review_publication_terminal",
            {
                "schema_version": 1,
                "intent_id": str(intent.operation_id),
                "state": "not_started",
            },
        )
        self.terminate()
        result = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            outcome="failed",
            error_summary="Stopped before control push.",
        )
        self.assertEqual(result.state, "completed")

    def test_control_publication_cannot_change_declared_receipt_bytes(self):
        _, data = self.publication_pair()
        data["files"][1]["digest"] = "a" * 64
        with self.assertRaisesMessage(RunConflict, "protected review facts"):
            self.write("native_intent", self.publication_intent(data))

    def test_control_publication_rejects_extra_json_instruction_files(self):
        _, data = self.publication_pair()
        prefix = next(iter(data["report_paths"].values())).rsplit("/", 1)[0]
        data["files"].append(
            {"path": prefix + "/instructions.json", "digest": "a" * 64}
        )
        with self.assertRaisesMessage(RunConflict, "Additional control files"):
            self.write("native_intent", self.publication_intent(data))

    def test_provider_readback_confirms_exact_control_commit_and_files(self):
        reviews, data = self.publication_pair()
        client, _, value, profile = self.provider_fixture(data, reviews)
        with patch("runner_control.review_services.provider", return_value=client):
            provider_publication(self.run, data, value, reviews, profile)
        self.assertEqual(client.content.call_count, 4)

    def test_provider_readback_rejects_source_changes_outside_control_prefix(self):
        reviews, data = self.publication_pair()
        client, responses, value, profile = self.provider_fixture(data, reviews)
        key = f"/repos/{data['repository']}/compare/{data['base_revision']}...{data['commit_sha']}"
        responses[key]["files"].append(
            {"filename": "work_queue/views.py", "status": "modified"}
        )
        with patch("runner_control.review_services.provider", return_value=client):
            with self.assertRaisesMessage(RunConflict, "only the declared new report"):
                provider_publication(self.run, data, value, reviews, profile)

    def test_provider_readback_rejects_tampered_protected_sidecar(self):
        reviews, data = self.publication_pair()
        client, _, value, profile = self.provider_fixture(data, reviews)
        client.content.side_effect = None
        client.content.return_value = b"{}"
        with patch("runner_control.review_services.provider", return_value=client):
            with self.assertRaisesMessage(RunConflict, "protected owned review"):
                provider_publication(self.run, data, value, reviews, profile)

    def test_provider_status_readback_is_bound_to_application_head(self):
        client, data, value, profile = self.status_fixture()
        with patch("runner_control.review_services.provider", return_value=client):
            provider_publication(self.run, data, value, self.review_facts, profile)
        client.pages.assert_called_once_with(
            f"/repos/example/configured/commits/{self.workspace['commit_sha']}/statuses"
        )

    def test_provider_status_readback_rejects_forged_producer(self):
        client, data, value, profile = self.status_fixture()
        client.pages.return_value[0]["creator"]["id"] = 999
        with patch("runner_control.review_services.provider", return_value=client):
            with self.assertRaisesMessage(
                RunConflict, "exact-head owned review status"
            ):
                provider_publication(self.run, data, value, self.review_facts, profile)

    def test_provider_control_pr_requires_selected_actor(self):
        client, data, value, profile = self.status_fixture()
        client.pull.return_value["user"]["id"] = 999
        with patch("runner_control.review_services.provider", return_value=client):
            with self.assertRaisesMessage(
                RunConflict, "exact owned provider publication"
            ):
                provider_publication(self.run, data, value, self.review_facts, profile)

    def test_optional_external_root_gate_still_uses_exactly_two_native_reviews(self):
        profile = copy.deepcopy(
            self.run.configuration_snapshot["effective"]["merge_profile"]
        )
        profile["reviews"].append(
            {
                **profile["reviews"][1],
                "role": "external_root",
                "context": "ai/external-root",
            }
        )
        saved = RunnerSettings.objects.get(pk=1)
        saved.merge_profile = profile
        saved.save()
        self.run.configuration_snapshot["effective"]["merge_profile"] = profile
        self.run.save(update_fields=["configuration_snapshot"])
        self.complete_owned_reviews()
        value = self.merge_intent()
        with patch(
            "runner_control.merge_services.provider_authorization",
            return_value=value["data"]["receipt"],
        ):
            self.write("native_intent", value)
        self.assertEqual(self.run.evidence.filter(kind="native_review").count(), 2)
