"""Selected structured credentials contain independently sensitive values."""

import json
from urllib.parse import urlsplit

SECRET_FIELDS = {
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "token",
    "secret",
    "password",
    "passphrase",
    "authorization",
    "bearertoken",
    "clientsecret",
    "databaseurl",
}


def protection_material(secret: bytes, database_url: str = "") -> bytes:
    if not database_url:
        return secret
    values = {
        "secret": secret.decode(),
        "database_url": database_url,
        "password": urlsplit(database_url).password or "",
    }
    try:
        values["backend"] = json.loads(secret)
    except (ValueError, UnicodeError, RecursionError):
        pass
    return json.dumps(values, sort_keys=True).encode()


def secret_values(secret: bytes) -> tuple[bytes, ...]:
    if not secret:
        return ()
    values = {secret}
    try:
        document = json.loads(secret)
    except (ValueError, UnicodeError, RecursionError):
        return (secret,)
    pending = [("", document)]
    while pending:
        key, value = pending.pop()
        if isinstance(value, dict):
            pending.extend(
                (str(name).lower().replace("_", ""), item)
                for name, item in value.items()
            )
        elif isinstance(value, list):
            pending.extend((key, item) for item in value)
        elif key in SECRET_FIELDS and isinstance(value, str):
            encoded = json.dumps(value)[1:-1].encode()
            if len(encoded) < 8:
                continue
            values.add(encoded)
            try:
                values.add(value.encode())
            except UnicodeError:
                continue
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def redact(content: bytes, secret: bytes, *, incomplete: bool = False) -> bytes:
    for value in secret_values(secret):
        content = content.replace(value, b"[REDACTED]")
        if not incomplete:
            continue
        for length in range(min(len(value) - 1, len(content)), 0, -1):
            if content.endswith(value[:length]):
                content = content[:-length]
                break
    return content


def contains_secret(content: bytes, secret: bytes) -> bool:
    return any(value in content for value in secret_values(secret))
