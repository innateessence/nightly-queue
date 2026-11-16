from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import LiveServerTestCase

from coding_runner.client import AuthenticationError, Client
from coding_runner.controller import Controller
from coding_runner.journal import Journal
from projects.models import Project
from work_queue.services import enqueue_item

from .auth import token_digest
from .models import Run, RunnerIdentity


class RunnerStartupIntegrationTests(LiveServerTestCase):
    def setUp(self):
        self.token = "startup-test-runner-token-" + "a" * 32
        self.runner = RunnerIdentity.objects.create(
            name="startup-test", token_digest=token_digest(self.token)
        )
        project = Project.objects.create(
            name="Startup test", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=project, title="Must remain queued")
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "journal"

    def test_real_http_startup_authenticates_without_claiming(self):
        client = Client(self.live_server_url, self.token)
        with Journal(self.state_path) as journal:
            Controller(client, journal).run(check=True)
            self.assertEqual(journal.load()["runner_id"], self.runner.pk)
            self.assertIsNone(journal.load()["remote_active_run_id"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertEqual(Run.objects.count(), 0)

    def test_real_http_rejects_wrong_token_without_claiming(self):
        client = Client(self.live_server_url, "wrong-token")
        with Journal(self.state_path) as journal:
            with self.assertRaises(AuthenticationError):
                Controller(client, journal).run(check=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertEqual(Run.objects.count(), 0)
