"""Automated host qualification. No coding model calls, queue claims, or human approval."""

import argparse
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from .artifacts import import_result, pack
from .client import Client, ExecutorUnavailable
from .contracts import Limits, Profile, canonical, digest
from .proxy import resolve_public
from .qualification import REQUIRED_CHECKS, binding
from .runtime import Runtime, durable


def smoke_profiles(image: str, database_image: str = "") -> list[Profile]:
    profiles = []
    for kind, phase in (
        ("agent", "agent"),
        ("leak", "agent"),
        ("setup", "setup"),
        ("validation", "validation"),
        ("limits", "validation"),
        ("sleep", "agent"),
        ("pid-hold", "agent"),
    ):
        profile = Profile(
            f"smoke-{kind}-v1",
            1,
            image,
            phase,
            ("python", "-I", "/opt/executor/vibearr_executor/smoke.py", kind),
            Limits(
                memory_mb=256 if kind in {"limits", "pid-hold"} else 512,
                pids=32 if kind in {"limits", "pid-hold"} else 64,
            ),
            (
                ("api.openai.com",)
                if kind == "agent"
                else (("pypi.org", "files.pythonhosted.org") if kind == "setup" else ())
            ),
            "VIBEARR_SMOKE_KEY" if kind in {"agent", "leak"} else "",
        )
        profiles.append(Profile.parse(profile.data()))
    if database_image:
        profiles.append(
            Profile(
                "smoke-database-v1",
                1,
                image,
                "validation",
                ("python", "-I", "/opt/executor/vibearr_executor/smoke.py", "database"),
                Limits(),
                database_image=database_image,
            )
        )
    return profiles


def require(value, message):
    if not value:
        raise RuntimeError(message)


class Harness:
    def __init__(self, state: Path, profiles: list[Profile]):
        self.state, self.profiles = state, profiles
        self.process: subprocess.Popen | None = None
        self.server_log = None
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token = secrets.token_hex(32)
        durable(state / "token", self.token.encode())
        durable(
            state / "profiles.json", canonical([profile.data() for profile in profiles])
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.client = Client(f"http://127.0.0.1:{self.port}", self.token)

    def start(self):
        self.server_log = (self.state / "server.log").open("ab")
        command = [
            sys.executable,
            "-m",
            "vibearr_executor.server",
            "--state",
            str(self.state),
            "--profiles",
            str(self.state / "profiles.json"),
            "--token-file",
            str(self.state / "token"),
            "--port",
            str(self.port),
        ]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        self.process = subprocess.Popen(
            command, env=environment, stdout=self.server_log, stderr=self.server_log
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    "Executor failed to start; inspect "
                    + str(self.state / "server.log")
                )
            try:
                self.client.capabilities()
                return
            except ExecutorUnavailable:
                time.sleep(0.2)
        raise RuntimeError("Executor API did not become available.")

    def shutdown(self):
        if self.process is not None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=70)
            self.process = None
        if self.server_log is not None:
            self.server_log.close()
            self.server_log = None

    def prepare(
        self,
        kind: str,
        source: bytes,
        context: bytes = b"Trusted fixed runtime qualification; no production access.",
        run_id: str | None = None,
    ) -> dict:
        profile = next(
            profile
            for profile in self.profiles
            if profile.profile_id == f"smoke-{kind}-v1"
        )
        self.client.upload("source", source)
        self.client.upload("context", context)
        manifest = {
            "schema_version": 1,
            "run_id": run_id or str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(source),
            "context_digest": digest(context),
            "phase": profile.phase,
            "limits": asdict(profile.limits),
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "network_hosts": list(profile.network_hosts),
        }
        self.client.prepare(manifest)
        return manifest

    def finished(self, operation_id: str, timeout: int = 180) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.client.status(operation_id)
            if result["state"] == "exited":
                return result
            require(result["state"] != "unknown", "Container outcome became unknown")
            time.sleep(0.5)
        raise RuntimeError("Qualification task did not terminate in time.")


