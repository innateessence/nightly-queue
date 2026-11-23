from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.compact_connections import (
    CompactClient,
    CompactConnections,
    ConnectionServer,
    MediationClient,
    prepare_state_directory,
)
from vibearr_runner.journal import Journal, JournalError

from .compact_connections import WorkerConnections
from .contracts import Conflict, Rejected, canonical, digest
from .managed_login import install_login
from .secret_material import redact


def login_files(account="selected-account", access="selected-access-token"):
    return {
        ".codex/auth.json": {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": access,
                "refresh_token": "selected-refresh-token",
                "id_token": "selected-identity-token",
                "account_id": account,
            },
        }
    }


class RunnerStateBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_existing_public_directory_becomes_private_for_controller_journal(self):
        state = self.root / "state"
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        with self.assertRaises(JournalError):
            Journal(state)
        prepare_state_directory(state)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(state.stat().st_uid, os.getuid())
        with Journal(state) as journal:
            self.assertIsNone(journal.load()["operation"])

    def test_state_symlink_is_rejected_without_changing_target(self):
        target = self.root / "target"
        target.mkdir()
        target.chmod(0o755)
        state = self.root / "state"
        state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Rejected):
            prepare_state_directory(state)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_state_symlink_parent_is_rejected_without_creating_child(self):
        target = self.root / "target"
        target.mkdir()
        parent = self.root / "parent"
        parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Rejected):
            prepare_state_directory(parent / "state")
        self.assertFalse((target / "state").exists())

    def test_state_regular_file_is_rejected_without_changing_mode(self):
        state = self.root / "state"
        state.write_text("existing")
        state.chmod(0o644)
        with self.assertRaises(OSError):
            prepare_state_directory(state)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o644)


