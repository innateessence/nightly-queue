import base64
from datetime import datetime, timezone
from datetime import timedelta
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack
from .codex_task import (
    ARGV,
    DATABASE_ENV,
    HOSTS,
    SECRET_ENV,
    auth_material,
    context_data,
    launch,
)
from .contracts import Limits, Profile, digest
from .runtime import Runtime


def session_secret(*, expires=None):
    expiry = expires or int(datetime.now(timezone.utc).timestamp()) + 3600
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "header." + claims + ".signature",
                "refresh_token": "refresh-token-not-for-tasks",
                "id_token": "id-token-0123456789",
                "account_id": "account-0123456789",
            },
        }
    )


class CodexTaskTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {DATABASE_ENV: ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = {
            "schema_version": 1,
            "adapter": "codex",
            "model": "gpt-6.1-sol",
            "prompt": "Edit the fixture and test it.",
            "auth_kind": "token",
            "account_digest": "a" * 64,
        }

    def binary(self, root, *, version="0.169.0"):
        path = root / "codex"
        path.write_text(
            "#!/usr/bin/env python3\nimport sys,time\nif '--version' in sys.argv:\n print('codex-cli "
            + version
            + '\')\nelse:\n print(\'{"type":"turn.started"}\',flush=True)\n time.sleep(60)\n'
        )
        path.chmod(0o700)
        return str(path)

    def test_session_transport_never_includes_refresh_token(self):
        selected = session_secret()
        transported = auth_material("session", selected)
        self.assertNotIn("refresh-token-not-for-tasks", transported)
        self.assertEqual(json.loads(transported)["auth_mode"], "chatgptAuthTokens")
        self.assertEqual(json.loads(transported)["tokens"]["refresh_token"], "")

    def test_same_selected_session_retries_after_cross_process_executor_restart(self):
        selected = session_secret(expires=4102444800)
        program = "import sys; from vibearr_executor.codex_task import auth_material; print(auth_material('session', sys.stdin.read()), end='')"
        materials = []
        for seed in (1, 2, 3):
            result = subprocess.run(
                [sys.executable, "-c", program],
                input=selected,
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                check=True,
                timeout=10,
            )
            materials.append(result.stdout)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            profile = Profile(
                "codex-auth-retry-v1",
                1,
                "sha256:" + "a" * 64,
                "agent",
                ARGV,
                Limits(),
                HOSTS["session"],
                SECRET_ENV,
            )
            source, context = pack({"source.txt": (0o644, b"source\n")}), b"context"
            manifest = {
                "schema_version": 1,
                "run_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_digest": digest(source),
                "context_digest": digest(context),
                "phase": "agent",
                "limits": asdict(profile.limits),
                "deadline": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
                "network_hosts": list(profile.network_hosts),
            }
            runtime = Runtime(directory, [profile])
            try:
                runtime.upload("source", source, digest(source))
                runtime.upload("context", context, digest(context))
                runtime.prepare(manifest)
                runtime.supply_secret(manifest["operation_id"], materials[0])
            finally:
                runtime.close()
            runtime = Runtime(directory, [profile])
            try:
                runtime.prepare(manifest)
                for material in materials[1:]:
                    self.assertEqual(
                        runtime.supply_secret(manifest["operation_id"], material),
                        {"stored": True},
                    )
                self.assertEqual(len(set(materials)), 1)
            finally:
                runtime.close()

    def test_expired_session_is_rejected_without_login_or_refresh(self):
        with self.assertRaises(ValueError):
            auth_material("session", session_secret(expires=1))

    def test_runtime_cannot_receive_refreshable_session(self):
        with self.assertRaises(ValueError):
            auth_material("session", session_secret(), runtime=True)

    def test_prompt_cannot_supply_cli_configuration(self):
        with self.assertRaises(ValueError):
            context_data({**self.context, "argv": ["other"]})

    def test_fixed_launch_uses_clean_home_and_only_selected_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "not-for-agent",
                    "CODEX_HOME": "/host/private",
                    "OPENAI_API_KEY": "ambient-key",
                },
            ):
                arguments, environment, prompt = launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root),
                )
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(environment["CODEX_API_KEY"], "selected-key-0123456789")
            self.assertEqual(environment["CODEX_HOME"], str(root / "home/.codex"))
            self.assertEqual(prompt.read_text(), self.context["prompt"])
            self.assertIn("--ignore-user-config", arguments)
            self.assertIn("--ignore-rules", arguments)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", arguments)

    def test_wrong_installed_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root, version="0.0.0"),
                )

    def test_only_executor_disposable_database_reaches_native_child(self):
        password = "a" * 48
        scoped = f"postgresql://task:{password}@172.20.0.2:5432/task"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    DATABASE_ENV: scoped,
                    "DATABASE_URL": "postgresql://production-db/private",
                    "POSTGRES_PASSWORD": "unrelated-host-password",
                    "GITHUB_TOKEN": "unrelated-host-token",
                    "NO_PROXY": "127.0.0.11,10.0.0.0/8,host-gateway",
                },
            ):
                _, environment, _ = launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root),
                )
            self.assertEqual(environment["DATABASE_URL"], scoped)
            self.assertEqual(environment["POSTGRES_DB"], "task")
            self.assertEqual(environment["POSTGRES_USER"], "task")
            self.assertEqual(environment["POSTGRES_PASSWORD"], password)
            self.assertEqual(environment["POSTGRES_HOST"], "172.20.0.2")
            self.assertEqual(environment["POSTGRES_PORT"], "5432")
            self.assertEqual(environment["VIBEARR_POSTGRES_PASSWORD"], password)
            self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost")
            self.assertEqual(environment["no_proxy"], "127.0.0.1,localhost")
            self.assertEqual(environment["TMPDIR"], "/work")
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("unrelated-host-password", environment.values())

    def test_ambient_database_without_executor_marker_is_not_inherited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgresql://production-db/private",
                    DATABASE_ENV: "",
                    "POSTGRES_PASSWORD": "host-password",
                },
            ):
                _, environment, _ = launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root),
                )
            self.assertNotIn("DATABASE_URL", environment)
            self.assertNotIn("POSTGRES_PASSWORD", environment)

    def test_host_database_cannot_masquerade_as_executor_disposable_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    DATABASE_ENV: "postgresql://operator:host-password@127.0.0.1:5432/production"
                },
            ):
                with self.assertRaises(ValueError):
                    launch(
                        self.context,
                        "selected-key-0123456789",
                        home=root / "home",
                        binary=self.binary(root),
                    )

    def test_session_auth_files_stay_private_under_project_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = os.umask(0o022)
            try:
                context = {**self.context, "auth_kind": "session"}
                _, _, prompt = launch(
                    context,
                    auth_material("session", session_secret()),
                    home=root / "home",
                    binary=self.binary(root),
                )
            finally:
                os.umask(previous)
            self.assertEqual((root / "home/.codex").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (root / "home/.codex/auth.json").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)

    def test_fixture_process_hang_is_terminable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments, environment, prompt = launch(
                self.context,
                "selected-key-0123456789",
                home=root / "home",
                binary=self.binary(root),
            )
            with prompt.open("rb") as source:
                process = subprocess.Popen(
                    arguments, env=environment, stdin=source, stdout=subprocess.PIPE
                )
            try:
                self.assertEqual(
                    process.stdout.readline(), b'{"type":"turn.started"}\n'
                )
                process.terminate()
                self.assertLess(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
