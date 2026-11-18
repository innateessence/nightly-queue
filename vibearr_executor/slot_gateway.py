"""Route retry-stable operations to fixed authenticated worker slots."""

import json
import fcntl
from pathlib import Path
import signal
import threading
from uuid import uuid4

from .client import Client, ExecutorUnavailable
from .contracts import Conflict, NotFound, Rejected, canonical, digest, identifier
from .runtime import durable
from .server import Handler, Server


class Gateway:
    def __init__(self, directory, clients):
        self.directory, self.clients = directory, clients
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.lockfile = (directory / "gateway.lock").open("a")
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = directory / "instance"
        if not identity.exists():
            durable(identity, str(uuid4()).encode())
        self.instance = identifier(identity.read_text())
        self.lock = threading.RLock()
        self.artifacts = directory / "artifacts"
        self.artifacts.mkdir(mode=0o700, exist_ok=True)
        from .slot_builds import DependencyBuilds

        self.dependency_builds = DependencyBuilds(self)

    def close(self):
        self.lockfile.close()

    def capabilities(self):
        workers = {
            phase: client.capabilities() for phase, client in self.clients.items()
        }
        if any(
            value.get("runtime") != "compose-slot-v2"
            or value.get("protocol_version") != 2
            for value in workers.values()
        ):
            raise Rejected("Every slot must implement the Docker-only worker protocol.")
        profiles = [
            profile for value in workers.values() for profile in value["profiles"]
        ]
        binding = {phase: value["binding_digest"] for phase, value in workers.items()}
        return {
            "schema_version": 1,
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "qualification_version": 1,
            "instance_id": self.instance,
            "binding_digest": digest(canonical(binding)),
            "qualified": all(value["qualified"] for value in workers.values()),
            "accepting_work": all(
                value["accepting_work"] for value in workers.values()
            ),
            "profiles": profiles,
            "workers": workers,
            "account_auth": workers.get("agent", {}).get("account_auth", {}),
        }

    def upload(self, kind, content, expected_digest):
        if digest(content) != expected_digest:
            raise Rejected("Artifact digest differs.")
        result = None
        for client in self.clients.values():
            result = client.upload(kind, content)
        if kind == "source":
            with self.lock:
                path = self.artifacts / expected_digest
                if not path.exists():
                    usage = sum(
                        path.stat().st_size for path in self.artifacts.iterdir()
                    )
                    if usage + len(content) > 512 * 1024**2:
                        raise Rejected("Gateway source artifact capacity is exhausted.")
                    durable(path, content)
        return result

    def prepare(self, data):
        operation_id = identifier(data.get("operation_id"))
        phase = data.get("phase")
        if phase not in self.clients:
            raise Rejected("Select an installed worker phase.")
        with self.lock:
            path = self.directory / operation_id
            if path.exists():
                record = json.loads(path.read_bytes())
                if record["manifest_digest"] != digest(canonical(data)):
                    raise Conflict("Operation identity already binds a different job.")
            else:
                observed = self.clients[phase].capabilities()
                if not any(
                    value["profile_id"] == data.get("profile_id")
                    and value["profile_digest"] == data.get("profile_digest")
                    for value in observed["profiles"]
                ):
                    raise Rejected("Manifest does not bind an installed slot recipe.")
                record = {
                    "phase": phase,
                    "manifest_digest": digest(canonical(data)),
                    "instance_id": observed["instance_id"],
                    "source_digest": data.get("source_digest"),
                    "run_id": data.get("run_id"),
                }
                durable(path, canonical(record))
            client = self.client(operation_id)
            return client.prepare(data)

    def client(self, operation_id):
        path = self.directory / identifier(operation_id)
        if not path.exists():
            raise NotFound("Operation was never assigned to a worker.")
        record = json.loads(path.read_bytes())
        client = self.clients[record["phase"]]
        if client.capabilities()["instance_id"] != record["instance_id"]:
            raise ExecutorUnavailable(
                "Worker state volume changed; reconcile the operation before replacement."
            )
        return client

    def start(self, operation_id):
        return self.client(operation_id).start(operation_id)

    def status(self, operation_id):
        path = self.directory / identifier(operation_id)
        if not path.exists():
            return {"operation_id": operation_id, "state": "not-started"}
        return self.client(operation_id).status(operation_id)

    def stop(self, operation_id):
        return self.client(operation_id).stop(operation_id)

    def pause(self, operation_id):
        return self.client(operation_id).pause(operation_id)

    def resume(self, operation_id):
        return self.client(operation_id).resume(operation_id)

    def supply_secret(self, operation_id, secret):
        return self.client(operation_id).supply_secret(operation_id, secret)

    def logs(self, operation_id, offset, limit):
        return self.client(operation_id).logs(operation_id, offset, limit)

    def export(self, operation_id):
        client = self.client(operation_id)
        import base64

        result = client.operation(operation_id, "result")
        return result["manifest"], base64.b64decode(result["content"], validate=True)

    def termination_receipt(self, operation_id):
        value = self.client(operation_id).termination_receipt(operation_id)
        value["instance_id"] = self.instance
        return value

    def purge(self, operation_id, run_id, resources):
        result = self.client(operation_id).purge(
            operation_id, run_id=run_id, resources=resources
        )
        with self.lock:
            path = self.directory / identifier(operation_id)
            record = json.loads(path.read_bytes())
            record["resources_removed"] = sorted(
                set(resources) | set(record.get("resources_removed", []))
            )
            durable(path, canonical(record))
            if "source" in resources and record.get("source_digest"):
                self.release_source(record["source_digest"])
        return result

    def release_source(self, source_digest):
        for other in self.directory.iterdir():
            if not other.is_file() or len(other.name) != 36:
                continue
            value = json.loads(other.read_bytes())
            if value.get(
                "source_digest"
            ) == source_digest and "source" not in value.get("resources_removed", []):
                return
        (self.artifacts / source_digest).unlink(missing_ok=True)


def main():
    clients = {}
    for phase in ("agent", "setup", "validation"):
        token = Path(f"/run/slots/{phase}/control-token").read_text().strip()
        clients[phase] = Client(f"http://{phase}-worker:8791", token)
    runtime = Gateway(Path("/var/lib/gateway"), clients)
    token = Path("/run/vibearr/runner/control-token").read_text().strip()
    with Server(("0.0.0.0", 8791), Handler) as server:
        server.runtime, server.token = runtime, token

        def stop(_signal, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever()
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
