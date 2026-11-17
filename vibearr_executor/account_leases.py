"""Account access leases bind one prepared worker operation and its boot."""

from datetime import datetime
import hmac
import json
import secrets
import threading
import time
from uuid import uuid4

from .codex_account import AccountStore, managed_auth, private_bytes, private_directory
from .contracts import canonical, digest, identifier, sha256
from .runtime import durable


LEASE_KIND = "codex-account-lease"
BINDING_FIELDS = {
    "operation_id",
    "run_id",
    "source_digest",
    "context_digest",
    "account_digest",
    "deadline",
    "execution_identity",
    "credential",
}


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise ValueError("Bind the complete account lease to a prepared operation.")
    for key in ("operation_id", "run_id"):
        identifier(value[key])
    for key in ("source_digest", "context_digest", "account_digest"):
        sha256(value[key])
    credential = value["credential"]
    if (
        not isinstance(credential, dict)
        or set(credential) != {"id", "version"}
        or any(type(item) is not int or item < 1 for item in credential.values())
    ):
        raise ValueError("Account lease requires its saved credential reference.")
    worker = value["execution_identity"]
    if not isinstance(worker, dict) or set(worker) != {
        "runtime",
        "protocol_version",
        "instance_id",
        "worker_boot",
        "binding_digest",
    }:
        raise ValueError("Account lease requires a complete worker identity.")
    if (
        worker["runtime"] != "compose-slot-v2"
        or type(worker["protocol_version"]) is not int
        or worker["protocol_version"] != 2
    ):
        raise ValueError("Account lease requires the Docker worker protocol.")
    for key in ("instance_id", "worker_boot"):
        identifier(worker[key])
    sha256(worker["binding_digest"])
    deadline = datetime.fromisoformat(value["deadline"])
    if deadline.utcoffset() is None:
        raise ValueError("Account lease deadline must include its timezone.")
    return value


def lease_secret(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "binding",
        "token",
        "auth",
    }:
        raise ValueError("Supply a complete protected account lease.")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != LEASE_KIND
    ):
        raise ValueError("Unsupported protected account lease.")
    validate_binding(value["binding"])
    if (
        not isinstance(value["token"], str)
        or not 32 <= len(value["token"]) <= 128
        or any(char.isspace() for char in value["token"])
    ):
        raise ValueError("Account lease capability is invalid.")
    if not isinstance(value["auth"], dict):
        raise ValueError("Account lease requires access material.")
    from .codex_task import auth_material

    auth_material(
        "session", canonical(value["auth"]).decode(), runtime=True, allow_expired=True
    )
    return value


class AccountLeases:
    def __init__(self, root, refresh, *, clock=time.time):
        self.root = root
        self.clock = clock
        private_directory(root)
        self.store = AccountStore(root / "accounts", refresh)
        self.leases = root / "leases"
        private_directory(self.leases)
        authority = root / "authority"
        if not authority.exists():
            durable(authority, str(uuid4()).encode())
        self.authority = identifier(private_bytes(authority).decode())
        self.lock = threading.RLock()

    def healthy(self):
        return {
            "runtime": "codex-account-v1",
            "protocol_version": 1,
            "authority": self.authority,
        }

    def read(self, operation):
        return json.loads(
            private_bytes(self.leases / identifier(operation), 512 * 1024)
        )

    def register(self, credential, secret):
        with self.lock:
            return self.store.register(credential, secret)

    def available(self, binding):
        deadline = datetime.fromisoformat(binding["deadline"])
        remaining = deadline.timestamp() - self.clock()
        if not 0 < remaining <= 86400:
            raise ValueError(
                "Account operation lease is expired or exceeds its deadline bound."
            )
        marker = self.root / "restore-required.json"
        if marker.exists():
            restored = json.loads(private_bytes(marker))
            reference = binding["credential"]
            directory = (
                self.root / "accounts" / f"{reference['id']}-{reference['version']}"
            )
            authorization = directory / "restore-authorization.json"
            if (
                not authorization.exists()
                or json.loads(private_bytes(authorization)) != restored
            ):
                raise ValueError(
                    "Sign in again to this account on the restored deployment."
                )

    def prepare(self, request):
        if not isinstance(request, dict) or set(request) != {"binding", "secret"}:
            raise ValueError(
                "Supply the selected managed account and operation binding."
            )
        binding = validate_binding(request["binding"])
        secret = request["secret"]
        if not isinstance(secret, str):
            raise ValueError("Managed account login is unavailable.")
        seed = digest(canonical(managed_auth(secret.encode())))
        operation = binding["operation_id"]
        with self.lock:
            self.available(binding)
            path = self.leases / operation
            if path.exists():
                record = self.read(operation)
                if record["binding"] != binding or record["seed_digest"] != seed:
                    raise ValueError(
                        "Account operation already binds another credential or worker."
                    )
                self.authorized(record, record["token"], binding["execution_identity"])
                return self.capsule(record)
            if len(list(self.leases.iterdir())) >= 1000:
                self.prune()
                if len(list(self.leases.iterdir())) >= 1000:
                    raise ValueError(
                        "Account lease storage is full; reconcile unfinished private leases."
                    )
            material = self.store.material(binding["credential"], secret)
            record = {
                "binding": binding,
                "seed_digest": seed,
                "token": secrets.token_urlsafe(48),
                "authority": self.authority,
                "closed": False,
                "initial_auth": json.loads(material),
                "renewals": {},
            }
            durable(path, canonical(record))
            return self.capsule(record)

    def prune(self):
        for path in self.leases.iterdir():
            record = self.read(path.name)
            deadline = datetime.fromisoformat(record["binding"]["deadline"]).timestamp()
            if record["closed"] and deadline <= self.clock():
                path.unlink()

    def capsule(self, record):
        value = {
            "schema_version": 1,
            "kind": LEASE_KIND,
            "binding": record["binding"],
            "token": record["token"],
            "auth": record["initial_auth"],
        }
        if len(canonical(value)) > 16384:
            raise ValueError("Account access material exceeds the worker secret limit.")
        return value

    def authorized(self, record, token, identity):
        self.available(record["binding"])
        if record["authority"] != self.authority or record["closed"]:
            raise ValueError("Account operation lease is retired.")
        if not isinstance(token, str) or not hmac.compare_digest(
            record["token"], token
        ):
            raise ValueError("Account operation capability differs.")
        if identity != record["binding"]["execution_identity"]:
            raise ValueError("Account lease belongs to another worker boot.")

    def renew(self, operation, token, identity, request_id):
        identifier(request_id)
        with self.lock:
            record = self.read(operation)
            self.authorized(record, token, identity)
            if request_id in record["renewals"]:
                return record["renewals"][request_id]
            if len(record["renewals"]) >= 16:
                raise ValueError("Account operation exceeded its renewal bound.")
            material = self.store.current(
                record["binding"]["credential"], record["seed_digest"], force=True
            )
            value = {"auth": json.loads(material)}
            record["renewals"][request_id] = value
            durable(self.leases / operation, canonical(record))
            return value

    def close(self, operation, token, identity):
        with self.lock:
            record = self.read(operation)
            if (
                not isinstance(token, str)
                or not hmac.compare_digest(record["token"], token)
                or identity != record["binding"]["execution_identity"]
            ):
                raise ValueError("Account operation capability differs.")
            record["closed"] = True
            durable(self.leases / operation, canonical(record))
            return {"closed": True}