def qualify_one(state: Path, profiles: list[Profile]) -> dict:
    harness = Harness(state, profiles)
    source = pack(
        {
            "original.txt": (0o644, b"original\n"),
            "delete.txt": (0o644, b"delete\n"),
            **{
                name: (0o644, b"raise RuntimeError('UNTRUSTED_SHADOW_EXECUTED')\n")
                for name in (
                    "vibearr_executor/__init__.py",
                    "vibearr_executor/worker.py",
                    "sitecustomize.py",
                    "pathlib.py",
                    "json.py",
                )
            },
        }
    )
    operations: list[str] = []
    observations: dict = {}
    harness.start()
    initial_binding = harness.client.capabilities()["binding_digest"]
    try:
        try:
            urllib.request.urlopen(harness.client.url + "/v1/capabilities", timeout=5)
        except urllib.error.HTTPError as exc:
            require(exc.code == 401, "Unauthenticated control was not rejected")
        else:
            raise RuntimeError("Unauthenticated executor access succeeded")
        for kind in ("setup", "agent", "validation", "limits"):
            manifest = harness.prepare(kind, source)
            operation = manifest["operation_id"]
            operations.append(operation)
            if kind == "agent":
                secret = "qualification-only-" + secrets.token_hex(24)
                harness.client.supply_secret(operation, secret)
            else:
                try:
                    harness.client.supply_secret(
                        operation, "qualification-forbidden-secret"
                    )
                except ExecutorUnavailable:
                    pass
                else:
                    raise RuntimeError(
                        "Credential-free phase accepted a backend credential"
                    )
            harness.client.start(operation)
            if kind == "agent":
                require(
                    (state / "operations" / operation / "input" / "secret").read_bytes()
                    == b"",
                    "Host credential material survived agent launch",
                )
            harness.client.start(operation)
            finished = harness.finished(operation)
            logs = harness.client.logs(operation).decode(errors="replace")
            require(
                "UNTRUSTED_SHADOW_EXECUTED" not in logs,
                "Candidate shadow module executed in a fixed trusted recipe",
            )
            require(
                finished.get("descendants_terminated") is True,
                "Termination is unconfirmed",
            )
            deliberate_oom = kind == "limits" and finished["result"]["exit_code"] == -9
            if kind == "limits":
                counters = finished["result"].get("resources", {})
                require(
                    counters.get("memory.events", {}).get("oom_kill", 0) > 0,
                    "No protected cgroup OOM evidence",
                )
                require(
                    counters.get("pids.events", {}).get("max", 0) > 0,
                    "No protected cgroup PID enforcement evidence",
                )
                require(
                    counters.get("cpu.stat", {}).get("nr_throttled", 0) > 0,
                    "No protected cgroup CPU throttling evidence",
                )
                require(
                    "workspace-pids-cpu-enforced" in logs
                    and "memory-exhaustion-probe-started" in logs,
                    "Resource recipe did not finish its checks before deliberate OOM",
                )
            require(
                finished["result"]["exit_code"] == 0 or deliberate_oom,
                f"{kind} smoke failed: {logs} {finished['result']}",
            )
            require(
                "qualification-recipe-passed" in logs or deliberate_oom,
                "Missing actual recipe outcome",
            )
            if kind == "agent":
                require(
                    secret not in logs and "[REDACTED]" in logs,
                    "Known secret leaked in log",
                )
                require(
                    (state / "operations" / operation / "input" / "secret").read_bytes()
                    == b"",
                    "Backend secret survived completion",
                )
                result_manifest, exported = harness.client.export(operation, source)
                checkout = state / ("import-proof-" + operation)
                checkout.mkdir(mode=0o700)
                from .artifacts import extract

                extract(source, checkout)
                import_result(checkout, source, exported, result_manifest)
                require(
                    (checkout / "added.txt").read_text() == "isolated addition\n",
                    "Addition was not imported",
                )
                require(
                    not (checkout / "delete.txt").exists(), "Deletion was not imported"
                )
                require(
                    (checkout / "executable").stat().st_mode & 0o111,
                    "Executable mode was lost",
                )
                require(
                    not (checkout / ".git").exists(), "Agent Git metadata was imported"
                )
                observations["artifact_manifest"] = result_manifest
            observations[kind] = {
                "operation_id": operation,
                "container_id": finished["container_id"],
                "exit_code": finished["result"]["exit_code"],
                "log_digest": finished["result"]["log_digest"],
                "log": logs,
                "expected_oom_kill": deliberate_oom,
            }
            changed = {**manifest, "source_digest": "0" * 64}
            try:
                harness.client.prepare(changed)
            except ExecutorUnavailable:
                pass
            else:
                raise RuntimeError("Changed operation manifest was accepted")
        leak = harness.prepare("leak", source)
        leak_op = leak["operation_id"]
        operations.append(leak_op)
        leak_secret = "qualification-private-leak-probe-" + secrets.token_hex(16)
        harness.client.supply_secret(leak_op, leak_secret)
        harness.client.start(leak_op)
        leaked = harness.finished(leak_op)
        require(
            "selected backend credential" in leaked["result"].get("export_error", ""),
            "Known credential was allowed into exported source",
        )
        require(
            not leaked["result"].get("artifact"),
            "Credential-containing source export was published",
        )
        require(
            leak_secret not in harness.client.logs(leak_op).decode(),
            "Credential appeared in leak-probe logs",
        )
        observations["credential_export_denied"] = {
            "operation_id": leak_op,
            "container_id": leaked["container_id"],
            "export_error": leaked["result"]["export_error"],
        }
        manifest = harness.prepare("sleep", source)
        operation = manifest["operation_id"]
        operations.append(operation)
        first = harness.client.start(operation)
        time.sleep(1)
        harness.shutdown()
        harness.start()
        recovered = harness.client.start(operation)
        require(
            recovered["container_id"] == first["container_id"]
            and recovered["state"] == "running",
            "Restart duplicated or lost the active operation",
        )
        stopped = harness.client.stop(operation)
        require(
            stopped["descendants_terminated"] and stopped["state"] == "exited",
            "Descendant stop was not confirmed",
        )
        require(harness.client.stop(operation) == stopped, "Stop was not retry-stable")
        _, exported = harness.client.export(operation, source)
        from .artifacts import unpack

        require(
            "started.txt" in unpack(exported), "Workspace was lost before stop/export"
        )
        observations["restart"] = {
            "operation_id": operation,
            "container_id": first["container_id"],
            "stopped": stopped,
        }
        from .engine import Docker

        docker = Docker()
        held = harness.prepare("pid-hold", source)
        held_op = held["operation_id"]
        operations.append(held_op)
        held_start = harness.client.start(held_op)
        held_id = held_start["container_id"]
        deadline = time.monotonic() + 30
        occupied = 0
        while time.monotonic() < deadline:
            occupied = int(
                docker.run(
                    "stats", "--no-stream", "--format", "{{.PIDs}}", held_id
                ).strip()
            )
            if occupied == held["limits"]["pids"]:
                break
            time.sleep(0.1)
        require(
            occupied == 32,
            "PID-hold fixture did not occupy all configured process slots",
        )
        held_status = harness.client.status(held_op)
        require(
            held_status["state"] == "running"
            and held_status["container_id"] == held_id,
            "PID exhaustion lost the running operation identity",
        )
        require(
            harness.client.start(held_op)["container_id"] == held_id,
            "Retry during PID exhaustion replaced the container",
        )
        held_stop = harness.client.stop(held_op)
        require(
            held_stop["state"] == "exited" and held_stop.get("descendants_terminated"),
            "Stop during full PID occupancy did not confirm termination",
        )
        require(
            held_stop["container_id"] == held_id and docker.inspect(held_id) is None,
            "PID-hold container identity or removal evidence is invalid",
        )
        require(
            held_stop["result"]["exit_code"] == -9
            and held_stop["result"].get("canceled") is True,
            "PID-hold cancellation did not preserve its actual terminated result",
        )
        _, held_export = harness.client.export(held_op, source)
        require(
            "pids-full.txt" in unpack(held_export),
            "Full-PID cancellation lost the verified workspace marker",
        )
        observations["pid_exhaustion_stop"] = {
            "operation_id": held_op,
            "container_id": held_id,
            "occupied_pids": occupied,
            "configured_pids": 32,
            "status_while_full": held_status,
            "stopped": held_stop,
        }
        first_db = harness.prepare("database", source, canonical({"hold_seconds": 20}))
        first_op = first_db["operation_id"]
        operations.append(first_op)
        harness.client.start(first_op)
        record = json.loads(
            (state / "operations" / first_op / "record.json").read_bytes()
        )
        database = docker.inspect(record["name"] + "-db")
        if database is None:
            raise RuntimeError("Disposable database container is absent")
        peer = database["NetworkSettings"]["Networks"][record["name"] + "-net"][
            "IPAddress"
        ]
        second_db = harness.prepare(
            "database",
            source,
            canonical({"foreign_db": peer}),
            run_id=first_db["run_id"],
        )
        second_op = second_db["operation_id"]
        operations.append(second_op)
        harness.client.start(second_op)
        observations["database"] = []
        for db_op in (first_op, second_op):
            finished = harness.finished(db_op)
            logs = harness.client.logs(db_op).decode(errors="replace")
            require(
                finished["result"]["exit_code"] == 0,
                "Disposable database smoke failed: " + logs,
            )
            require(
                "fresh-disposable-postgresql-query-passed" in logs,
                "Missing actual database query outcome",
            )
            observations["database"].append(
                {
                    "operation_id": db_op,
                    "container_id": finished["container_id"],
                    "log_digest": finished["result"]["log_digest"],
                    "exit_code": 0,
                }
            )
        malicious = io.BytesIO()
        with tarfile.open(fileobj=malicious, mode="w") as archive:
            entry = tarfile.TarInfo("../escape")
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
        try:
            harness.client.upload("source", malicious.getvalue())
        except ExecutorUnavailable:
            pass
        else:
            raise RuntimeError("Archive traversal upload succeeded")
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            try:
                resolve_public("pypi.org", set())
            except ValueError:
                pass
            else:
                raise RuntimeError("Rebound private DNS destination was accepted")
        require(
            harness.client.capabilities()["binding_digest"] == initial_binding,
            "Runtime binding changed during boundary probes; qualification is revoked.",
        )
    finally:
        for operation in operations:
            try:
                harness.client.stop(operation)
            except ExecutorUnavailable:
                pass
        harness.shutdown()
    runtime = Runtime(state, profiles)
    try:
        report = {
            "schema_version": 1,
            "binding": binding(runtime),
            "checks": sorted(REQUIRED_CHECKS),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "observations": observations,
            "native_agent_ready": False,
            "image_checks": {profiles[0].image: sorted(REQUIRED_CHECKS)},
        }
        durable(state / "qualification.json", canonical(report))
        return report
    finally:
        runtime.close()


