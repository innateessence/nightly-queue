from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.test import Client as Browser
from django.test import LiveServerTestCase
from django.urls import reverse

from coding_runner.approvals import ApprovalBroker, ApprovalRequired
from coding_runner.client import Client, ConflictError
from coding_runner.sandbox import ApprovedExecutor, probe_sandbox
from coding_runner.test_workspaces import WorkspaceFixture
from projects.models import Project
from work_queue.services import enqueue_item

from .auth import token_digest
from .models import RunnerIdentity


class ApprovalBoundaryHTTPChecks(WorkspaceFixture, LiveServerTestCase):
    def setUp(self):
        self.create_workspace()
        probe_sandbox(self.workspace.checkout)
        snapshot = patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        token = "boundary-http-runner-test-token"
        RunnerIdentity.objects.create(name="boundary", token_digest=token_digest(token))
        project = Project.objects.create(
            name="Boundary", repository_url="https://github.com/example/repo"
        )
        enqueue_item(project=project, title="Injected command remains data")
        self.api = Client(self.live_server_url, token)
        run_id = self.workspace.workspace_id
        response = self.api.request(
            "POST", "/api/v1/runner/runs/claim/", {"run_id": run_id}
        )
        self.broker = ApprovalBroker(self.api, run_id, response["item"]["claim_token"])
        self.executor = ApprovedExecutor(self.broker, self.sources, lambda: True)

    def decide(self, approval, decision):
        browser = Browser(enforce_csrf_checks=True)
        browser.get(reverse("runner_approval_inbox"))
        csrf = browser.cookies["csrftoken"].value
        response = browser.post(
            reverse("runner_approval_decide", args=[approval["id"]]),
            {"decision": decision, "binding_digest": approval["binding_digest"]},
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(response.status_code, 302)

    def test_real_api_pending_and_denial_prevent_sandbox_execution(self):
        action = self.action(["/usr/bin/touch", "/workspace/injected"])
        approval = self.broker.request(action, str(uuid4()))
        self.assertFalse((self.workspace.checkout / "injected").exists())
        with self.assertRaises(ConflictError):
            self.executor.execute(self.workspace, action, approval["id"])
        self.decide(approval, "denied")
        with self.assertRaises(ConflictError):
            self.executor.execute(self.workspace, action, approval["id"])
        self.assertFalse((self.workspace.checkout / "injected").exists())

    def test_real_api_exact_human_approval_executes_only_once(self):
        action = self.action(["/usr/bin/printf", "%s", "$(touch /workspace/injected)"])
        approval = self.broker.request(action, str(uuid4()))
        self.decide(approval, "approved")
        result = self.executor.execute(self.workspace, action, approval["id"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, b"$(touch /workspace/injected)")
        self.assertFalse((self.workspace.checkout / "injected").exists())
        restarted = ApprovedExecutor(
            ApprovalBroker(self.api, self.broker.run_id, self.broker.claim_token),
            self.sources,
            lambda: True,
        )
        with self.assertRaises(ApprovalRequired):
            restarted.execute(self.workspace, action, approval["id"])

    def test_concurrent_real_api_consumption_grants_one_permit(self):
        action = self.action()
        approval = self.broker.request(action, str(uuid4()))
        self.decide(approval, "approved")

        def consume(_):
            api = Client(self.live_server_url, self.api.token)
            payload = {
                "claim_token": self.broker.claim_token,
                "execution_id": str(uuid4()),
                "binding_digest": action.binding_digest,
            }
            path = f"/api/v1/runner/runs/{self.broker.run_id}/approvals/{approval['id']}/consume/"
            return api.request("POST", path, payload)["permit"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            permits = list(pool.map(consume, range(2)))
        self.assertEqual(sorted(permits), [False, True])
