"""Authenticated execution in a container separate from trusted runner state."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import threading

from .client import Client
from .compact_profiles import (
    REQUIRED_BOUNDARY_CHECKS,
    ROLES,
    RUNTIME,
    profiles,
    source_access,
)
from .contracts import Conflict, Manifest, Rejected, canonical, digest, identifier
from .control_token import read_control_token
from .integrated import DependencyGateway
from .local_database import LocalDatabase
from .runtime import durable
from .server import Handler
from .slot_http import SlotServer
from .slot_network import configure
from .slots import SlotRuntime


MAX_BROWSER = 16 * 1024 * 1024
TASK_UIDS = (1000, 10003)


class CompactWorkerRuntime(SlotRuntime):
    task_uids = TASK_UIDS

    def __init__(
        self,
        directory,
        installed,
        *,
        connections=None,
        services=None,
        validation_profile=None,
        **kwargs,
    ):
        self.connections = connections
        self.services = services
        self.pending_operation = ""
        self.source_modes = {}
        self.validation_profile = validation_profile
        if connections is not None:
            connections.bind(self)
        if services is not None and hasattr(services, "bind"):
            services.bind(self)
        super().__init__(directory, installed, configure_network=self.network, **kwargs)
        from .slot_builds import DependencyBuilds

        self.dependency_builds = DependencyBuilds(DependencyGateway(self))
        if self.validation_profile is not None:
            durable(
                self.directory / "validation-profile.json",
                canonical(self.validation_profile),
            )

    def boundary(self):
        from .slot_safety import binding

        value = binding(self)
        value["runtime"] = RUNTIME
        if self.database is not None and hasattr(self.database, "provenance"):
            value["local_database"] = self.database.provenance()
        return value

    def execution_identity(self):
        return {
            "runtime": RUNTIME,
            "protocol_version": 2,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": digest(canonical(self.boundary())),
        }

    def capabilities(self):
        identity = self.execution_identity()
        path = self.directory / "compact-qualification.json"
        report = json.loads(path.read_bytes()) if path.exists() else {}
        qualified = (
            report.get("schema_version") == 1
            and report.get("runtime") == RUNTIME
            and report.get("binding_digest") == identity["binding_digest"]
            and report.get("passed") is True
            and report.get("worker_boot") == self.boot
            and report.get("checks") == sorted(REQUIRED_BOUNDARY_CHECKS)
            and bool(report.get("restart_receipt"))
            and bool(report.get("receipts"))
            and not self.qualifying
        )
        roles = {}
        browser = None
        if qualified:
            from .compact_role_qualification import qualified_roles

            roles = qualified_roles(self, identity)
            browser = roles.pop("browser", None)
        return {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": 1,
            **identity,
            "qualified": qualified,
            "accepting_work": qualified and not self.active and not self.fenced,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in self.profiles.values()
            ],
            "factory_roles": roles,
            "factory_browser": browser,
            "validation_profile": self.validation_profile,
            "image_digest": next(iter(self.profiles.values())).image,
            "qualification_digest": digest(canonical(report)) if qualified else "",
            "tool_provenance": self.boundary().get("local_database", {}),
            "reason": self.fenced
            or (
                "Compact worker boot qualified."
                if qualified
                else "Qualify this compact worker boot."
            ),
        }

    def context(self, record):
        document = json.loads(
            (self.artifacts / record["manifest"]["context_digest"]).read_bytes()
        )
        if not isinstance(document, dict):
            raise Rejected("Worker context must be an object.")
        return document

    def prepare(self, data):
        operation_id = identifier(data.get("operation_id"))
        if "manifest" in self.record(operation_id):
            return super().prepare(data)
        profile = self.profile_for(data)
        if profile is not None and profile.profile_id.startswith("factory-"):
            Manifest.parse(data, profile)
            document = json.loads(
                (self.artifacts / data["context_digest"]).read_bytes()
            )
            role = profile.profile_id.removeprefix("factory-")
            probe = (
                self.qualifying
                and operation_id == self.probe_operation
                and profile is self.probe_profile
            )
            if (
                not isinstance(document, dict)
                or not probe
                and document.get("factory_role") != role
            ):
                raise Rejected("Factory role differs from its registered profile.")
        return super().prepare(data)

    def supply_secret(self, operation_id, secret):
        if self.qualifying and operation_id == self.probe_operation:
            return super().supply_secret(operation_id, secret)
        if self.connections is None:
            raise Rejected("Runner connection mediation is not configured.")
        record = self.record(operation_id)
        validated = self.connections.validate_secret(operation_id, secret, record)
        return super().supply_secret(operation_id, validated)

    def connection_binding(self, operation_id):
        identifier(operation_id)
        record = self.record(operation_id)
        if "manifest" not in record or record.get("worker_boot") != self.boot:
            raise Conflict("Connection operation does not belong to this worker boot.")
        manifest = record["manifest"]
        profile = self.profile_for(manifest)
        identity = self.execution_identity()
        if profile is None or record.get("execution_identity") != identity:
            raise Conflict("Connection profile or boundary changed.")
        context = self.context(record)
        return {
            "scope": {
                "operation_id": operation_id,
                "phase": profile.phase,
                "worker_instance": self.instance,
                "worker_boot": self.boot,
                "binding_digest": identity["binding_digest"],
                "source_digest": manifest["source_digest"],
                "context_digest": manifest["context_digest"],
            },
            "provider": context.get("adapter", ""),
            "model": context.get("model", ""),
            "credential_id": context.get("credential_id"),
            "deadline": manifest["deadline"],
            "state": self.public(record)["state"],
            "descendants_terminated": record.get("descendants_terminated", False),
        }

    def network(self, profile):
        if (
            profile.phase == "validation"
            or not profile.network_hosts
            or self.qualifying
            and profile is self.probe_profile
        ):
            configure([], blocked_ports="8791,8792,9001", task_uids=TASK_UIDS)
            return []
        if self.connections is None:
            raise Rejected("Runner connection mediation is not configured.")
        destinations = self.connections.network(self.pending_operation, profile)
        configure(destinations, blocked_ports="8791,8792,9001", task_uids=TASK_UIDS)
        return destinations

    def start(self, operation_id):
        with self.lock:
            if self.record(operation_id)["state"] != "prepared":
                return super().start(operation_id)
            if self.active or self.fenced:
                raise Conflict("Worker phase is active or fenced.")
            self.pending_operation = operation_id
            self.cancel.clear()
            try:
                return super().start(operation_id)
            except Exception:
                try:
                    try:
                        self.end_phase(operation_id)
                    finally:
                        try:
                            if self.database is not None:
                                self.database.reset(operation_id)
                        finally:
                            self.reset()
                except Exception:
                    self.fenced = (
                        "Phase preparation cleanup is unknown; worker remains fenced."
                    )
                raise

    def prepare_source(self, profile, record):
        self.source_modes = {}
        if source_access(profile.profile_id) != "read_only":
            return
        for path in [self.work, *sorted(self.work.rglob("*"))]:
            if path.is_symlink():
                raise Rejected("Read-only source cannot contain links.")
            self.source_modes[str(path)] = stat.S_IMODE(path.stat().st_mode)
            os.chown(path, 0, 0)
            path.chmod(0o555 if path.is_dir() else path.stat().st_mode & 0o555)

    def finish_source(self, profile, record):
        for name, mode in self.source_modes.items():
            path = Path(name)
            if not path.exists() or path.is_symlink():
                raise Rejected("Read-only candidate changed during its role.")
            path.chmod(mode)
        self.source_modes = {}

    def task_environment(self, operation_id, profile, environment):
        environment = dict(environment)
        if profile.profile_id.startswith("factory-"):
            role = profile.profile_id.removeprefix("factory-")
            if role not in ROLES:
                raise Rejected("Unknown registered factory role.")
            environment["VIBEARR_FACTORY_ROLE"] = role
        if self.connections is not None and not (
            self.qualifying and operation_id == self.probe_operation
        ):
            environment = self.connections.environment(
                operation_id, profile, environment
            )
        if self.services is not None:
            record = self.record(operation_id)
            self.services.before_phase(
                operation_id,
                profile,
                {
                    "document": self.context(record),
                    "environment": environment,
                    "manifest": record["manifest"],
                    "execution_identity": self.execution_identity(),
                },
            )
        return environment

    def end_phase(self, operation_id):
        try:
            if self.services is not None:
                self.services.after_phase(operation_id)
        finally:
            if self.connections is not None and not (
                self.qualifying and operation_id == self.probe_operation
            ):
                self.connections.finish(
                    operation_id, self.home, self.record(operation_id)
                )

    def protected_material(self, secret, record):
        if self.connections is not None and not (
            self.qualifying and record["operation_id"] == self.probe_operation
        ):
            return self.connections.protected_material(secret, record)
        return super().protected_material(secret, record)

    def reset(self):
        super().reset()
        self.source_modes = {}

    def stop(self, operation_id):
        with self.lock:
            prepared = self.record(operation_id)["state"] == "prepared"
        if self.connections is not None:
            try:
                self.connections.revoke(operation_id)
            except Exception:
                self.fenced = "Connection revocation is unknown; worker remains fenced."
        result = super().stop(operation_id)
        if prepared and self.connections is not None:
            try:
                self.connections.finish(
                    operation_id, self.home, self.record(operation_id)
                )
            except Exception:
                self.fenced = "Connection return is unknown; worker remains fenced."
        return result

    def pause(self, operation_id):
        result = super().pause(operation_id)
        if self.connections is not None:
            try:
                self.connections.suspend(operation_id)
            except Exception:
                self.fenced = "Connection suspension is unknown; worker remains fenced."
                raise
        return result

    def resume(self, operation_id):
        with self.lock:
            record = self.record(operation_id)
            if operation_id != self.active or not record.get("paused") or self.fenced:
                raise Conflict("Operation cannot resume in this worker boot.")
        if self.connections is not None:
            self.connections.resume(operation_id)
        return super().resume(operation_id)

    def browser_result(self, operation_id):
        identifier(operation_id)
        record = self.record(operation_id)
        result = record.get("result", {})
        if (
            self.services is None
            or record.get("manifest", {}).get("profile_id") != "browser-ui"
            or record.get("state") != "exited"
            or record.get("descendants_terminated") is not True
            or result.get("exit_code") != 0
            or result.get("terminated") is not True
            or result.get("timed_out") is not False
            or result.get("canceled") is not False
            or result.get("export_error")
        ):
            raise Conflict(
                "Browser output requires a confirmed successful terminal phase."
            )
        report, content = self.services.export(operation_id)
        if len(content) > MAX_BROWSER:
            raise Rejected("Browser output exceeds its byte limit.")
        return {
            "operation_id": operation_id,
            "execution_identity": record["execution_identity"],
            "report": report,
            "sha256": digest(content),
            "bytes": len(content),
            "content": base64.b64encode(content).decode(),
        }


class CompactHandler(Handler):
    def route(self, data):
        runtime = self.server.runtime
        if self.command == "POST":
            from .compact_protocol import PREFIX, recovery_request

            if self.path.startswith(PREFIX + "/"):
                if runtime.execution_identity().get("runtime") != RUNTIME:
                    raise Rejected(
                        "Compact writes require the registered worker boundary."
                    )
                self.path = self.path[len(PREFIX) :]
            elif not recovery_request(self.path):
                raise Rejected("Compact writes require the versioned worker route.")
        if self.command == "GET" and self.path == "/v4/qualification" and not data:
            path = runtime.directory / "compact-qualification.json"
            report = json.loads(path.read_bytes()) if path.exists() else {}
            return {"report": report, "sha256": digest(canonical(report))}
        if self.command == "GET" and not data:
            parts = self.path.split("/")
            if (
                len(parts) == 5
                and parts[:3] == ["", "v4", "operations"]
                and parts[4] == "connection-binding"
            ):
                return runtime.connection_binding(parts[3])
        if self.command == "POST" and self.path == "/v4/qualify":
            from .compact_protocol import qualification_targets

            if set(data) != {"targets"}:
                raise Rejected("Supply only the inspected qualification targets.")
            targets = qualification_targets(data["targets"])
            from .compact_qualification import qualify

            return qualify(runtime, targets=targets)
        if self.command == "POST" and not data:
            if self.path == "/v4/qualify-roles":
                from .compact_role_qualification import qualify_roles

                return qualify_roles(runtime)
            if self.path == "/v4/restart-witness":
                from .slot_qualification import prepare_restart, restart_witness

                if runtime.active:
                    return restart_witness(runtime)
                if not runtime.qualification_lock.acquire(blocking=False):
                    raise Conflict("Another qualification owns the worker.")
                try:
                    return prepare_restart(runtime)
                finally:
                    runtime.qualification_lock.release()
            parts = self.path.split("/")
            if (
                len(parts) == 5
                and parts[:3] == ["", "v4", "browser"]
                and parts[4] == "result"
            ):
                return runtime.browser_result(parts[3])
        return super().route(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        nargs="?",
        choices=["serve", "health", "qualify", "qualify-roles", "restart-witness"],
        default="serve",
    )
    args = parser.parse_args()
    os.umask(0o077)
    token_path = Path(
        os.environ.get(
            "VIBEARR_EXECUTOR_TOKEN_FILE", "/run/vibearr/worker/control-token"
        )
    )
    token = read_control_token(token_path)
    if (token_path.parent / "restore-required.json").exists():
        raise Rejected(
            "Restored worker authority must be retired and reprovisioned before startup."
        )
    if args.action != "serve":
        client = Client(
            "http://127.0.0.1:8791", token, timeout=3 if args.action == "health" else 90
        )
        if args.action == "health":
            observed = client.capabilities()
            if observed.get("runtime") != RUNTIME or observed.get(
                "image_digest"
            ) != os.environ.get("VIBEARR_WORKER_IMAGE_ID"):
                raise Rejected("Worker health identity differs from installed image.")
            return
        data = {}
        if args.action == "qualify":
            from .compact_protocol import qualification_targets

            targets = os.environ.get("VIBEARR_QUALIFICATION_TARGETS", "")
            if len(targets.encode()) > 512:
                raise Rejected("Qualification targets exceed their byte limit.")
            try:
                decoded = json.loads(targets)
            except ValueError:
                raise Rejected(
                    "Supply inspected qualification targets as JSON."
                ) from None
            data = {"targets": qualification_targets(decoded)}
        print(json.dumps(client.request("/v4/" + args.action, data)))
        return
    requirements = Path("/opt/validation/project-requirements.txt").read_bytes()
    installed, validation = profiles(
        os.environ["VIBEARR_WORKER_IMAGE_ID"], hashlib.sha256(requirements).hexdigest()
    )
    database = LocalDatabase()
    services = None
    try:
        from .local_services import LocalServices, browser_profile
    except ModuleNotFoundError as error:
        if error.name != "vibearr_executor.local_services":
            raise
    else:
        services = LocalServices(database=database)
        installed.append(
            browser_profile(os.environ["VIBEARR_WORKER_IMAGE_ID"], installed[0].limits)
        )
    connections = None
    if os.environ.get("VIBEARR_MEDIATION_URL"):
        from .compact_connections import WorkerConnections

        secret = read_control_token(Path(os.environ["VIBEARR_MEDIATION_TOKEN_FILE"]))
        connections = WorkerConnections(os.environ["VIBEARR_MEDIATION_URL"], secret)
    configure([], blocked_ports="8791,8792,9001", task_uids=TASK_UIDS)
    database.reset("")
    runtime = CompactWorkerRuntime(
        Path(os.environ.get("VIBEARR_WORKER_CONTROL_DIR", "/control")),
        installed,
        connections=connections,
        services=services,
        validation_profile=validation.data(),
        database=database,
    )
    stopping = threading.Event()
    with SlotServer(("0.0.0.0", 8791), CompactHandler) as server:
        server.runtime, server.token = runtime, token
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.set())
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            runtime.close()
            database.reset("")


if __name__ == "__main__":
    main()