def qualify(state: Path, profiles: list[Profile]) -> dict:
    preflight = Runtime(state, profiles)
    try:
        if any(
            preflight.record(path.name).get("launch_intent")
            and preflight.record(path.name)["state"] != "exited"
            for path in preflight.operations.iterdir()
        ):
            raise RuntimeError(
                "Active or unknown operations need reconciliation before qualification."
            )
        durable(
            state / "qualification.json",
            canonical(
                {
                    "schema_version": 1,
                    "reason": "Qualification in progress or failed; execution is revoked.",
                }
            ),
        )
    finally:
        preflight.close()
    primary = profiles[0].image
    database = next(
        profile.database_image for profile in profiles if profile.database_image
    )
    report = qualify_one(state, profiles)
    for image in sorted({profile.image for profile in profiles} - {primary}):
        probe_state = state / ("image-probe-" + image.removeprefix("sha256:")[:16])
        image_report = qualify_one(probe_state, smoke_profiles(image, database))
        report["image_checks"][image] = image_report["checks"]
        report.setdefault("image_observations", {})[image] = image_report
    durable(state / "qualification.json", canonical(report))
    runtime = Runtime(state, profiles)
    try:
        from .qualification import capabilities

        require(
            capabilities(runtime)["qualified"],
            "Runtime binding changed during qualification; rerun against the installed source/images.",
        )
    finally:
        runtime.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--database-image",
        required=True,
        help="Installed immutable PostgreSQL 17 image ID",
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Installed immutable sha256 image ID built from vibearr_executor/Dockerfile",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        help="Additional operator-owned immutable profiles to bind",
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    profiles = smoke_profiles(args.image, args.database_image)
    if args.profiles:
        profiles.extend(
            Profile.parse(value) for value in json.loads(args.profiles.read_bytes())
        )
    report = qualify(args.state, profiles)
    if args.evidence:
        args.evidence.write_bytes(canonical(report) + b"\n")
    print(
        json.dumps(
            {
                "qualified": True,
                "checks": report["checks"],
                "binding": report["binding"],
                "native_agent_ready": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
