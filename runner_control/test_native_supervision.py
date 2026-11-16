from dataclasses import replace
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from coding_runner.test_native_delivery import GitHub, Transport
from coding_runner.test_native_lifecycle import NativeLifecycleTests
from credentials.test_models import TEST_KEY
from vibearr_runner.client import APIError, ConflictError, NotFoundError
from vibearr_runner.native_delivery import NativeDelivery
from vibearr_runner.native_lifecycle import NativeLifecycle
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import git

from .api import response_data
from .test_native_protocol import NativeProtocolMixin


class APIBridge:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, payload=None):
        response = self.client.generic(
            method,
            path,
            data=json.dumps(payload) if payload else "",
            content_type="application/json",
        )
        if response.status_code != 200:
            error = {404: NotFoundError, 409: ConflictError}.get(
                response.status_code, APIError
            )
            raise error(str(response.json()))
        return response.json()


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeSupervisionTests(NativeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.supervision_run_id = str(self.run.pk)
        NativeLifecycleTests.setUp(self)
        branch = self.item.branch_name
        git(["branch", "-m", branch], self.workspace.checkout)
        self.workspace = replace(
            self.workspace,
            repository_url=self.item.repository_url,
            branch_name=branch,
            requested_base_branch=self.item.base_branch,
            resolved_base_branch="main",
        )
        self.state["run"] = response_data(self.run, include_claim=True)
        self.state["runner_id"] = self.runner.pk
        self.host.client = APIBridge(self.client)
        self.host.workspaces.prepare = lambda *args: self.workspace
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.item.repository_url,
            "default_branch": "main",
        }
        repository = patch(
            "work_queue.github.repository_metadata", return_value=observation
        )
        repository.start()
        self.addCleanup(repository.stop)
        native_repository = patch(
            "vibearr_runner.native_github.NativeGitHub.repository",
            return_value=(observation, {}),
        )
        native_repository.start()
        self.addCleanup(native_repository.stop)
        self.journal.save(self.state)

    def engine(self):
        return NativeLifecycle(
            self.host, executor=self.executor, validation_profile=self.profile
        )

    def run_to(self, phase):
        for _ in range(50):
            if self.state.get("native", {}).get("phase") == phase:
                return
            self.engine().step(self.state)
        self.fail("Native supervisor did not reach " + phase)

    def test_supervisor_receipts_finish_through_real_machine_api(self):
        self.run_to("delivery")
        github, transport = GitHub(), Transport()
        repository = self.item.repository_url.removeprefix(
            "https://github.com/"
        ).removesuffix(".git")
        github.repository = lambda **kwargs: (
            {
                "repository_id": 123,
                "repository": repository,
                "repository_url": self.item.repository_url,
                "default_branch": "main",
            },
            {},
        )
        with patch.object(NativeDelivery, "clients", return_value=(github, transport)):
            self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result_status, "succeeded")
        self.assertEqual(self.run.native_outcome, "delivered")
        self.assertEqual(github.creates, 1)
        self.assertEqual(self.run.evidence.filter(kind="verified_delivery").count(), 1)
        response = self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Native session")
        self.assertNotContains(response, "Every command still requires")

    def test_unknown_cancellation_cannot_release_claim_through_api(self):
        self.run_to("import_candidate")
        self.executor.unknown.add(self.executor.started[0])
        self.run.cancellation_requested_at = self.run.created_at
        self.run.save(update_fields=["cancellation_requested_at"])
        self.state["run"]["run"][
            "cancellation_requested_at"
        ] = self.run.created_at.isoformat()
        with self.assertRaises(ReconciliationRequired):
            self.engine().step(self.state)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, "active")
        self.assertEqual(self.run.active_slot, 1)
