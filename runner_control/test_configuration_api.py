from .test_support import provision_controller
import json
import uuid

from django.test import override_settings
from django.urls import reverse

from work_queue.models import QueueItem
from work_queue.services import enqueue_item

from .auth import token_digest
from .configuration import configuration_document
from .models import Run, RunnerIdentity
from .settings_models import RunnerSettings
from .test_settings import ConfiguredProjectTestCase


@override_settings(NQ_API_TOKEN="generic-token")
class RunnerConfigurationAPITests(ConfiguredProjectTestCase):
    def setUp(self):
        super().setUp()
        RunnerSettings.objects.filter(pk=1).update(enabled=True)
        self.token = "runner-settings-token-" + "a" * 32
        self.runner = RunnerIdentity.objects.create(
            name="configuration-test", token_digest=token_digest(self.token)
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        provision_controller(self)

    def claim(self, revision):
        return self.client.post(
            reverse("runner_claim"),
            json.dumps(
                {"run_id": str(uuid.uuid4()), "configuration_revision": revision}
            ),
            content_type="application/json",
        )

    def test_configuration_authenticates_runner(self):
        response = self.client.get(reverse("runner_configuration"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], 1)
        self.assertEqual(
            response.json()["revision"], configuration_document()["revision"]
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("encrypted-reference", response.content.decode())

    def test_generic_queue_token_cannot_read_configuration(self):
        response = self.client.get(
            reverse("runner_configuration"), HTTP_AUTHORIZATION="Bearer generic-token"
        )
        self.assertEqual(response.status_code, 401)

    def test_configuration_rejects_writes(self):
        self.assertEqual(
            self.client.post(reverse("runner_configuration"), {}).status_code, 405
        )

    def test_claim_saves_effective_snapshot_atomically(self):
        item = enqueue_item(project=self.project, title="Configured task")
        revision = configuration_document()["revision"]
        response = self.claim(revision)
        self.assertEqual(response.status_code, 200, response.content)
        run = Run.objects.get(item=item)
        self.assertEqual(run.configuration_revision, revision)
        self.assertEqual(
            run.configuration_snapshot["effective"]["research_model"], "research-model"
        )
        self.assertFalse(run.configuration_snapshot["execution_ready"])
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_model = "future-model"
        saved.save()
        run.refresh_from_db()
        self.assertEqual(
            run.configuration_snapshot["effective"]["research_model"], "research-model"
        )

    def test_stale_preflight_never_claims_item_or_creates_run(self):
        item = enqueue_item(project=self.project, title="Wait for preflight")
        revision = configuration_document()["revision"]
        saved = RunnerSettings.objects.get(pk=1)
        saved.paused = True
        saved.save()
        response = self.claim(revision)
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.status, QueueItem.Status.QUEUED)
        self.assertFalse(Run.objects.exists())

    def test_missing_configuration_never_claims_item(self):
        item = enqueue_item(project=self.project, title="Incomplete configuration")
        saved = RunnerSettings.objects.get(pk=1)
        saved.implementation_model = ""
        saved.save()
        response = self.claim(configuration_document()["revision"])
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, QueueItem.Status.QUEUED)
        self.assertIsNone(response.json()["item"])

    def test_invalid_project_does_not_starve_valid_manual_work(self):
        from projects.models import Project
        from .settings_models import ProjectRunnerSettings

        invalid = Project.objects.create(
            name="Invalid", repository_url="https://github.com/example/invalid"
        )
        ProjectRunnerSettings.objects.create(
            project=invalid, overrides={"implementation_model": {"mode": "clear"}}
        )
        waiting = enqueue_item(project=invalid, title="Invalid first")
        valid = enqueue_item(project=self.project, title="Valid second")
        response = self.claim(configuration_document()["revision"])
        self.assertEqual(response.json()["item"]["id"], valid.pk)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "queued")

    def test_disabled_native_execution_does_not_claim_manual_work(self):
        enqueue_item(project=self.project, title="Wait")
        RunnerSettings.objects.filter(pk=1).update(enabled=False)
        self.assertIsNone(
            self.claim(configuration_document()["revision"]).json()["item"]
        )
