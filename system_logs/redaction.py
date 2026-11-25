import json
import re
from urllib.parse import quote

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential, CredentialSecretVersion
from credentials.redaction import credential_values


@sensitive_variables()
def secret_values():
    names = (
        "SECRET_KEY",
        "VIBEARR_API_TOKEN",
        "VIBEARR_CREDENTIAL_KEY",
    )
    values = [getattr(settings, name, "") for name in names]
    values.extend(
        database.get("PASSWORD", "") for database in settings.DATABASES.values()
    )
    for credential in Credential.all_objects.exclude(encrypted_payload="").iterator():
        values.extend(credential_values(credential))
        values.append(credential.encrypted_payload)
    for version in CredentialSecretVersion.objects.exclude(
        encrypted_payload=""
    ).iterator():
        credential = Credential(
            provider=version.provider,
            kind=version.kind,
            encrypted_payload=version.encrypted_payload,
        )
        values.extend(credential_values(credential))
        values.append(version.encrypted_payload)
    variants = set()
    for value in values:
        if value:
            variants.update((value, json.dumps(value)[1:-1], quote(value, safe="")))
    return sorted(variants, key=len, reverse=True)


@sensitive_variables()
def redact(text, secrets):
    for secret in secrets:
        text = re.sub(re.escape(secret), "[redacted]", text, flags=re.I)
    text = re.sub(
        r"-----BEGIN [\w ]*PRIVATE KEY-----.*?-----END [\w ]*PRIVATE KEY-----",
        "[redacted]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+", r"\1 [redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)([?&][\w-]+=)[^&\s]+", r"\1[redacted]", text)
    pattern = r"""(?i)(["']?(?:[\w-]*(?:token|secret|password|passphrase|api[_-]?key)|authorization|cookie)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}]+)"""
    text = re.sub(pattern, r"\1[redacted]", text)
    return text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
