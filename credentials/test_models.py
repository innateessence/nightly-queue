from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from projects.models import Project

from .models import Credential


TEST_KEY = Fernet.generate_key().decode("ascii")


def private_key(passphrase=""):
    encryption = serialization.NoEncryption()
    if passphrase:
        encryption = serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption
    ).decode("ascii")


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class CredentialTests(TestCase):
    def make_credential(
        self, kind=Credential.Kind.GITHUB_TOKEN, secret="test-token", passphrase=""
    ):
        credential = Credential(name="Repository access", kind=kind)
        credential.set_secret(secret, passphrase)
        credential.save()
        return credential

    def test_token_round_trip(self):
        credential = self.make_credential()
        credential.refresh_from_db()
        self.assertEqual(
            credential.get_secret(), {"secret": "test-token", "passphrase": ""}
        )

    def test_database_stores_ciphertext(self):
        credential = self.make_credential()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT encrypted_payload FROM credentials_credential WHERE id = %s",
                [credential.pk],
            )
            payload = cursor.fetchone()[0]
        self.assertNotIn("test-token", payload)
        self.assertEqual(
            Fernet(TEST_KEY).decrypt(payload.encode("ascii")),
            b'{"secret": "test-token", "passphrase": ""}',
        )

    def test_ssh_key_round_trip(self):
        key = private_key()
        credential = self.make_credential(Credential.Kind.SSH_KEY, key)
        credential.refresh_from_db()
        self.assertEqual(credential.get_secret()["secret"], key)
        self.assertNotIn(key, credential.encrypted_payload)

    def test_pem_private_key_is_supported(self):
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        credential = self.make_credential(Credential.Kind.SSH_KEY, pem)
        self.assertEqual(credential.get_secret()["secret"], pem)

    def test_ssh_passphrase_is_encrypted(self):
        passphrase = " key password "
        key = private_key(passphrase)
        credential = self.make_credential(Credential.Kind.SSH_KEY, key, passphrase)
        credential.refresh_from_db()
        self.assertEqual(credential.get_secret()["passphrase"], passphrase)
        self.assertNotIn(passphrase, credential.encrypted_payload)

    def test_ssh_key_rejects_wrong_passphrase(self):
        key = private_key("password")
        with self.assertRaises(ValidationError):
            self.make_credential(Credential.Kind.SSH_KEY, key, "incorrect")

    def test_ssh_key_rejects_public_key(self):
        key = ed25519.Ed25519PrivateKey.generate().public_key()
        public = key.public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        with self.assertRaises(ValidationError):
            self.make_credential(Credential.Kind.SSH_KEY, public.decode("ascii"))

    def test_ssh_key_rejects_arbitrary_text(self):
        with self.assertRaises(ValidationError):
            self.make_credential(Credential.Kind.SSH_KEY, "not a private key")

    def test_empty_secret_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_credential(secret=" ")

    def test_token_rejects_whitespace(self):
        with self.assertRaises(ValidationError):
            self.make_credential(secret="test token")

    def test_token_rejects_passphrase(self):
        with self.assertRaises(ValidationError):
            self.make_credential(passphrase="password")

    def test_missing_secret_cannot_be_saved(self):
        credential = Credential(name="Empty", kind=Credential.Kind.GITHUB_TOKEN)
        with self.assertRaises(ValidationError):
            credential.save()

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_credential(kind="unsupported")

    def test_name_is_trimmed(self):
        credential = self.make_credential()
        credential.name = "  Renamed  "
        credential.save()
        self.assertEqual(credential.name, "Renamed")

    def test_blank_name_is_rejected(self):
        credential = self.make_credential()
        credential.name = "   "
        with self.assertRaises(ValidationError):
            credential.save()

    def test_duplicate_name_is_rejected(self):
        self.make_credential()
        with self.assertRaises(ValidationError):
            self.make_credential()

    def test_type_cannot_change(self):
        credential = self.make_credential()
        credential.kind = Credential.Kind.SSH_KEY
        with self.assertRaises(ValidationError):
            credential.save()

    @override_settings(NQ_CREDENTIAL_KEY="")
    def test_missing_encryption_key_disables_storage(self):
        with self.assertRaises(ImproperlyConfigured):
            self.make_credential()
        self.assertFalse(Credential.objects.exists())

    @override_settings(NQ_CREDENTIAL_KEY="invalid")
    def test_invalid_encryption_key_disables_storage(self):
        with self.assertRaises(ImproperlyConfigured):
            self.make_credential()

    def test_wrong_key_cannot_decrypt(self):
        credential = self.make_credential()
        with override_settings(NQ_CREDENTIAL_KEY=Fernet.generate_key().decode("ascii")):
            with self.assertRaises(ValidationError):
                credential.get_secret()

    def test_tampered_ciphertext_cannot_decrypt(self):
        credential = self.make_credential()
        credential.encrypted_payload = "invalid-ciphertext"
        with self.assertRaises(ValidationError):
            credential.get_secret()

    def test_representation_does_not_include_secret(self):
        credential = self.make_credential()
        self.assertEqual(str(credential), "Repository access")
        self.assertNotIn("test-token", repr(credential))

    def test_credential_can_be_shared_by_projects(self):
        credential = self.make_credential()
        fields = {
            "repository_url": "https://github.com/example/repo",
            "credential": credential,
        }
        first = Project.objects.create(name="First", **fields)
        second = Project.objects.create(name="Second", **fields)
        self.assertEqual(first.credential_id, second.credential_id)

    def test_used_credential_cannot_be_deleted(self):
        credential = self.make_credential()
        Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            credential=credential,
        )
        with self.assertRaises(ProtectedError):
            credential.delete()
