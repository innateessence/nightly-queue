import json

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_ssh_private_key,
)
from django.core.exceptions import ValidationError
from django.db import models
from django.views.decorators.debug import sensitive_variables

from .encryption import credential_cipher


class Credential(models.Model):
    class Kind(models.TextChoices):
        GITHUB_TOKEN = "github_token", "GitHub token"
        SSH_KEY = "ssh_key", "SSH private key"

    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    encrypted_payload = models.TextField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def clean(self):
        super().clean()
        self.name = self.name.strip()
        if not self.name:
            raise ValidationError({"name": "Enter a credential name."})
        if not self.encrypted_payload:
            raise ValidationError("Enter a secret before saving the credential.")
        if not self.pk:
            return
        kinds = Credential.objects.values_list("kind", flat=True)
        original = kinds.filter(pk=self.pk).first()
        if original is not None and original != self.kind:
            raise ValidationError(
                {"kind": "Create a new credential to use a different type."}
            )

    @sensitive_variables()
    def set_secret(self, secret, passphrase=""):
        if not isinstance(secret, str) or not secret.strip():
            raise ValidationError({"secret": "Enter a token or private key."})
        if not isinstance(passphrase, str):
            raise ValidationError({"passphrase": "Enter a text passphrase."})
        if self.kind == self.Kind.GITHUB_TOKEN:
            if any(character.isspace() for character in secret):
                raise ValidationError({"secret": "Tokens cannot contain whitespace."})
            if passphrase:
                raise ValidationError({"passphrase": "Only SSH keys use a passphrase."})
        elif self.kind == self.Kind.SSH_KEY:
            self.validate_private_key(secret, passphrase)
        else:
            raise ValidationError({"kind": "Select a supported credential type."})
        payload = json.dumps({"secret": secret, "passphrase": passphrase})
        self.encrypted_payload = (
            credential_cipher().encrypt(payload.encode("utf-8")).decode("ascii")
        )

    @staticmethod
    @sensitive_variables()
    def validate_private_key(secret, passphrase):
        password = passphrase.encode("utf-8") if passphrase else None
        loader = load_pem_private_key
        if secret.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
            loader = load_ssh_private_key
        try:
            loader(secret.encode("utf-8"), password=password)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            message = "Enter a valid private key and its passphrase, if encrypted."
            raise ValidationError({"secret": message}) from None

    @sensitive_variables()
    def get_secret(self):
        cipher = credential_cipher()
        try:
            payload = cipher.decrypt(self.encrypted_payload.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            raise ValidationError(
                "Unable to decrypt this credential; check NQ_CREDENTIAL_KEY."
            ) from None
        return json.loads(payload)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name
