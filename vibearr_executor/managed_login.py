"""Persist only selected native subscription login material."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

from .runtime import durable
from .worker import terminate_children


ROOT = Path("/logins")
FILES = {
    "codex": (".codex/auth.json",),
    "claude": (".claude/.credentials.json", ".claude.json"),
}


def bounded_json(path, *, directory=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        value = stream.read(16385)
    if len(value) > 16384:
        raise ValueError("Login material exceeds its bound.")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Invalid subscription login material.")
    return result


def validate(provider, files):
    if provider == "codex":
        auth = files[".codex/auth.json"]
        tokens = auth.get("tokens")
        if auth.get("OPENAI_API_KEY") or auth.get("auth_mode", "chatgpt") != "chatgpt":
            raise ValueError("Codex requires a ChatGPT subscription login.")
        if not isinstance(tokens, dict) or not all(
            isinstance(tokens.get(key), str) and tokens[key]
            for key in ("access_token", "refresh_token", "id_token", "account_id")
        ):
            raise ValueError("Codex subscription login is incomplete.")
    elif provider == "claude":
        auth = files[".claude/.credentials.json"].get("claudeAiOauth")
        if not isinstance(auth, dict) or not all(
            isinstance(auth.get(key), str) and auth[key]
            for key in ("accessToken", "refreshToken")
        ):
            raise ValueError("Claude requires a Claude subscription login.")
    else:
        raise ValueError("Choose Codex or Claude subscription login.")
    return files


def login_path(provider, credential_id):
    if provider not in FILES or type(credential_id) is not int or credential_id < 1:
        raise ValueError("Select a subscription provider and positive credential ID.")
    return ROOT / provider / str(credential_id)


def read_login(provider, credential_id):
    path = login_path(provider, credential_id) / "login.json"
    return validate(provider, bounded_json(path))


def collect(provider, home):
    files = {}
    for relative in FILES[provider]:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = relative.split("/")
            for part in parts[:-1]:
                nested = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = nested
            files[relative] = bounded_json(parts[-1], directory=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)
    if ".claude.json" in files:
        account = files[".claude.json"].get("oauthAccount", {})
        identity = {
            key: account[key]
            for key in ("accountUuid", "organizationUuid")
            if isinstance(account, dict)
            and isinstance(account.get(key), str)
            and account[key]
        }
        files[".claude.json"] = {"hasCompletedOnboarding": True}
        if identity:
            files[".claude.json"]["oauthAccount"] = identity
    return validate(provider, files)


def save_login(provider, credential_id, files):
    validate(provider, files)
    path = login_path(provider, credential_id)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable(path / "login.json", json.dumps(files).encode())


def install_login(provider, files, home):
    validate(provider, files)
    for relative, value in files.items():
        if relative not in FILES[provider]:
            raise ValueError("Login bundle contains an unexpected file.")
        path = home / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        durable(path, json.dumps(value).encode())


def main():
    parser = argparse.ArgumentParser(
        description="Manage subscription logins inside the runner; no model API keys."
    )
    parser.add_argument("action", choices=["login", "logout", "status"])
    parser.add_argument("provider", choices=sorted(FILES))
    parser.add_argument("--credential-id", required=True, type=int)
    args = parser.parse_args()
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Stop active coding work before changing a login.")
        if (ROOT / "execution-lease.json").exists():
            raise SystemExit(
                "Reconcile the prior coding operation before changing a login."
            )
        target = login_path(args.provider, args.credential_id)
        if args.action == "logout":
            shutil.rmtree(target, ignore_errors=True)
            print("Subscription login removed.")
            return
        if args.action == "status":
            try:
                read_login(args.provider, args.credential_id)
            except (OSError, ValueError, KeyError):
                raise SystemExit("Subscription login required.") from None
            print("Subscription login stored; live readiness is checked before coding.")
            return
        home = Path("/login-work") / f"{args.provider}-{args.credential_id}"
        home.mkdir(mode=0o700, parents=True, exist_ok=False)
        mediation = None
        grant = None
        try:
            os.chown(home, 1000, 1000)
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "HTTPS_PROXY": "http://127.0.0.1:3128",
                "HTTP_PROXY": "http://127.0.0.1:3128",
                "NO_PROXY": "localhost,127.0.0.1",
            }
            controller_token = os.environ.get("VIBEARR_MEDIATION_CONTROLLER_TOKEN_FILE")
            if controller_token:
                from vibearr_runner.compact_connections import MediationClient

                mediation = MediationClient(
                    "http://127.0.0.1:8792", Path(controller_token).read_text().strip()
                )
                grant = mediation.request(
                    "/v4/logins/begin", {"provider": args.provider}
                )
                proxy = "http://grant:" + grant["token"] + "@127.0.0.1:3128"
                environment.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy)
            if args.provider == "codex":
                config = home / ".codex"
                config.mkdir(mode=0o700)
                os.chown(config, 1000, 1000)
                (config / "config.toml").write_text(
                    'forced_login_method = "chatgpt"\ncli_auth_credentials_store = "file"\n'
                )
                command = ["codex", "login", "--device-auth"]
            else:
                command = ["claude", "auth", "login", "--claudeai"]
            try:
                subprocess.run(
                    command,
                    env=environment,
                    cwd=home,
                    user=1000,
                    group=1000,
                    extra_groups=[],
                    check=True,
                )
            finally:
                terminate_children()
            save_login(args.provider, args.credential_id, collect(args.provider, home))
            print("Subscription login saved in the runner volume.")
        finally:
            try:
                if mediation is not None and grant is not None:
                    mediation.request("/v4/logins/end", {"token": grant["token"]})
            finally:
                shutil.rmtree(home)


if __name__ == "__main__":
    main()