class Worker:
    def __init__(self):
        self.scope = {
            "operation_id": str(uuid4()),
            "phase": "agent",
            "worker_instance": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "a" * 64,
            "source_digest": "b" * 64,
            "context_digest": "c" * 64,
        }
        self.provider, self.model = "codex", "selected-model"
        self.credential_id = 1
        self.exited = False
        self.supplied = []

    def capabilities(self):
        return {
            "instance_id": self.scope["worker_instance"],
            "worker_boot": self.scope["worker_boot"],
            "binding_digest": self.scope["binding_digest"],
        }

    def request(self, path, data=None):
        if path != f"/v4/operations/{self.scope['operation_id']}/connection-binding":
            raise Rejected("Unknown operation.")
        return {
            "scope": dict(self.scope),
            "provider": self.provider,
            "model": self.model,
            "credential_id": self.credential_id,
            "state": "exited" if self.exited else "running",
            "descendants_terminated": self.exited,
            "deadline": datetime.fromtimestamp(1100, timezone.utc).isoformat(),
        }

    def prepare(self, manifest):
        return {"state": "not-started", "operation_id": manifest["operation_id"]}

    def supply_secret(self, operation_id, secret):
        self.supplied.append((operation_id, secret))
        return {"operation_id": operation_id, "state": "not-started"}

    def status(self, _operation_id):
        return {
            "state": "exited" if self.exited else "running",
            "descendants_terminated": self.exited,
            "execution_identity": {
                "instance_id": self.scope["worker_instance"],
                "worker_boot": self.scope["worker_boot"],
            },
        }


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = Worker()
        self.clock = 1000
        self.manager = CompactConnections(
            self.root / "connections",
            self.worker,
            login_root=self.root / "logins",
            now=lambda: self.clock,
        )
        self.addCleanup(self.manager.close)
        self.files = login_files()
        path = self.root / "logins/codex/1"
        path.mkdir(mode=0o700, parents=True)
        (path / "login.json").write_bytes(canonical(self.files))
        self.connection = {
            "provider": "codex",
            "credential_id": 1,
            "login_digest": digest(canonical(self.files)),
        }

    def prepare(self):
        return self.manager.prepare(
            self.worker.scope, self.connection, model=self.worker.model, deadline=1100
        )

    def activate(self):
        envelope = self.prepare()
        result = self.manager.activate(
            self.worker.scope, envelope["grant"]["token"], model=self.worker.model
        )
        return result["token"]

    def test_only_selected_login_material_is_supplied(self):
        other = self.root / "logins/codex/2"
        other.mkdir()
        (other / "login.json").write_bytes(
            canonical(login_files("other-account", "other-access-token"))
        )
        envelope = self.prepare()
        self.assertEqual(envelope["connection"]["files"], self.files)
        self.assertNotIn("other-access-token", json.dumps(envelope))

    def test_identical_prepare_reuses_operation_grant(self):
        self.assertEqual(self.prepare(), self.prepare())

    def test_conflicting_selected_account_is_rejected(self):
        self.prepare()
        self.connection["credential_id"] = 2
        with self.assertRaises((Rejected, OSError)):
            self.prepare()

    def test_wrong_provider_is_rejected_before_login_access(self):
        self.connection["provider"] = "claude"
        with self.assertRaises(Rejected):
            self.prepare()

    def test_changed_login_digest_requires_readiness_again(self):
        self.connection["login_digest"] = "d" * 64
        with self.assertRaises(Conflict):
            self.prepare()

    def test_other_existing_account_cannot_replace_context_selection(self):
        path = self.root / "logins/codex/2"
        path.mkdir()
        path.joinpath("login.json").write_bytes(canonical(self.files))
        self.connection["credential_id"] = 2
        with self.assertRaises(Rejected):
            self.prepare()

    def test_only_one_managed_login_grant_is_active(self):
        with (self.root / "logins/execution.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            grant = self.manager.begin_login("codex")
            with self.assertRaises(Conflict):
                self.manager.begin_login("claude")
            self.manager.end_login(grant["token"])
            self.assertTrue(self.manager.begin_login("claude")["token"])

    def test_managed_login_grant_cannot_outlive_its_process_lease(self):
        with (self.root / "logins/execution.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            grant = self.manager.begin_login("codex")
            self.assertEqual(
                self.manager.grant(grant["token"], host="chatgpt.com"),
                {"provider": "codex"},
            )
        with self.assertRaises(Rejected):
            self.manager.grant(grant["token"], host="chatgpt.com")
        with self.assertRaises(Conflict):
            self.prepare()
        self.manager.end_login(grant["token"])
        self.assertTrue(self.prepare())

    def test_wrong_worker_boot_is_rejected(self):
        scope = dict(self.worker.scope, worker_boot=str(uuid4()))
        with self.assertRaises(Conflict):
            self.manager.prepare(
                scope, self.connection, model=self.worker.model, deadline=1100
            )

    def test_wrong_operation_phase_is_rejected(self):
        scope = dict(self.worker.scope, phase="setup")
        with self.assertRaises(Conflict):
            self.manager.activate(scope)

    def test_wrong_source_digest_is_rejected(self):
        scope = dict(self.worker.scope, source_digest="f" * 64)
        with self.assertRaises(Conflict):
            self.manager.prepare(
                scope, self.connection, model=self.worker.model, deadline=1100
            )

    def test_prepared_grant_cannot_use_egress_before_activation(self):
        token = self.prepare()["grant"]["token"]
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_phase_host_restrictions_apply_to_active_grant(self):
        token = self.activate()
        self.assertEqual(
            self.manager.grant(token, host="chatgpt.com")["credential_id"], 1
        )
        for host in ("pypi.org", "127.0.0.1", "169.254.169.254", "app", "worker"):
            with self.subTest(host=host), self.assertRaises(Rejected):
                self.manager.grant(token, host=host)

    def test_expired_grant_cannot_open_or_continue_egress(self):
        token = self.activate()
        self.clock = 1100
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_exited_worker_cannot_use_an_active_network_grant(self):
        token = self.activate()
        self.worker.exited = True
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_suspend_disables_and_resume_restores_same_grant(self):
        token = self.activate()
        self.manager.suspend(self.worker.scope, token)
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")
        resumed = self.manager.activate(
            self.worker.scope, token, model=self.worker.model
        )
        self.assertEqual(resumed["token"], token)
        self.assertTrue(self.manager.grant(token, host="chatgpt.com"))

    def test_setup_grant_cannot_reach_model_endpoints(self):
        self.worker.scope["phase"] = "setup"
        token = self.manager.activate(self.worker.scope)["token"]
        self.assertEqual(
            self.manager.grant(token, host="pypi.org")["provider"], "setup"
        )
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_validation_cannot_receive_public_egress(self):
        self.worker.scope["phase"] = "validation"
        with self.assertRaises(Rejected):
            self.manager.activate(self.worker.scope)

    def test_only_one_connection_phase_can_own_login_and_egress(self):
        self.activate()
        self.worker.scope["operation_id"] = str(uuid4())
        with self.assertRaises(Conflict):
            self.prepare()

    def test_completion_requires_confirmed_termination(self):
        token = self.activate()
        with self.assertRaises(Conflict):
            self.manager.finish(self.worker.scope, token)

    def test_refreshed_login_is_persisted_and_completion_is_idempotent(self):
        token = self.activate()
        refreshed = login_files(access="renewed-access-token")
        first = self.manager.finish(
            self.worker.scope, token, files=refreshed, terminated=True
        )
        self.assertEqual(
            first,
            self.manager.finish(
                self.worker.scope, token, files=refreshed, terminated=True
            ),
        )
        self.assertEqual(self.manager._login("codex", 1), refreshed)
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")
        self.assertFalse((self.root / "logins/execution-lease.json").exists())

    def test_refreshed_login_cannot_switch_account(self):
        token = self.activate()
        with self.assertRaises(Rejected):
            self.manager.finish(
                self.worker.scope,
                token,
                files=login_files("other-account"),
                terminated=True,
            )
        self.assertEqual(self.manager._login("codex", 1), self.files)

    def test_lost_finish_response_does_not_lose_or_repeat_login_refresh(self):
        token = self.activate()
        refreshed = login_files(access="renewed-access-token")
        save = self.manager._save

        def interrupted(record):
            if record["state"] == "finished":
                raise OSError("Response interrupted after credential persistence")
            save(record)

        with (
            patch.object(self.manager, "_save", side_effect=interrupted),
            self.assertRaises(OSError),
        ):
            self.manager.finish(
                self.worker.scope, token, files=refreshed, terminated=True
            )
        self.assertEqual(self.manager._login("codex", 1), refreshed)
        self.assertTrue(
            self.manager.finish(
                self.worker.scope, token, files=refreshed, terminated=True
            )["finished"]
        )

    def test_restart_preserves_pending_connection_and_login_fence(self):
        envelope = self.prepare()
        self.manager.close()
        restarted = CompactConnections(
            self.root / "connections",
            self.worker,
            login_root=self.root / "logins",
            now=lambda: self.clock,
        )
        self.addCleanup(restarted.close)
        self.assertEqual(
            restarted.prepare(
                self.worker.scope,
                self.connection,
                model=self.worker.model,
                deadline=1100,
            )["grant"],
            envelope["grant"],
        )
        with (
            (self.root / "logins/execution.lock").open("a") as contender,
            self.assertRaises(BlockingIOError),
        ):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_duplicate_manager_cannot_own_same_journal(self):
        with self.assertRaises(Conflict):
            CompactConnections(
                self.root / "connections", self.worker, login_root=self.root / "logins"
            )

    def test_reconciliation_does_not_unlock_a_running_or_unknown_operation(self):
        self.activate()
        self.manager.reconcile()
        self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_reconciliation_fences_lost_subscription_return(self):
        token = self.activate()
        self.worker.exited = True
        self.manager.reconcile()
        self.assertTrue((self.root / "logins/execution-lease.json").exists())
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_subscription_completion_requires_login_files(self):
        token = self.activate()
        with self.assertRaises(Conflict):
            self.manager.finish(self.worker.scope, token, terminated=True)
        self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_revocation_cannot_be_resumed(self):
        token = self.activate()
        self.manager.revoke(self.worker.scope, token)
        with self.assertRaises(Conflict):
            self.manager.activate(self.worker.scope, token, model=self.worker.model)
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_reconciliation_finishes_a_durably_returned_login(self):
        token = self.activate()
        self.worker.exited = True
        save = self.manager._save

        def interrupted(record):
            if record["state"] == "finished":
                raise OSError("Interrupted final receipt")
            save(record)

        with (
            patch.object(self.manager, "_save", side_effect=interrupted),
            self.assertRaises(OSError),
        ):
            self.manager.finish(
                self.worker.scope,
                token,
                files=login_files(access="renewed-access-token"),
                terminated=True,
            )
        self.manager.reconcile()
        self.assertFalse((self.root / "logins/execution-lease.json").exists())

    def test_external_ollama_only_uses_selected_endpoint_and_model(self):
        self.worker.provider = "opencode"
        self.worker.credential_id = 3
        self.connection = {
            "provider": "opencode",
            "credential_id": 3,
            "endpoint": "http://192.168.1.20:11434",
        }
        token = self.activate()
        self.assertEqual(
            self.manager.relay(token, "selected-model"),
            {"endpoint": self.connection["endpoint"], "model": "selected-model"},
        )
        with self.assertRaises(Rejected):
            self.manager.relay(token, "other-model")
        with self.assertRaises(Rejected):
            self.manager.grant(token, host="chatgpt.com")

    def test_worker_channel_cannot_read_login_store_or_prepare_connections(self):
        with ConnectionServer(
            ("127.0.0.1", 0), self.manager, "c" * 48, "w" * 48
        ) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = MediationClient(
                    "http://127.0.0.1:" + str(server.server_port), "w" * 48
                )
                with self.assertRaises(Conflict):
                    client.request(
                        "/v4/connections/status",
                        {"provider": "codex", "credential_id": 1},
                    )
                controller = MediationClient(client.origin, "c" * 48)
                self.assertEqual(
                    controller.request(
                        "/v4/connections/status",
                        {"provider": "codex", "credential_id": 1},
                    ),
                    self.manager.status("codex", 1),
                )
            finally:
                server.shutdown()
                thread.join()

    def test_task_grant_cannot_call_management_channel(self):
        token = self.activate()
        with ConnectionServer(
            ("127.0.0.1", 0), self.manager, "c" * 48, "w" * 48
        ) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = MediationClient(
                    "http://127.0.0.1:" + str(server.server_port), token
                )
                with self.assertRaises(Conflict):
                    client.request(
                        "/v4/connections/activate",
                        {
                            "scope": self.worker.scope,
                            "token": token,
                            "model": self.worker.model,
                        },
                    )
            finally:
                server.shutdown()
                thread.join()

    def test_compact_client_routes_login_reads_to_runner(self):
        client = CompactClient(
            self.worker,
            "http://127.0.0.1:8792",
            "c" * 48,
            directory=self.root / "client",
        )
        with patch.object(
            client.mediation, "request", return_value={"available": True}
        ) as mediation:
            self.assertTrue(client.request("/v3/logins/codex/1")["available"])
        mediation.assert_called_once_with(
            "/v4/connections/status", {"provider": "codex", "credential_id": 1}
        )
        self.assertTrue(client.compact)
        self.assertFalse(client.integrated)

    def test_compact_client_binds_secret_to_prepared_manifest(self):
        client = CompactClient(
            self.worker,
            "http://127.0.0.1:8792",
            "c" * 48,
            directory=self.root / "client",
        )
        manifest = {
            "operation_id": self.worker.scope["operation_id"],
            "source_digest": self.worker.scope["source_digest"],
            "context_digest": self.worker.scope["context_digest"],
            "deadline": datetime.fromtimestamp(1100, timezone.utc).isoformat(),
        }
        client.prepare(manifest)
        with patch.object(client.mediation, "request", return_value=self.prepare()):
            client.supply_secret(manifest["operation_id"], json.dumps(self.connection))
        self.assertEqual(self.worker.supplied[0][0], manifest["operation_id"])
        self.assertEqual(
            json.loads(self.worker.supplied[0][1])["connection"]["files"], self.files
        )


class WorkerConnectionTests(ConnectionTests):
    def setUp(self):
        super().setUp()
        self.runtime = self.worker
        self.runtime.artifacts = self.root / "artifacts"
        self.runtime.artifacts.mkdir()
        context = {"adapter": "codex", "model": "selected-model", "credential_id": 1}
        context_content = canonical(context)
        self.worker.scope["context_digest"] = digest(context_content)
        self.runtime.artifacts.joinpath(
            self.worker.scope["context_digest"]
        ).write_bytes(context_content)
        self.runtime.artifacts.joinpath(self.worker.scope["source_digest"]).write_bytes(
            b"source without auth"
        )
        self.runtime.connection_binding = lambda operation_id: self.worker.request(
            f"/v4/operations/{operation_id}/connection-binding"
        )
        self.runtime.path = lambda operation_id: self.root / operation_id
        self.runtime.path(self.worker.scope["operation_id"]).mkdir()
        self.runtime.home = self.root / "home"
        self.runtime.home.mkdir()
        self.record = {
            "manifest": {
                "source_digest": self.worker.scope["source_digest"],
                "operation_id": self.worker.scope["operation_id"],
                "context_digest": self.worker.scope["context_digest"],
                "deadline": datetime.fromtimestamp(1100, timezone.utc).isoformat(),
            }
        }
        self.connections = WorkerConnections(
            "http://runner:8792", "w" * 48, client=self, now=lambda: self.clock
        )
        self.connections.bind(self.runtime)

    def request(self, path, data):
        if path.endswith("/activate"):
            return self.manager.activate(
                data["scope"], data["token"], model=data["model"]
            )
        if path.endswith("/finish"):
            return self.manager.finish(
                data["scope"],
                data["token"],
                files=data.get("files"),
                terminated=data["terminated"],
            )
        if path.endswith("/revoke"):
            return self.manager.revoke(data["scope"], data["token"])
        raise Rejected("Unknown request")

    def test_worker_does_not_accept_another_boots_login_envelope(self):
        envelope = self.prepare()
        envelope["scope"]["worker_boot"] = str(uuid4())
        with self.assertRaises(Conflict):
            self.connections.validate_secret(
                self.worker.scope["operation_id"], json.dumps(envelope), self.record
            )

    def test_worker_rejects_credentials_already_in_task_source(self):
        envelope = self.prepare()
        self.runtime.artifacts.joinpath(self.worker.scope["source_digest"]).write_text(
            "selected-refresh-token"
        )
        with self.assertRaises(Rejected):
            self.connections.validate_secret(
                self.worker.scope["operation_id"], json.dumps(envelope), self.record
            )

    def test_worker_proxy_environment_contains_only_operation_grant(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        profile = type("Profile", (), {"phase": "agent"})()
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            self.assertEqual(
                self.connections.network(operation_id, profile), [("172.20.0.2", 3128)]
            )
            environment = self.connections.environment(
                operation_id, profile, {"HOME": "/home/task"}
            )
        self.assertIn("http://grant:", environment["HTTPS_PROXY"])
        self.assertNotIn("w" * 48, json.dumps(environment))
        self.assertNotIn("selected-refresh-token", json.dumps(environment))

    def test_worker_protects_original_and_refreshed_individual_tokens(self):
        operation_id = self.worker.scope["operation_id"]
        envelope = self.prepare()
        self.connections.validate_secret(
            operation_id, json.dumps(envelope), self.record
        )
        install_login(
            "codex", login_files(access="new-access-token"), self.runtime.home
        )
        material = self.connections.protected_material(canonical(envelope), self.record)
        self.assertEqual(
            redact(b"selected-access-token new-access-token", material),
            b"[REDACTED] [REDACTED]",
        )

    def test_opencode_launcher_uses_bounded_relay_header(self):
        from .native_task import command

        with patch.dict(
            "os.environ",
            {
                "VIBEARR_OLLAMA_URL": "http://172.20.0.2:3130/v1",
                "VIBEARR_EGRESS_GRANT": "bounded-operation-grant-0123456789",
            },
            clear=True,
        ):
            command("opencode", "selected-model", self.runtime.home)
        configuration = json.loads((self.runtime.home / "opencode.json").read_bytes())
        options = configuration["provider"]["ollama"]["options"]
        self.assertEqual(
            options,
            {
                "baseURL": "http://172.20.0.2:3130/v1",
                "headers": {"X-Vibearr-Grant": "bounded-operation-grant-0123456789"},
            },
        )
        self.assertNotIn("apiKey", json.dumps(configuration))

    def test_empty_secret_still_protects_setup_grant_and_database_password(self):
        operation_id = self.worker.scope["operation_id"]
        self.worker.scope["phase"] = "setup"
        profile = type("Profile", (), {"phase": "setup"})()
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            self.connections.network(operation_id, profile)
        self.record["database_url"] = (
            "postgresql://task:database-secret-password@127.0.0.1/task"
        )
        token = self.connections._state(operation_id)["grant"]["token"]
        material = self.connections.protected_material(b"", self.record)
        self.assertEqual(
            redact((token + " database-secret-password").encode(), material),
            b"[REDACTED] [REDACTED]",
        )

    def test_offline_validation_pause_and_resume_do_not_require_grants(self):
        operation_id = self.worker.scope["operation_id"]
        with patch.object(self.connections.client, "request") as request:
            self.assertIsNone(self.connections.suspend(operation_id))
            self.assertIsNone(self.connections.resume(operation_id))
        request.assert_not_called()

    def test_worker_returns_refreshed_selected_login_after_termination(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        profile = type("Profile", (), {"phase": "agent"})()
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            self.connections.network(operation_id, profile)
        refreshed = login_files(access="new-access-token")
        install_login("codex", refreshed, self.runtime.home)
        self.record["task_launch_intent"] = True
        self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(self.manager._login("codex", 1), refreshed)
        self.assertFalse(self.connections._path(operation_id).exists())

    def test_missing_refresh_ack_keeps_worker_connection_state_for_reconciliation(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        install_login("codex", self.files, self.runtime.home)
        with (
            patch.object(
                self.connections.client,
                "request",
                side_effect=Conflict("Unknown return"),
            ),
            self.assertRaises(Conflict),
        ):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertTrue(self.connections._path(operation_id).exists())

    def test_lost_refresh_response_retries_saved_login_after_home_cleanup(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        refreshed = login_files(access="refreshed-after-unknown-response")
        install_login("codex", refreshed, self.runtime.home)
        with (
            patch.object(
                self.connections.client,
                "request",
                side_effect=Conflict("Lost response"),
            ),
            self.assertRaises(Conflict),
        ):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        (self.runtime.home / ".codex/auth.json").unlink()
        self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(self.manager._login("codex", 1), refreshed)

    def test_repeated_secret_does_not_erase_pending_login_return(self):
        operation_id = self.worker.scope["operation_id"]
        envelope = json.dumps(self.prepare())
        self.connections.validate_secret(operation_id, envelope, self.record)
        install_login("codex", self.files, self.runtime.home)
        with (
            patch.object(
                self.connections.client,
                "request",
                side_effect=Conflict("Lost response"),
            ),
            self.assertRaises(Conflict),
        ):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        before = self.connections._state(operation_id)
        self.connections.validate_secret(operation_id, envelope, self.record)
        self.assertEqual(self.connections._state(operation_id), before)

    def test_missing_login_return_cannot_release_worker_fence(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        with self.assertRaises((KeyError, ValueError)):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertTrue(self.connections._path(operation_id).exists())
        self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_prepared_cancellation_returns_original_login_without_home_access(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.record["task_launch_intent"] = False
        self.connections.revoke(operation_id)
        self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(self.manager._login("codex", 1), self.files)
        self.assertFalse(self.connections._path(operation_id).exists())

    def test_prelaunch_cancellation_returns_original_login_after_grant_activation(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        profile = type("Profile", (), {"phase": "agent"})()
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            self.connections.network(operation_id, profile)
        self.record["task_launch_intent"] = False
        self.connections.revoke(operation_id)
        self.assertTrue((self.root / "logins/execution-lease.json").exists())
        self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(self.manager._login("codex", 1), self.files)
        self.assertFalse((self.root / "logins/execution-lease.json").exists())

    def test_prelaunch_lost_return_keeps_original_payload_and_account_fence(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.record["task_launch_intent"] = False
        self.connections.revoke(operation_id)
        with (
            patch.object(
                self.connections.client, "request", side_effect=Conflict("Lost return")
            ),
            self.assertRaises(Conflict),
        ):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(
            self.connections._state(operation_id)["return"]["files"], self.files
        )
        self.assertTrue((self.root / "logins/execution-lease.json").exists())
        self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertFalse((self.root / "logins/execution-lease.json").exists())

    def test_ambiguous_launch_intent_cannot_return_original_login(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.connections.revoke(operation_id)
        for launch_intent in (True, None, 0, "false"):
            with self.subTest(launch_intent=launch_intent):
                self.record["task_launch_intent"] = launch_intent
                with self.assertRaises((KeyError, ValueError)):
                    self.connections.finish(
                        operation_id, self.runtime.home, self.record
                    )
                self.assertNotIn("return", self.connections._state(operation_id))
                self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_prelaunch_return_rejects_stale_worker_scope(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.record["task_launch_intent"] = False
        self.worker.scope["worker_boot"] = str(uuid4())
        with self.assertRaises(Conflict):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertNotIn("return", self.connections._state(operation_id))
        self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_prelaunch_return_rejects_different_manifest_context(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.record["task_launch_intent"] = False
        self.record["manifest"]["context_digest"] = "d" * 64
        with self.assertRaises(Conflict):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertNotIn("return", self.connections._state(operation_id))
        self.assertTrue((self.root / "logins/execution-lease.json").exists())

    def test_prelaunch_return_rejects_different_selected_account(self):
        operation_id = self.worker.scope["operation_id"]
        self.connections.validate_secret(
            operation_id, json.dumps(self.prepare()), self.record
        )
        self.record["task_launch_intent"] = False
        state = self.connections._state(operation_id)
        state["original_files"] = login_files(account="other-account")
        self.connections._path(operation_id).write_bytes(canonical(state))
        with self.assertRaises(Rejected):
            self.connections.finish(operation_id, self.runtime.home, self.record)
        self.assertEqual(self.manager._login("codex", 1), self.files)
        self.assertTrue((self.root / "logins/execution-lease.json").exists())


class ClaudeRefreshTests(unittest.TestCase):
    setUp = ConnectionTests.setUp
    prepare = ConnectionTests.prepare
    activate = ConnectionTests.activate

    def claude(
        self, access="claude-selected-access", account="claude-selected-account"
    ):
        return {
            ".claude/.credentials.json": {
                "claudeAiOauth": {
                    "accessToken": access,
                    "refreshToken": "claude-selected-refresh",
                }
            },
            ".claude.json": {
                "hasCompletedOnboarding": True,
                "oauthAccount": {
                    "accountUuid": account,
                    "organizationUuid": "selected-organization",
                },
            },
        }

    def select(self, files):
        self.worker.provider = "claude"
        path = self.root / "logins/claude/1"
        path.mkdir(parents=True)
        path.joinpath("login.json").write_bytes(canonical(files))
        self.connection = {
            "provider": "claude",
            "credential_id": 1,
            "login_digest": digest(canonical(files)),
        }
        return self.activate()

    def test_claude_refresh_preserves_selected_account(self):
        token = self.select(self.claude())
        refreshed = self.claude(access="claude-renewed-access")
        self.manager.finish(self.worker.scope, token, files=refreshed, terminated=True)
        self.assertEqual(self.manager._login("claude", 1), refreshed)

    def test_claude_refresh_rejects_another_account(self):
        token = self.select(self.claude())
        with self.assertRaises(Rejected):
            self.manager.finish(
                self.worker.scope,
                token,
                files=self.claude(account="other-account"),
                terminated=True,
            )

    def test_legacy_claude_refresh_requires_account_reconciliation(self):
        files = self.claude()
        files.pop(".claude.json")
        token = self.select(files)
        refreshed = self.claude(access="renewed-access")
        refreshed.pop(".claude.json")
        with self.assertRaises(Rejected):
            self.manager.finish(
                self.worker.scope, token, files=refreshed, terminated=True
            )
        self.assertTrue(
            self.manager.finish(self.worker.scope, token, files=files, terminated=True)[
                "finished"
            ]
        )

    def test_claude_collection_retains_only_account_identity(self):
        from .managed_login import collect

        files = self.claude()
        files[".claude.json"].update(
            hooks={"untrusted": "command"}, enabledPlugins=["untrusted"]
        )
        files[".claude.json"]["oauthAccount"]["emailAddress"] = "private@example.com"
        home = self.root / "claude-home"
        home.mkdir()
        install_login("claude", files, home)
        self.assertEqual(collect("claude", home), self.claude())
