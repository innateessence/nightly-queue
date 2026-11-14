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
    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"

    class Kind(models.TextChoices):
        TOKEN = "token", "Token"
        SSH = "ssh", "SSH"
        SECRET = "secret", "Secret (other)"

    name = models.CharField(max_length=200, unique=True)
    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.GITHUB
    )
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
            raise ValidationError({"secret": "Enter a secret value."})
        if not isinstance(passphrase, str):
            raise ValidationError({"passphrase": "Enter a text passphrase."})
        if self.kind != self.Kind.SSH and passphrase:
            raise ValidationError({"passphrase": "Only SSH keys use a passphrase."})
        if self.kind == self.Kind.TOKEN:
            if any(character.isspace() for character in secret):
                raise ValidationError({"secret": "Tokens cannot contain whitespace."})
        elif self.kind == self.Kind.SSH:
            self.validate_private_key(secret, passphrase)
        elif self.kind != self.Kind.SECRET:
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


class APISettings(models.Model):
    class SyncInterval(models.IntegerChoices):
        HOURLY = 1, "1h"
        FOUR_HOURS = 4, "4h"
        EIGHT_HOURS = 8, "8h"
        TWELVE_HOURS = 12, "12h"
        DAILY = 24, "24h"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    auto_sync_interval_hours = models.PositiveSmallIntegerField(
        choices=SyncInterval.choices, default=SyncInterval.HOURLY
    )
    last_auto_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    github_credential = models.ForeignKey(
        Credential,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="github_api_settings",
        limit_choices_to={"provider": "github", "kind": "token"},
    )
    gitlab_credential = models.ForeignKey(
        Credential,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gitlab_api_settings",
        limit_choices_to={"provider": "gitlab", "kind": "token"},
    )

    def clean(self):
        super().clean()
        if self.pk != 1:
            raise ValidationError("Only one API settings record is allowed.")
        for provider in Credential.Provider.values:
            field = f"{provider}_credential"
            credential = getattr(self, field)
            if credential is None:
                continue
            if (
                credential.provider != provider
                or credential.kind != Credential.Kind.TOKEN
            ):
                raise ValidationError(
                    {field: f"Select a {provider.title()} token credential."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
