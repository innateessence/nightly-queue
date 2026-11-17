"""Private managed account storage; task processes receive access tokens only."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from .codex_task import auth_material
from .contracts import canonical
from .runtime import durable


MAX_AUTH = 65536
OAUTH_ISSUER = "https://auth.openai.com"
OAUTH_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise ValueError("Account storage must be private and owned by its service.")


def private_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_nlink != 1
            or details.st_size > MAX_AUTH
        ):
            raise ValueError(
                "Account files must be bounded private regular files without links."
            )
        content = stream.read(MAX_AUTH + 1)
    if len(content) > MAX_AUTH:
        raise ValueError("Account file exceeds its byte limit.")
    return content


def managed_auth(content: bytes) -> dict[str, Any]:
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_AUTH:
        raise ValueError("Select a bounded managed Codex account login.")
    try:
        value = json.loads(content)
        if not isinstance(value, dict) or set(value) - {
            "auth_mode",
            "OPENAI_API_KEY",
            "tokens",
            "last_refresh",
        }:
            raise ValueError
        if (
            value.get("auth_mode", "chatgpt") != "chatgpt"
            or value.get("OPENAI_API_KEY") is not None
        ):
            raise ValueError
        tokens = value["tokens"]
        required = {"access_token", "refresh_token", "id_token", "account_id"}
        if not isinstance(tokens, dict) or not required <= set(tokens):
            raise ValueError
        if set(tokens) - required - {"login_owner_id", "oauth_provenance"}:
            raise ValueError
        optional = (
            {"login_owner_id"} if tokens.get("login_owner_id") is not None else set()
        )
        for name in required | optional:
            token = tokens[name]
            if (
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or any(char.isspace() for char in token)
            ):
                raise ValueError
        provenance = tokens.get("oauth_provenance")
        if provenance is not None and provenance != {
            "issuer": OAUTH_ISSUER,
            "client_id": OAUTH_CLIENT,
        }:
            raise ValueError
        if "last_refresh" in value and not isinstance(value["last_refresh"], str):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError(
            "Use a managed ChatGPT login from the pinned Codex CLI."
        ) from None


class AccountStore:
    def __init__(self, root: Path, refresh: Callable[..., None]):
        self.root, self.refresh = Path(root), refresh
        private_directory(self.root)

    def material(
        self, credential: dict[str, int], secret: str, *, force: bool = False
    ) -> str:
        if (
            not isinstance(credential, dict)
            or set(credential) != {"id", "version"}
            or any(type(value) is not int or value < 1 for value in credential.values())
        ):
            raise ValueError(
                "Bind account storage to its saved credential ID and version."
            )
        seed = managed_auth(secret.encode())
        account = seed["tokens"]["account_id"]
        directory = self.root / f"{credential['id']}-{credential['version']}"
        private_directory(directory)
        descriptor = os.open(
            directory / "account.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            home = directory / "codex"
            private_directory(home)
            binding = {
                **credential,
                "seed_digest": hashlib.sha256(secret.encode()).hexdigest(),
                "account_id": account,
            }
            metadata = directory / "binding.json"
            path = home / "auth.json"
            if metadata.exists():
                if json.loads(private_bytes(metadata)) != binding:
                    raise ValueError(
                        "Account seed changed without a new credential version."
                    )
                if managed_auth(private_bytes(path))["tokens"]["account_id"] != account:
                    raise ValueError(
                        "The saved account differs from the selected credential."
                    )
            else:
                if path.exists() or path.is_symlink():
                    raise ValueError(
                        "Reconcile the unbound account cache before reseeding."
                    )
                durable(path, canonical(seed))
                durable(metadata, canonical(binding))
            self.refresh(home, force=force)
            current = managed_auth(private_bytes(path))
            if current["tokens"]["account_id"] != account:
                raise ValueError(
                    "Codex refreshed a different account; sign in to the selected account again."
                )
            return auth_material("session", canonical(current).decode())
