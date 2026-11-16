import json

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_ssh_private_key,
)
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .encryption import credential_cipher


class Credential(models.Model):
    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"
        CODEX = "codex", "Codex"
        CLAUDE = "claude", "Claude Code"
        OPENCODE = "opencode", "OpenCode"

    class Kind(models.TextChoices):
        TOKEN = "token", "Token"
        SSH = "ssh", "SSH"
        SECRET = "secret", "Secret (other)"
        SESSION = "session", "Codex session (JSON)"
        CONNECTION = "connection", "OpenCode connection (JSON)"

    name = models.CharField(max_length=200, unique=True)
    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.GITHUB
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    encrypted_payload = models.TextField(editable=False)
    secret_version = models.PositiveIntegerField(default=1, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if "encrypted_payload" in field_names:
            instance._saved_encrypted_payload = instance.encrypted_payload
        return instance

    def refresh_from_db(self, using=None, fields=None):
        super().refresh_from_db(using=using, fields=fields)
        if fields is None or "encrypted_payload" in fields:
            self._saved_encrypted_payload = self.encrypted_payload

    def clean(self):
        super().clean()
        self.name = self.name.strip()
        if not self.name:
            raise ValidationError({"name": "Enter a credential name."})
        if not self.encrypted_payload:
            raise ValidationError("Enter a secret before saving the credential.")
        self.validate_provider_kind()
        if not self.pk:
            return
        original = (
            Credential.objects.filter(pk=self.pk).values("kind", "provider").first()
        )
        if original is not None and original != {
            "kind": self.kind,
            "provider": self.provider,
        }:
            raise ValidationError(
                {"kind": "Create a new credential to use a different provider or type."}
            )

    @sensitive_variables()
    def set_secret(self, secret, passphrase=""):
        if not isinstance(secret, str) or not secret.strip():
            raise ValidationError({"secret": "Enter a secret value."})
        if not isinstance(passphrase, str):
            raise ValidationError({"passphrase": "Enter a text passphrase."})
        if self.kind != self.Kind.SSH and passphrase:
            raise ValidationError({"passphrase": "Only SSH keys use a passphrase."})
        self.validate_provider_kind()
        if self.kind in (self.Kind.SESSION, self.Kind.CONNECTION):
            from .validation import validate_structured_secret

            validate_structured_secret(self.provider, self.kind, secret)
        elif self.kind == self.Kind.TOKEN:
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

    def validate_provider_kind(self):
        allowed = {
            self.Provider.GITHUB: (self.Kind.TOKEN, self.Kind.SSH, self.Kind.SECRET),
            self.Provider.GITLAB: (self.Kind.TOKEN, self.Kind.SSH, self.Kind.SECRET),
            self.Provider.CODEX: (self.Kind.TOKEN, self.Kind.SESSION),
            self.Provider.CLAUDE: (self.Kind.TOKEN,),
            self.Provider.OPENCODE: (self.Kind.CONNECTION,),
        }
        if self.kind not in allowed.get(self.provider, ()):
            raise ValidationError(
                {"kind": "Select a supported type for this provider."}
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
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != {"secret", "passphrase"}:
                raise ValueError
            if not all(isinstance(entry, str) for entry in value.values()):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise ValidationError(
                "Credential data is invalid; replace the secret."
            ) from None

    def save(self, *args, **kwargs):
        from runner_control.settings_models import RunnerSettings

        with transaction.atomic():
            RunnerSettings.objects.get_or_create(pk=1)
            RunnerSettings.objects.select_for_update().get(pk=1)
            original = Credential.objects.select_for_update().filter(pk=self.pk).first()
            if original and original.revoked_at:
                self.revoked_at = original.revoked_at
            fields = kwargs.get("update_fields")
            writes_payload = fields is None or "encrypted_payload" in fields
            if original:
                saved_payload = getattr(self, "_saved_encrypted_payload", None)
                if not writes_payload or self.encrypted_payload == saved_payload:
                    self.encrypted_payload = original.encrypted_payload
                rotated = original.encrypted_payload != self.encrypted_payload
                self.secret_version = original.secret_version + int(rotated)
                if rotated:
                    CredentialSecretVersion.objects.get_or_create(
                        credential=original,
                        version=original.secret_version,
                        defaults={
                            "encrypted_payload": original.encrypted_payload,
                            "provider": original.provider,
                            "kind": original.kind,
                        },
                    )
                if rotated and fields is not None:
                    kwargs["update_fields"] = set(fields) | {
                        "secret_version",
                        "updated_at",
                    }
            self.full_clean()
            result = super().save(*args, **kwargs)
            self._saved_encrypted_payload = self.encrypted_payload
            return result

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])

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
        for provider in (Credential.Provider.GITHUB, Credential.Provider.GITLAB):
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


class RunnerPreflightGrant(models.Model):
    runner = models.ForeignKey(
        "runner_control.RunnerIdentity", on_delete=models.CASCADE
    )
    credential = models.ForeignKey(Credential, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["runner", "credential"], name="runner_preflight_credential"
            )
        ]

    def __str__(self):
        return f"{self.runner}: {self.credential}"


class CredentialSecretVersion(models.Model):
    credential = models.ForeignKey(Credential, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    provider = models.CharField(max_length=20)
    kind = models.CharField(max_length=20)
    encrypted_payload = models.TextField(editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["credential", "version"], name="credential_secret_version"
            )
        ]

    def __str__(self):
        return f"Credential {self.credential_id}, retained version {self.version}"
