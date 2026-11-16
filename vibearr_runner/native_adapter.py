"""Shared native session binding, receipts and authentication proof."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import math

from .native_sessions import NativeSession, NativeSessionError
from .policy import ExecutionPolicy

MAX_OUTPUT = 1024 * 1024
AUTH_PROMPT = "Authentication diagnostic: reply with a short greeting only. Do not run tools, edit files, or access another repository."


def auth_source():
    from vibearr_executor.artifacts import pack

    return pack(
        {
            "vibearr-auth-probe.txt": (
                0o644,
                b"Trusted native authentication diagnostic.\n",
            )
        }
    )


class NativeProcessAdapter:
    provider: str
    version: str

    def task_api(self):
        raise NotImplementedError

    def profile_factory(self, *args, **kwargs):
        raise NotImplementedError

    def events_factory(self):
        raise NotImplementedError

    resume_supported = False

    def __init__(self, client):
        self.client = client

    def selected_profile(self, policy: ExecutionPolicy, kind: str):
        from vibearr_executor import QUALIFICATION_VERSION
        from vibearr_executor.contracts import Limits

        capabilities = self.client.capabilities()
        versions = {
            "schema_version": 1,
            "protocol_version": 1,
            "qualification_version": QUALIFICATION_VERSION,
        }
        if capabilities.get("runtime") != "docker-oci-v1" or any(
            type(capabilities.get(key)) is not int or capabilities[key] != expected
            for key, expected in versions.items()
        ):
            raise NativeSessionError("Installed executor protocol is unsupported.")
        if (
            capabilities.get("qualified") is not True
            or capabilities.get("accepting_work") is not True
        ):
            raise NativeSessionError(
                "Qualify the installed executor and resolve its capacity before native work."
            )
        for value in capabilities.get("profiles", []):
            if value.get("profile_id") != policy.runtime_profile:
                continue
            profile = self.profile_factory(
                value["image"],
                kind,
                profile_id=value["profile_id"],
                limits=Limits.parse(value["limits"]),
                database_image=value["database_image"],
            )
            if profile.digest != value.get("profile_digest"):
                raise NativeSessionError(
                    "Selected profile is not the pinned native agent launch recipe."
                )
            if not set(profile.network_hosts) <= set(policy.network_hosts):
                raise NativeSessionError(
                    "Native agent authentication requires hosts outside project policy."
                )
            desired = Limits(
                policy.cpu_limit,
                policy.memory_mb,
                policy.pids_limit,
                policy.workspace_mb,
            )
            Limits.parse(asdict(desired))
            if any(
                amount > asdict(profile.limits)[key]
                for key, amount in asdict(desired).items()
            ):
                raise NativeSessionError(
                    "Native agent profile cannot enforce configured resource limits."
                )
            return profile
        raise NativeSessionError(
            "Install the project's registered native agent profile."
        )

    def prepare(
        self,
        *,
        run_id: str,
        operation_id: str,
        source: bytes,
        prompt: str,
        model: str,
        policy: dict,
        deadline: str,
        credential: dict,
    ) -> NativeSession:
        auth_material, context_data = self.task_api()
        from vibearr_executor.contracts import Manifest, canonical, digest
        from vibearr_executor.secret_material import contains_secret

        selected = ExecutionPolicy.from_data(policy)
        try:
            reference, material = credential["credential"], credential["material"]
            kind, secret = reference["kind"], material["secret"]
            if reference["provider"] != self.provider:
                raise ValueError
            for key in ("id", "version"):
                if type(reference[key]) is not int or reference[key] < 1:
                    raise ValueError
            runtime_secret = auth_material(kind, secret)
        except (KeyError, TypeError, ValueError):
            raise NativeSessionError(
                "Selected native agent credential is unavailable or invalid."
            ) from None
        account = {
            "backend": self.provider,
            "credential_id": reference["id"],
            "credential_version": reference["version"],
            "model": model,
        }
        context = context_data(
            {
                "schema_version": 1,
                "adapter": self.provider,
                "model": model,
                "prompt": prompt,
                "auth_kind": kind,
                "account_digest": digest(canonical(account)),
            }
        )
        encoded = canonical(context)
        if contains_secret(encoded, secret.encode()) or contains_secret(
            source, secret.encode()
        ):
            raise NativeSessionError(
                "Task source or prompt contains selected credential material."
            )
        profile = self.selected_profile(selected, kind)
        capabilities = self.client.capabilities()
        if capabilities.get("qualified") is not True or not any(
            value.get("profile_digest") == profile.digest
            and value.get("profile_id") == profile.profile_id
            for value in capabilities.get("profiles", [])
        ):
            raise NativeSessionError(
                "Runtime changed while preparing native execution; retry diagnostics."
            )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "operation_id": operation_id,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(source),
            "context_digest": digest(encoded),
            "phase": "agent",
            "limits": {
                "cpu": selected.cpu_limit,
                "memory_mb": selected.memory_mb,
                "pids": selected.pids_limit,
                "workspace_mb": selected.workspace_mb,
            },
            "deadline": deadline,
            "network_hosts": list(profile.network_hosts),
        }
        Manifest.parse(manifest, profile)
        session = NativeSession.from_data(
            {
                "adapter": self.provider,
                "agent_version": self.version,
                "account": account,
                "auth_kind": kind,
                "image_digest": profile.image,
                "runtime_binding_digest": capabilities.get("binding_digest"),
                "manifest": manifest,
            }
        )
        self.client.upload("source", source)
        self.client.upload("context", encoded)
        self.client.prepare(manifest)
        self.client.supply_secret(operation_id, runtime_secret)
        return session

    def bound(self, session: NativeSession):
        session = NativeSession.from_data(session.to_data())
        if (
            session.adapter != self.provider
            or session.agent_version != self.version
            or session.account["backend"] != self.provider
        ):
            raise NativeSessionError(
                "Native session belongs to another adapter/version."
            )
        self.client.prepare(session.manifest)
        return session.manifest["operation_id"]

    def start(self, session: NativeSession) -> dict:
        capabilities = self.client.capabilities()
        if (
            capabilities.get("qualified") is not True
            or capabilities.get("accepting_work") is not True
            or capabilities.get("binding_digest") != session.runtime_binding_digest
        ):
            raise NativeSessionError(
                "Runtime qualification changed before native start; reconcile existing execution."
            )
        return self.client.start(self.bound(session))

    def prepare_auth_probe(self, **request) -> NativeSession:
        return self.prepare(**request, source=auth_source(), prompt=AUTH_PROMPT)

    def authentication_result(self, session: NativeSession) -> dict:
        from vibearr_executor.contracts import canonical, digest

        context = {
            "schema_version": 1,
            "adapter": self.provider,
            "model": session.account["model"],
            "prompt": AUTH_PROMPT,
            "auth_kind": session.auth_kind,
            "account_digest": digest(canonical(session.account)),
        }
        source = auth_source()
        if session.manifest["source_digest"] != digest(source) or session.manifest[
            "context_digest"
        ] != digest(canonical(context)):
            raise NativeSessionError(
                "Native session is not the fixed authentication probe."
            )
        outcome = self.outcome(session, source)
        if not outcome["successful"] or outcome["artifact"]["result_digest"] != digest(
            source
        ):
            raise NativeSessionError(
                "Native authentication probe did not complete successfully."
            )
        capabilities = self.client.capabilities()
        if (
            capabilities.get("qualified") is not True
            or capabilities.get("binding_digest") != session.runtime_binding_digest
        ):
            raise NativeSessionError(
                "Runtime changed during native authentication probe; repeat diagnostics."
            )
        completed_at = outcome["result"].get("ended_at")
        now = datetime.now(timezone.utc)
        if (
            type(completed_at) not in (int, float)
            or not math.isfinite(completed_at)
            or not 0 < completed_at <= now.timestamp()
        ):
            raise NativeSessionError(
                "Native authentication completion timestamp is missing, invalid or in the future."
            )
        verified_at = datetime.fromtimestamp(completed_at, timezone.utc)
        if now >= verified_at + timedelta(minutes=15):
            raise NativeSessionError(
                "Native authentication probe expired; run a new bounded probe."
            )
        return {
            "schema_version": 1,
            "adapter": self.provider,
            "authenticated": True,
            "account": dict(session.account),
            "auth_kind": session.auth_kind,
            "runtime_binding_digest": session.runtime_binding_digest,
            "profile_id": session.manifest["profile_id"],
            "profile_digest": session.manifest["profile_digest"],
            "image_digest": session.image_digest,
            "agent_version": session.agent_version,
            "operation_id": session.manifest["operation_id"],
            "session_id": outcome["session_id"],
            "log_digest": outcome["log_digest"],
            "source_digest": digest(source),
            "context_digest": session.manifest["context_digest"],
            "verified_at": verified_at.isoformat(),
            "expires_at": (verified_at + timedelta(minutes=15)).isoformat(),
        }

    def inspect(self, session: NativeSession) -> dict:
        result = self.client.status(self.bound(session))
        if result.get("operation_id") != session.manifest["operation_id"] or result.get(
            "state"
        ) not in {"not-started", "running", "exited", "unknown"}:
            raise NativeSessionError(
                "Executor returned an invalid native operation state."
            )
        receipt = result.get("result")
        if receipt and (
            receipt.get("run_id") != session.manifest["run_id"]
            or receipt.get("operation_id") != session.manifest["operation_id"]
        ):
            raise NativeSessionError("Native execution receipt belongs to another run.")
        return result

    def cancel(self, session: NativeSession) -> dict:
        self.client.stop(self.bound(session))
        return self.inspect(session)

    def reconcile(self, session: NativeSession) -> dict:
        return self.inspect(session)

    def read_events(self, session: NativeSession):
        operation = self.bound(session)
        content = bytearray()
        while len(content) <= MAX_OUTPUT:
            chunk = self.client.logs(operation, len(content), 65536)
            if not chunk:
                parser = self.events_factory()
                parser.feed(bytes(content), final=True)
                self.validate_events(parser, session)
                return parser, bytes(content)
            content.extend(chunk)
        raise NativeSessionError("Native agent output exceeds its byte limit.")

    def validate_events(self, parser, session):
        pass

    def events(self, session: NativeSession) -> list[dict]:
        return self.read_events(session)[0].events

    def outcome(self, session: NativeSession, source: bytes) -> dict:
        if hashlib.sha256(source).hexdigest() != session.manifest["source_digest"]:
            raise NativeSessionError(
                "Outcome source does not match the prepared native session."
            )
        state = self.inspect(session)
        if (
            state["state"] != "exited"
            or state.get("descendants_terminated") is not True
        ):
            raise NativeSessionError(
                "Native outcome is still active or uncertain; reconcile the same operation."
            )
        result = state.get("result", {})
        parser, logs = self.read_events(session)
        success = (
            type(result.get("exit_code")) is int
            and result["exit_code"] == 0
            and result.get("timed_out") is False
            and not result.get("canceled")
            and not result.get("log_truncated")
            and parser.completed
            and not parser.failed
            and bool(parser.session_id)
        )
        if result.get("log_digest") != hashlib.sha256(logs).hexdigest():
            raise NativeSessionError(
                "Native event log does not match its protected receipt."
            )
        inventory, exported = self.client.export(
            session.manifest["operation_id"], source
        )
        return {
            "successful": bool(success),
            "session_id": parser.session_id,
            "agent_version": self.version,
            "usage": parser.usage,
            "usage_unit": "tokens",
            "quota_supported": False,
            "result": result,
            "artifact": inventory,
            "source": exported,
            "events": parser.events,
            "log_digest": result["log_digest"],
        }

    def diagnostics(
        self, policy: dict, credential: dict, *, model=None, proof=None
    ) -> dict:
        try:
            auth_material, _ = self.task_api()

            reference = credential["credential"]
            if reference["provider"] != self.provider:
                raise NativeSessionError("Select a native agent credential.")
            auth_material(reference["kind"], credential["material"]["secret"])
            profile = self.selected_profile(
                ExecutionPolicy.from_data(policy), reference["kind"]
            )
            if proof is not None:
                if not isinstance(proof, dict):
                    raise NativeSessionError(
                        "Native authentication proof is unavailable or invalid."
                    )
                expected = {
                    "backend": self.provider,
                    "credential_id": reference["id"],
                    "credential_version": reference["version"],
                    "model": model,
                }
                capabilities = self.client.capabilities()
                matches = (
                    proof.get("schema_version") == 1
                    and proof.get("authenticated") is True
                    and proof.get("adapter") == self.provider
                    and proof.get("account") == expected
                    and model is not None
                    and proof.get("auth_kind") == reference["kind"]
                    and proof.get("agent_version") == self.version
                    and proof.get("profile_id") == profile.profile_id
                    and proof.get("profile_digest") == profile.digest
                    and proof.get("image_digest") == profile.image
                    and capabilities.get("qualified") is True
                    and proof.get("runtime_binding_digest")
                    == capabilities.get("binding_digest")
                )
                verified = datetime.fromisoformat(proof["verified_at"])
                expires = datetime.fromisoformat(proof["expires_at"])
                now = datetime.now(timezone.utc)
                fresh = (
                    verified <= now < expires
                    and (expires - verified).total_seconds() <= 900
                )
                if not matches or not fresh:
                    raise NativeSessionError(
                        "Native authentication proof is stale or belongs to another account/model/runtime."
                    )
                return {
                    "native_adapter_available": True,
                    "auth_available": True,
                    "authenticated": True,
                    "native_agent_ready": True,
                    "agent_version": self.version,
                    "profile_digest": profile.digest,
                    "reason": "Native authentication probe passed for this selected account, model and installed runtime.",
                }
            return {
                "native_adapter_available": True,
                "auth_available": True,
                "authenticated": False,
                "native_agent_ready": False,
                "agent_version": self.version,
                "profile_digest": profile.digest,
                "reason": "Run a bounded native authentication/task qualification; configured credentials alone do not prove live auth.",
            }
        except (ImportError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            return {
                "native_adapter_available": True,
                "auth_available": False,
                "authenticated": False,
                "native_agent_ready": False,
                "reason": str(exc) or "Native agent diagnostics are unavailable.",
            }
