"""One Docker service owns the controller and sequential task phases."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from types import SimpleNamespace

from .client import Client
from .contracts import Conflict, Rejected, canonical, digest
from .integrated_profiles import MODEL_HOSTS, RUNTIME, profiles
from .local_database import LocalDatabase
from .managed_login import ROOT as LOGIN_ROOT, collect, read_login, save_login
from .ollama_relay import Handler as OllamaHandler, Relay, external_endpoint
from .proxy import Handler as ProxyHandler, Proxy
from .runtime import durable
from .server import Handler
from .slot_http import SlotServer
from .slot_network import configure
from .slots import SlotRuntime


class LocalClient:
    def __init__(self, runtime):
        self.runtime = runtime

    def capabilities(self):
        return self.runtime.capabilities()

    def request(self, path, data=None):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(runtime=self.runtime)
        handler.command = "POST"
        handler.path = path
        return handler.route(data or {})


class DependencyGateway:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory, self.artifacts = runtime.directory, runtime.artifacts
        self.local = LocalClient(runtime)
        self.clients = {phase: self.local for phase in ("agent", "setup", "validation")}

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def client(self, _operation_id):
        return self.local


class IntegratedRuntime(SlotRuntime):
    def __init__(self, directory, installed, *, proxy, relay, database=None):
        self.proxy, self.relay = proxy, relay
        self.execution_lock = None
        self.login_binding = None
        self.pending_operation = ""
        super().__init__(
            directory, installed, configure_network=self.network, database=database
        )
        from .slot_builds import DependencyBuilds

        self.dependency_builds = DependencyBuilds(DependencyGateway(self))

    def execution_identity(self):
        from .slot_safety import binding

        value = binding(self)
        value["runtime"] = RUNTIME
        return {
            "runtime": RUNTIME,
            "protocol_version": 2,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": digest(canonical(value)),
        }

    def capabilities(self):
        identity = self.execution_identity()
        path = self.directory / "integrated-qualification.json"
        qualification = json.loads(path.read_bytes()) if path.exists() else {}
        qualified = (
            qualification.get("binding_digest") == identity["binding_digest"]
            and qualification.get("passed") is True
            and not self.qualifying
        )
        return {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": 1,
            "runtime": RUNTIME,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": identity["binding_digest"],
            "qualified": qualified,
            "accepting_work": qualified and not self.active and not self.fenced,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in self.profiles.values()
            ],
            "reason": self.fenced
            or (
                "Integrated runner boot qualified."
                if qualified
                else "Qualify this runner boot before enabling coding."
            ),
        }

    def supply_secret(self, operation_id, secret):
        connection = json.loads(secret)
        context = json.loads(
            (
                self.artifacts / self.record(operation_id)["manifest"]["context_digest"]
            ).read_bytes()
        )
        if connection.get("provider") != context.get("adapter") or set(connection) - {
            "provider",
            "credential_id",
            "endpoint",
            "login_digest",
        }:
            raise Rejected("Connection differs from the bound native task.")
        provider = connection["provider"]
        if provider == "opencode":
            connection["endpoint"] = external_endpoint(connection["endpoint"])
        else:
            connection["files"] = read_login(provider, connection["credential_id"])
            if connection.get("login_digest") != digest(canonical(connection["files"])):
                raise Conflict(
                    "Subscription login changed during preparation; repeat readiness."
                )
            from .secret_material import contains_secret

            manifest = self.record(operation_id)["manifest"]
            protected = canonical(connection)
            if any(
                contains_secret(
                    (self.artifacts / manifest[key]).read_bytes(), protected
                )
                for key in ("source_digest", "context_digest")
            ):
                raise Rejected(
                    "Task source or context contains selected subscription credentials."
                )
        return super().supply_secret(operation_id, json.dumps(connection))

    def network(self, profile):
        self.relay.endpoint = ""
        self.relay.model = ""
        self.login_binding = None
        if profile.phase == "setup":
            from .validation_profile import PACKAGE_HOSTS

            self.proxy.hosts = set(PACKAGE_HOSTS)
        elif profile.phase == "agent" and profile.backend_secret_env:
            connection = json.loads(
                (self.path(self.pending_operation) / "backend-secret").read_bytes()
            )
            provider = connection["provider"]
            if provider == "opencode":
                self.proxy.hosts = set()
                self.relay.endpoint = external_endpoint(connection["endpoint"])
                context = json.loads(
                    (
                        self.artifacts
                        / self.record(self.pending_operation)["manifest"][
                            "context_digest"
                        ]
                    ).read_bytes()
                )
                self.relay.model = context["model"]
            else:
                self.proxy.hosts = (
                    {"chatgpt.com", "auth.openai.com"}
                    if provider == "codex"
                    else {"api.anthropic.com", "claude.ai", "platform.claude.com"}
                )
                self.login_binding = (provider, connection["credential_id"])
        else:
            self.proxy.hosts = set()
        configure([("127.0.0.1", 3128)], blocked_ports="8791,9001")
        return [("127.0.0.1", 3128)] if self.proxy.hosts else []

    def start(self, operation_id):
        with self.lock:
            if self.record(operation_id)["state"] != "prepared":
                return super().start(operation_id)
            if self.execution_lock is not None:
                raise Conflict("Another task phase owns this runner.")
            lock = (LOGIN_ROOT / "execution.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise Conflict(
                    "A subscription login operation owns the runner."
                ) from None
            self.execution_lock = lock
            self.pending_operation = operation_id
            try:
                return super().start(operation_id)
            except Exception:
                lock.close()
                self.execution_lock = None
                raise

    def reset(self):
        if self.login_binding and self.active:
            from .worker import terminate_children

            terminate_children()
            provider, credential_id = self.login_binding
            path = self.home / (
                ".codex/auth.json"
                if provider == "codex"
                else ".claude/.credentials.json"
            )
            if path.exists():
                save_login(provider, credential_id, collect(provider, self.home))
        super().reset()
        self.login_binding = None
        self.relay.endpoint = ""
        self.proxy.hosts = set(MODEL_HOSTS)

    def protected_material(self, secret, record):
        original = super().protected_material(secret, record)
        if self.login_binding is None:
            return original
        try:
            refreshed = collect(self.login_binding[0], self.home)
        except (OSError, ValueError, KeyError):
            return original
        return canonical({"secret": original.decode(), "refreshed": refreshed})

    def run(self, *args):
        try:
            super().run(*args)
        finally:
            if self.execution_lock is not None:
                self.execution_lock.close()
                self.execution_lock = None


class IntegratedHandler(Handler):
    def route(self, data):
        if self.command == "GET" and self.path.startswith("/v3/logins/") and not data:
            parts = self.path.split("/")
            if len(parts) != 5:
                raise Rejected("Choose a provider and credential ID.")
            try:
                files = read_login(parts[3], int(parts[4]))
            except (OSError, ValueError, KeyError):
                return {"available": False}
            return {"available": True, "login_digest": digest(canonical(files))}
        if self.command == "POST" and self.path == "/v3/qualify" and not data:
            from .integrated_qualification import qualify

            return qualify(self.server.runtime)
        if self.command == "POST" and self.path == "/v3/restart-witness" and not data:
            from .slot_qualification import prepare_restart, restart_witness

            runtime = self.server.runtime
            if runtime.active:
                return restart_witness(runtime)
            if not runtime.qualification_lock.acquire(blocking=False):
                raise Conflict("Another qualification owns the runner.")
            try:
                return prepare_restart(runtime)
            finally:
                runtime.qualification_lock.release()
        return super().route(data)


def identity():
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        nargs="?",
        choices=["serve", "qualify", "restart-witness"],
        default="serve",
    )
    args = parser.parse_args()
    token_path = Path("/run/vibearr/runner/control-token")
    if args.action != "serve":
        client = Client("http://127.0.0.1:8791", token_path.read_text().strip())
        print(json.dumps(client.request("/v3/" + args.action, {})))
        return
    os.umask(0o077)
    image = os.environ.get("VIBEARR_RUNNER_IMAGE_ID", "")
    requirements = Path("/opt/validation/project-requirements.txt").read_bytes()
    installed, validation = profiles(image, hashlib.sha256(requirements).hexdigest())
    state = Path("/var/lib/vibearr")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    os.chown(state, 10001, 10001)
    LOGIN_ROOT.mkdir(mode=0o700, exist_ok=True)
    LOGIN_ROOT.chmod(0o700)
    control = Path("/control")
    control.mkdir(mode=0o700, exist_ok=True)
    stop = threading.Event()
    with (
        Proxy(("127.0.0.1", 3128), ProxyHandler) as proxy,
        Relay(("127.0.0.1", 3130), OllamaHandler) as relay,
    ):
        proxy.hosts, proxy.denied = set(MODEL_HOSTS), set()
        configure([("127.0.0.1", 3128)])
        database = LocalDatabase()
        database.reset("")
        runtime = IntegratedRuntime(
            control, installed, proxy=proxy, relay=relay, database=database
        )
        private = Path("/run/vibearr/runner")
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(private, 10001, 10001)
        if not token_path.exists():
            import secrets

            durable(token_path, secrets.token_urlsafe(48).encode())
            os.chown(token_path, 10001, 10001)
        profile_path = private / "validation-profile.json"
        durable(profile_path, canonical(validation.data()))
        os.chown(profile_path, 10001, 10001)
        with SlotServer(("127.0.0.1", 8791), IntegratedHandler) as server:
            server.runtime, server.token = runtime, token_path.read_text().strip()
            for service in (server, proxy, relay):
                threading.Thread(target=service.serve_forever, daemon=True).start()
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(state),
                "VIBEARR_URL": os.environ.get("VIBEARR_URL", "http://app:8000"),
                "VIBEARR_RUNNER_STATE_DIR": str(state),
                "VIBEARR_RUNNER_API_TOKEN_FILE": str(private / "api-token"),
                "VIBEARR_EXECUTOR_URL": "http://127.0.0.1:8791",
                "VIBEARR_EXECUTOR_TOKEN_FILE": str(token_path),
                "VIBEARR_RUNTIME_PROTOCOL": RUNTIME,
                "VIBEARR_VALIDATION_PROFILE_FILE": str(profile_path),
            }
            controller = subprocess.Popen(
                ["python", "-I", "-m", "vibearr_runner"],
                env=environment,
                cwd="/opt/executor",
                preexec_fn=identity,
                start_new_session=True,
            )
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            try:
                while not stop.wait(0.5):
                    if controller.poll() is not None:
                        raise RuntimeError(
                            "Controller exited; restarting the runner is required."
                        )
            finally:
                runtime.close()
                database.reset("")
                if controller.poll() is None:
                    os.killpg(controller.pid, signal.SIGTERM)
                    try:
                        controller.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(controller.pid, signal.SIGKILL)
                        controller.wait()
                for service in (server, proxy, relay):
                    service.shutdown()


if __name__ == "__main__":
    main()
