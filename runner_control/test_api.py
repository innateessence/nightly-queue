import io
import json
import os
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from projects.models import Project
from work_queue.models import QueueItem
from work_queue.services import enqueue_item, requeue_items

from .auth import token_digest
from .models import Run, RunEvidence, RunnerIdentity


@override_settings(NQ_API_TOKEN="generic-api-token")
class RunnerAPITests(TestCase):
    def setUp(self):
        snapshot = patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.token = "runner-test-token-" + "a" * 32
        self.runner = RunnerIdentity.objects.create(
            name="controller", token_digest=token_digest(self.token)
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=self.project, title="First")
        self.run_id = uuid.uuid4()

    def post(self, name, data, run_id=None):
        kwargs = (
            {"run_id": run_id or self.run_id} if name not in ["runner_claim"] else {}
        )
        return self.client.post(
            reverse(name, kwargs=kwargs),
            json.dumps(data),
            content_type="application/json",
        )

    def claim(self, run_id=None):
        return self.post("runner_claim", {"run_id": str(run_id or self.run_id)})

    def claim_token(self):
        response = self.claim()
        self.assertEqual(response.status_code, 200)
        return response.json()["item"]["claim_token"]

    def finish(self, token, **result):
        fields = {"claim_token": token, "status": "failed", "error_summary": "Stopped"}
        fields.update(result)
        return self.post("runner_finish", fields)

    def artifact(self, token, kind="log", data=None, operation_id=None):
        return self.post(
            "runner_artifact",
            {
                "claim_token": token,
                "operation_id": str(operation_id or uuid.uuid4()),
                "kind": kind,
                "data": data or {},
            },
        )

    def test_identity_authenticates_runner_and_keeps_execution_disabled(self):
        response = self.client.get(reverse("runner_identity"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["runner"], {"id": self.runner.pk, "name": "controller"}
        )
        self.assertFalse(response.json()["capabilities"]["execution_enabled"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_shared_api_token_cannot_authenticate_runner(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer generic-api-token"
        self.assertEqual(self.claim().status_code, 401)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")

    def test_runner_token_cannot_authenticate_generic_api(self):
        response = self.client.post(
            reverse("api_claim"), "{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    def test_disabled_identity_cannot_read_runs(self):
        self.claim_token()
        self.runner.enabled = False
        self.runner.save()
        self.assertEqual(
            self.client.get(reverse("runner_detail", args=[self.run_id])).status_code,
            401,
        )

    def test_rotated_identity_rejects_old_token(self):
        self.runner.token_digest = token_digest("new-token-value")
        self.runner.save()
        self.assertEqual(self.client.get(reverse("runner_identity")).status_code, 401)

    def test_claim_links_run_and_checkout_atomically(self):
        token = self.claim_token()
        run = Run.objects.get(pk=self.run_id)
        self.item.refresh_from_db()
        self.assertEqual(run.item, self.item)
        self.assertEqual(run.runner, self.runner)
        self.assertEqual(run.claim_digest, token_digest(token))
        self.assertEqual(self.item.execution_ref, f"runner:{self.run_id}")
        self.assertEqual(self.item.repository_url, self.project.repository_url)

    def test_duplicate_claim_does_not_consume_another_item(self):
        second = enqueue_item(project=self.project, title="Second")
        first = self.claim().json()
        replay = self.claim().json()
        self.assertEqual(first, replay)
        second.refresh_from_db()
        self.assertEqual(second.status, "queued")
        self.assertEqual(Run.objects.count(), 1)

    def test_new_uuid_cannot_overlap_an_active_run(self):
        self.claim_token()
        self.assertEqual(self.claim(uuid.uuid4()).status_code, 409)
        self.assertEqual(Run.objects.count(), 1)

    def test_second_identity_cannot_overlap_active_run(self):
        self.claim_token()
        RunnerIdentity.objects.create(
            name="second", token_digest=token_digest("second")
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer second"
        self.assertEqual(self.claim(uuid.uuid4()).status_code, 409)

    def test_empty_outcome_stays_empty_after_work_arrives(self):
        self.item.delete()
        self.assertIsNone(self.claim().json()["item"])
        new = enqueue_item(project=self.project, title="New")
        replay = self.claim()
        self.assertEqual(replay.json()["run"]["state"], "empty")
        self.assertIsNone(replay.json()["item"])
        new.refresh_from_db()
        self.assertEqual(new.status, "queued")
        self.assertEqual(self.claim(uuid.uuid4()).json()["item"]["id"], new.pk)

    def test_lost_claim_response_reconciles_without_token_on_get(self):
        token = self.claim_token()
        response = self.client.get(reverse("runner_detail", args=[self.run_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["id"], self.item.pk)
        self.assertEqual(
            response.json()["item"]["repository_url"], self.project.repository_url
        )
        self.assertNotIn("claim_token", response.json()["item"])
        self.assertNotIn(token, response.content.decode())
        self.assertNotIn("claim_digest", response.content.decode())

    def test_protected_post_recovers_claim_token(self):
        token = self.claim_token()
        response = self.post("runner_recover", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["claim_token"], token)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_active_lookup_returns_owned_run_without_token(self):
        token = self.claim_token()
        response = self.client.get(reverse("runner_active"))
        self.assertEqual(response.json()["run"]["id"], str(self.run_id))
        self.assertNotIn(token, response.content.decode())

    def test_active_lookup_empty_does_not_claim(self):
        self.assertEqual(
            self.client.get(reverse("runner_active")).json(),
            {"run": None, "item": None},
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")

    def test_foreign_run_cannot_be_read_recovered_or_mutated(self):
        token = self.claim_token()
        RunnerIdentity.objects.create(
            name="second", token_digest=token_digest("second")
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer second"
        self.assertEqual(
            self.client.get(reverse("runner_detail", args=[self.run_id])).status_code,
            404,
        )
        self.assertEqual(self.post("runner_recover", {}).status_code, 404)
        self.assertEqual(self.finish(token).status_code, 404)
        self.assertEqual(self.artifact(token).status_code, 404)
        self.assertEqual(self.claim().status_code, 404)

    def test_lost_finish_response_reconciles_and_replays(self):
        token = self.claim_token()
        first = self.finish(token)
        self.assertEqual(first.status_code, 200)
        detail = self.client.get(reverse("runner_detail", args=[self.run_id])).json()
        self.assertEqual(detail["run"]["state"], "completed")
        self.assertEqual(detail["run"]["result_status"], "failed")
        self.assertEqual(first.json(), self.finish(token).json())

    def test_conflicting_finish_replay_is_rejected(self):
        token = self.claim_token()
        self.finish(token)
        self.assertEqual(self.finish(token, error_summary="Different").status_code, 409)

    def test_completed_run_rejects_wrong_token(self):
        token = self.claim_token()
        self.finish(token)
        self.assertEqual(self.finish(str(uuid.uuid4())).status_code, 409)

    def test_completed_run_uuid_cannot_claim_again(self):
        token = self.claim_token()
        self.finish(token)
        second = enqueue_item(project=self.project, title="Second")
        replay = self.claim().json()
        self.assertEqual(replay["run"]["state"], "completed")
        self.assertNotIn("claim_token", replay["item"])
        second.refresh_from_db()
        self.assertEqual(second.status, "queued")
        self.assertEqual(self.claim(uuid.uuid4()).json()["item"]["id"], second.pk)

    def test_admin_requeue_preserves_evidence_and_fences_old_claim(self):
        token = self.claim_token()
        self.artifact(token, data={"message": "Keep this"})
        self.assertEqual(requeue_items(QueueItem.objects.filter(pk=self.item.pk)), 1)
        run = Run.objects.get(pk=self.run_id)
        self.assertEqual(run.state, "obsolete")
        self.assertEqual(run.evidence.get().payload, {"message": "Keep this"})
        self.assertEqual(self.finish(token).status_code, 409)
        self.assertEqual(self.artifact(token).status_code, 409)
        self.assertEqual(self.post("runner_recover", {}).status_code, 409)
        retry = self.claim(uuid.uuid4()).json()
        self.assertEqual(retry["item"]["id"], self.item.pk)
        self.assertNotEqual(retry["item"]["claim_token"], token)
        self.assertEqual(Run.objects.count(), 2)

    def test_requeued_completed_failure_rejects_old_finish_replay(self):
        token = self.claim_token()
        self.finish(token)
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        self.assertEqual(self.finish(token).status_code, 409)

    def test_queue_removal_preserves_run_history_without_server_error(self):
        token = self.claim_token()
        self.finish(token)
        response = self.client.post(
            reverse("remove_item", args=[self.item.pk]), follow=True
        )
        self.assertContains(
            response, "Work with runner history must be retained for recovery."
        )
        self.assertTrue(QueueItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(Run.objects.filter(pk=self.run_id).exists())

    def test_generic_finish_cannot_complete_runner_owned_claim(self):
        token = self.claim_token()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer generic-api-token"
        response = self.client.post(
            reverse("api_finish", args=[self.item.pk]),
            json.dumps(
                {
                    "claim_token": token,
                    "status": "succeeded",
                    "result_url": "",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "running")

    def test_generic_execution_ref_prefix_does_not_establish_ownership(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer generic-api-token"
        response = self.client.post(
            reverse("api_claim"),
            json.dumps(
                {
                    "execution_ref": "runner:fake",
                }
            ),
            content_type="application/json",
        )
        token = response.json()["item"]["claim_token"]
        response = self.client.post(
            reverse("api_finish", args=[self.item.pk]),
            json.dumps(
                {
                    "claim_token": token,
                    "status": "succeeded",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Run.objects.count(), 0)

    def test_progress_duplicate_does_not_roll_back_newer_progress(self):
        token = self.claim_token()
        data = {
            "claim_token": token,
            "operation_id": str(uuid.uuid4()),
            "stage": "research",
        }
        self.assertEqual(self.post("runner_progress", data).status_code, 200)
        newer = {
            **data,
            "operation_id": str(uuid.uuid4()),
            "stage": "planning",
            "plan_reference": "plan:1",
        }
        self.post("runner_progress", newer)
        self.assertEqual(self.post("runner_progress", data).status_code, 200)
        run = Run.objects.get(pk=self.run_id)
        self.assertEqual(run.stage, "planning")
        self.assertEqual(run.plan_reference, "plan:1")
        self.assertEqual(run.evidence.count(), 2)

    def test_duplicate_artifact_upload_is_idempotent(self):
        token = self.claim_token()
        operation_id = uuid.uuid4()
        first = self.artifact(
            token, data={"message": "Saved"}, operation_id=operation_id
        )
        replay = self.artifact(
            token, data={"message": "Saved"}, operation_id=operation_id
        )
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(RunEvidence.objects.count(), 1)

    def test_conflicting_artifact_operation_is_rejected(self):
        token = self.claim_token()
        operation_id = uuid.uuid4()
        self.artifact(token, data={"message": "First"}, operation_id=operation_id)
        response = self.artifact(
            token, data={"message": "Different"}, operation_id=operation_id
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(RunEvidence.objects.get().payload, {"message": "First"})

    def test_display_evidence_redacts_claim_bearer_and_sensitive_keys(self):
        token = self.claim_token()
        self.artifact(
            token,
            data={
                "message": f"claim={token} auth={self.token}",
                "credentials": {
                    "secret": "credential-value",
                    "password": "password-value",
                },
                token: self.token,
            },
        )
        response = self.client.get(reverse("runner_detail", args=[self.run_id]))
        for secret in [token, self.token, "credential-value", "password-value"]:
            self.assertNotIn(secret, response.content.decode())
            self.assertNotIn(secret, json.dumps(RunEvidence.objects.get().payload))

    def test_finish_error_summary_redacts_tokens(self):
        token = self.claim_token()
        response = self.finish(token, error_summary=f"failed {token} {self.token}")
        self.assertNotIn(token, response.content.decode())
        self.assertNotIn(self.token, response.content.decode())
        self.item.refresh_from_db()
        self.assertNotIn(token, self.item.error_summary)

    def test_oversized_evidence_is_rejected(self):
        token = self.claim_token()
        self.assertEqual(
            self.artifact(token, data={"text": "x" * 65536}).status_code, 400
        )
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_deeply_nested_evidence_is_rejected(self):
        token = self.claim_token()
        payload = {}
        for _ in range(30):
            payload = {"nested": payload}
        self.assertEqual(self.artifact(token, data=payload).status_code, 400)

    def test_nonfinite_json_is_rejected(self):
        token = self.claim_token()
        self.assertEqual(
            self.artifact(token, data={"number": float("nan")}).status_code, 400
        )

    def test_evidence_rejects_null_characters(self):
        token = self.claim_token()
        response = self.artifact(token, data={"message": "log\x00entry"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_evidence_rejects_unpaired_surrogates(self):
        token = self.claim_token()
        response = self.artifact(token, data={"message": "\ud800"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_evidence_rejects_null_characters_in_keys(self):
        token = self.claim_token()
        response = self.artifact(token, data={"log\x00key": "entry"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_evidence_preserves_unicode_and_literal_json_escapes(self):
        token = self.claim_token()
        payload = {"message": "Saved \U0001f600 \u00e9", "escape": r"\u0000"}
        response = self.artifact(token, data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RunEvidence.objects.get().payload, payload)

    def test_progress_rejects_null_characters_in_references(self):
        token = self.claim_token()
        response = self.post(
            "runner_progress",
            {
                "claim_token": token,
                "operation_id": str(uuid.uuid4()),
                "stage": "research",
                "context_reference": "context\x00reference",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_finish_rejects_null_characters_in_error_summary(self):
        token = self.claim_token()
        response = self.finish(token, error_summary="error\x00message")
        self.assertEqual(response.status_code, 400)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "running")

    def test_evidence_is_bounded_and_paginated(self):
        token = self.claim_token()
        operation_ids = []
        for number in range(12):
            operation_id = uuid.uuid4()
            operation_ids.append(str(operation_id))
            self.artifact(
                token, kind="log", data={"number": number}, operation_id=operation_id
            )
        detail = self.client.get(reverse("runner_detail", args=[self.run_id])).json()[
            "run"
        ]
        self.assertEqual(len(detail["evidence"]), 10)
        self.assertTrue(detail["evidence_has_more"])
        first = self.client.get(reverse("runner_evidence", args=[self.run_id])).json()
        self.assertEqual(len(first["evidence"]), 10)
        self.assertTrue(first["has_more"])
        second = self.client.get(
            reverse("runner_evidence", args=[self.run_id]),
            {"after": first["next_after"]},
        ).json()
        entries = first["evidence"] + second["evidence"]
        self.assertEqual([entry["operation_id"] for entry in entries], operation_ids)
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_after"])

    def test_success_without_delivery_and_validation_is_rejected(self):
        token = self.claim_token()
        response = self.finish(
            token,
            status="succeeded",
            result_url="https://github.com/example/repo/pull/1",
        )
        self.assertEqual(response.status_code, 400)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "running")

    def test_success_requires_matching_delivery_and_passing_commands(self):
        token = self.claim_token()
        url = "https://github.com/example/repo/pull/1"
        self.artifact(
            token,
            "validation",
            {
                "passed": True,
                "commands": [{"command": "python -m unittest", "exit_code": 0}],
            },
        )
        self.artifact(
            token,
            "delivery",
            {"verified": True, "pull_request_url": url, "commit_sha": "a" * 40},
        )
        self.assertEqual(
            self.finish(
                token, status="succeeded", result_url=url, error_summary=""
            ).status_code,
            200,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "succeeded")

    def test_latest_failing_validation_prevents_success(self):
        token = self.claim_token()
        url = "https://github.com/example/repo/pull/1"
        self.artifact(
            token,
            "validation",
            {"passed": True, "commands": [{"command": "test", "exit_code": 0}]},
        )
        self.artifact(
            token,
            "validation",
            {"passed": False, "commands": [{"command": "test", "exit_code": 1}]},
        )
        self.artifact(
            token,
            "delivery",
            {"verified": True, "pull_request_url": url, "commit_sha": "a" * 40},
        )
        self.assertEqual(
            self.finish(token, status="succeeded", result_url=url).status_code, 400
        )

    def test_snapshot_hook_failure_leaves_item_unclaimed(self):
        with patch(
            "runner_control.services.locked_configuration_snapshot",
            side_effect=CommandError("hook failure"),
        ):
            with self.assertRaises(CommandError):
                self.claim()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertEqual(Run.objects.count(), 0)

    def test_run_insert_failure_rolls_back_queue_claim(self):
        with patch(
            "runner_control.services.Run.objects.create",
            side_effect=IntegrityError("insert failed"),
        ):
            with self.assertRaises(IntegrityError):
                self.claim()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertIsNone(self.item.claim_token)
        self.assertEqual(Run.objects.count(), 0)

    def test_claim_saves_effective_configuration_snapshot(self):
        snapshot = {"backend": "test", "model": "first"}
        with patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("a" * 64, snapshot),
        ):
            self.claim_token()
        run = Run.objects.get(pk=self.run_id)
        self.assertEqual(run.configuration_revision, "a" * 64)
        self.assertEqual(run.configuration_snapshot, snapshot)

    def test_active_database_constraint_rejects_missing_slot(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Run.objects.create(runner=self.runner, item=self.item, state="active")

    def test_bootstrap_persists_hash_and_never_prints_token(self):
        output = io.StringIO()
        token = "bootstrap-test-" + "b" * 32
        with patch.dict(os.environ, {"RUNNER_BOOTSTRAP_TEST": token}):
            call_command(
                "provision_runner",
                "bootstrap",
                token_env="RUNNER_BOOTSTRAP_TEST",
                stdout=output,
            )
        saved = RunnerIdentity.objects.get(name="bootstrap")
        self.assertEqual(saved.token_digest, token_digest(token))
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, saved.token_digest)

    def test_bootstrap_can_disable_and_rotate_identity(self):
        output = io.StringIO()
        call_command("provision_runner", "controller", disable=True, stdout=output)
        self.runner.refresh_from_db()
        self.assertFalse(self.runner.enabled)
        token = "replacement-" + "b" * 32
        with patch.dict(os.environ, {"RUNNER_BOOTSTRAP_TEST": token}):
            call_command(
                "provision_runner",
                "controller",
                token_env="RUNNER_BOOTSTRAP_TEST",
                stdout=output,
            )
        self.runner.refresh_from_db()
        self.assertTrue(self.runner.enabled)
        self.assertEqual(self.runner.token_digest, token_digest(token))

    def test_bootstrap_rejects_shared_api_token(self):
        token = "generic-" + "g" * 32
        with (
            override_settings(NQ_API_TOKEN=token),
            patch.dict(os.environ, {"RUNNER_BOOTSTRAP_TEST": token}),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "provision_runner", "bootstrap", token_env="RUNNER_BOOTSTRAP_TEST"
                )

    def test_bootstrap_rejects_reused_identity_token(self):
        with patch.dict(os.environ, {"RUNNER_BOOTSTRAP_TEST": self.token}):
            with self.assertRaises(CommandError):
                call_command(
                    "provision_runner", "second", token_env="RUNNER_BOOTSTRAP_TEST"
                )
