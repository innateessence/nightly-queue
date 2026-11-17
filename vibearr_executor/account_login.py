"""Explicit device-code sign-in to a registered private Codex account cache."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .codex_account import (
    account_identity,
    managed_auth,
    private_bytes,
    private_directory,
)
from .codex_account_rpc import account_environment
from .codex_task import BINARY, VERSION
from .contracts import canonical
from .runtime import durable


def device_auth(home, binary):
    environment = account_environment(home)
    version = subprocess.run(
        [binary, "--version"], cwd=home, env=environment, capture_output=True, timeout=5
    )
    if (
        version.returncode
        or version.stdout.strip() != ("codex-cli " + VERSION).encode()
    ):
        raise ValueError("Device sign-in requires the pinned Codex CLI.")
    command = [
        binary,
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'forced_login_method="chatgpt"',
        "login",
        "--device-auth",
    ]
    process = subprocess.Popen(
        command, cwd=home, env=environment, start_new_session=True, umask=0o077
    )
    try:
        status = process.wait(timeout=900)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    if status:
        raise ValueError(
            "Codex device sign-in did not complete; retain the private account cache."
        )


def login(root, reference, *, binary=BINARY):
    private_directory(root)
    if set(reference) != {"id", "version"} or any(
        type(value) is not int or value < 1 for value in reference.values()
    ):
        raise ValueError("Select the saved Codex account connection ID and version.")
    descriptor = os.open(
        root / "service.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Drain account tasks and stop codex-account before device sign-in."
            ) from None
        authority = private_bytes(root / "authority").decode()
        for path in (root / "leases").iterdir():
            lease = json.loads(private_bytes(path, 512 * 1024))
            if lease["authority"] == authority and not lease["closed"]:
                raise ValueError(
                    "An account operation still owns a lease; reconcile its positive termination first."
                )
        directory = root / "accounts" / f"{reference['id']}-{reference['version']}"
        private_directory(directory)
        binding = json.loads(private_bytes(directory / "binding.json"))
        if any(binding.get(key) != value for key, value in reference.items()):
            raise ValueError("Account storage differs from the selected connection.")
        home = directory / "codex"
        private_directory(home)
        account_lock = os.open(
            directory / "account.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(account_lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with tempfile.TemporaryDirectory(
                prefix=".device-login-", dir=directory
            ) as temporary:
                staged = Path(temporary)
                device_auth(staged, binary)
                account = managed_auth(private_bytes(staged / "auth.json"))
                expected = {key: binding.get(key) for key in ("account_id", "user_id")}
                if account_identity(account) != expected:
                    raise ValueError(
                        "Sign in to the same selected account and workspace, or select a new connection version."
                    )
                durable(home / "auth.json", canonical(account))
            marker = root / "restore-required.json"
            if marker.exists():
                durable(directory / "restore-authorization.json", private_bytes(marker))
            durable(
                directory / "login-observation.json",
                canonical(
                    {
                        "signed_in_at": datetime.now(timezone.utc).isoformat(),
                        "credential": reference,
                    }
                ),
            )
            return {
                "account_login": "saved",
                "credential": reference,
                "api_key_required": False,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-id", type=int, required=True)
    parser.add_argument("--credential-version", type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = login(
        Path("/var/lib/codex-account"),
        {"id": args.credential_id, "version": args.credential_version},
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
