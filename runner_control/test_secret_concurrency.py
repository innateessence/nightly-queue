from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from credentials.models import Credential, RunnerPreflightGrant
from credentials.test_models import TEST_KEY

from .auth import token_digest
from .models import RunnerIdentity
from .secrets import secret_document
from .settings_models import RunnerSettings


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class SelectedSecretConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            name="owner", token_digest=token_digest("concurrency-owner")
        )
        self.credential = Credential(name="Backend", provider="codex", kind="token")
        self.credential.set_secret("original-private-secret")
        self.credential.save()
        saved = RunnerSettings.objects.get(pk=1)
        saved.implementation_backend = "codex"
        saved.implementation_credential = self.credential
        saved.save()
        RunnerPreflightGrant.objects.create(
            runner=self.runner, credential=self.credential
        )
        self.data = {
            "scope": "preflight",
            "backend": "codex",
            "credential_id": self.credential.pk,
        }

    def action(self, function, barrier):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            barrier.wait(timeout=5)
            return function()
        finally:
            connections.close_all()

    def retrieve(self):
        try:
            return secret_document(self.data, self.runner)
        except ValidationError:
            return None

    def rotate(self):
        credential = Credential.objects.get(pk=self.credential.pk)
        credential.set_secret("rotated-private-secret")
        credential.save()

    def revoke(self):
        Credential.objects.get(pk=self.credential.pk).revoke()

    def test_rotation_racing_delivery_keeps_material_and_version_consistent(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(self.action, self.retrieve, barrier)
            rotation = pool.submit(self.action, self.rotate, barrier)
            response = delivery.result(timeout=10)
            rotation.result(timeout=10)
        pair = response["credential"]["version"], response["material"]["secret"]
        self.assertIn(
            pair, [(1, "original-private-secret"), (2, "rotated-private-secret")]
        )
        current = self.retrieve()
        self.assertEqual(current["credential"]["version"], 2)
        self.assertEqual(current["material"]["secret"], "rotated-private-secret")

    def test_revocation_racing_delivery_blocks_subsequent_delivery(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(self.action, self.retrieve, barrier)
            revocation = pool.submit(self.action, self.revoke, barrier)
            response = delivery.result(timeout=10)
            revocation.result(timeout=10)
        if response is not None:
            self.assertEqual(response["material"]["secret"], "original-private-secret")
        self.assertIsNone(self.retrieve())
