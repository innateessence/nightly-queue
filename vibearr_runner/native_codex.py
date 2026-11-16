"""Codex CLI sessions through the qualified OCI executor."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from uuid import UUID

from .native_sessions import NativeSession, NativeSessionError
from .policy import ExecutionPolicy

CODEX_VERSION = "0.169.0"
MAX_EVENTS = 2048
MAX_OUTPUT = 1024 * 1024
MAX_LINE = 256 * 1024
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


def codex_profile(
    image: str,
    kind: str,
    *,
    profile_id: str | None = None,
    limits=None,
    database_image="",
):
    from vibearr_executor.codex_task import ARGV, HOSTS, SECRET_ENV
    from vibearr_executor.contracts import Limits, Profile

    if kind not in HOSTS:
        raise NativeSessionError(
            "Codex supports a dedicated API token or an explicit subscription session."
        )
    return Profile(
        profile_id or f"codex-{kind}-v1",
        1,
        image,
        "agent",
        ARGV,
        limits or Limits(2, 4096, 128, 2048),
        HOSTS[kind],
        SECRET_ENV,
        database_image,
    )


class CodexEvents:
    def __init__(self):
        self.pending = b""
        self.received = 0
        self.events = []
        self.session_id = ""
        self.completed = False
        self.failed = False
        self.usage = {}

    def feed(self, content: bytes, *, final=False):
        self.received += len(content)
        if self.received > MAX_OUTPUT:
            raise NativeSessionError("Native Codex output exceeds its byte limit.")
        self.pending += content
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.line(line)
        if len(self.pending) > MAX_LINE:
            raise NativeSessionError("Native Codex event exceeds its byte limit.")
        if final and self.pending.strip():
            self.line(self.pending)
            self.pending = b""
        return self.events

    def line(self, line: bytes):
        if len(line) > MAX_LINE or len(self.events) >= MAX_EVENTS:
            raise NativeSessionError("Native Codex event limit exceeded.")
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self.events.append(
                {"kind": "diagnostic", "text": "Codex emitted a non-event diagnostic."}
            )
            return
        if not isinstance(value, dict):
            raise NativeSessionError("Native Codex event must be an object.")
        kind = value.get("type")
        if kind == "thread.started":
            session_id = value.get("thread_id")
            try:
                if (
                    str(UUID(session_id)) != session_id
                    or self.session_id
                    and session_id != self.session_id
                ):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise NativeSessionError(
                    "Native Codex session identity is invalid or changed."
                ) from None
            self.session_id = session_id
            self.events.append({"kind": "session.started", "session_id": session_id})
        elif kind == "turn.started":
            self.events.append({"kind": "turn.started"})
        elif kind == "turn.completed":
            if self.completed:
                raise NativeSessionError(
                    "Native Codex emitted duplicate turn completion."
                )
            usage = value.get("usage", {})
            if not isinstance(usage, dict):
                raise NativeSessionError("Native Codex usage is invalid.")
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                if key not in usage:
                    continue
                if type(usage[key]) is not int or not 0 <= usage[key] < 2**63:
                    raise NativeSessionError("Native Codex usage is invalid.")
                self.usage[key] = usage[key]
            self.completed = True
            self.events.append(
                {"kind": "turn.completed", "usage": dict(self.usage), "unit": "tokens"}
            )
        elif kind in {"turn.failed", "error"}:
            self.failed = self.failed or kind == "turn.failed"
            self.events.append(
                {
                    "kind": "error" if kind == "turn.failed" else "diagnostic",
                    "text": "Native Codex reported an execution or authentication error.",
                }
            )
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = value.get("item")
            if not isinstance(item, dict):
                raise NativeSessionError("Native Codex item is invalid.")
            event = {"kind": kind, "item_type": str(item.get("type", "unknown"))[:80]}
            if item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
                event["text"] = item["text"][:4000]
            self.events.append(event)
        else:
            self.events.append(
                {"kind": "diagnostic", "text": "Codex emitted an unrecognized event."}
            )


class NativeCodexAdapter:
    provider = "codex"
    version = CODEX_VERSION
    resume_supported = False

    def __init__(self, client):
        self.client = client

    def selected_profile(self, policy: ExecutionPolicy, kind: str):
        from vibearr_executor.contracts import Limits

        capabilities = self.client.capabilities()
        if capabilities.get("runtime") != "docker-oci-v1" or any(
            type(capabilities.get(key)) is not int or capabilities[key] != 1
            for key in ("schema_version", "protocol_version", "qualification_version")
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
            profile = codex_profile(
                value["image"],
                kind,
                profile_id=value["profile_id"],
                limits=Limits.parse(value["limits"]),
                database_image=value["database_image"],
            )
            if profile.digest != value.get("profile_digest"):
                raise NativeSessionError(
                    "Selected profile is not the pinned Codex launch recipe."
                )
            if not set(profile.network_hosts) <= set(policy.network_hosts):
                raise NativeSessionError(
                    "Codex authentication requires hosts outside project policy."
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
                    "Codex profile cannot enforce configured resource limits."
                )
            return profile
        raise NativeSessionError(
            "Install the project's registered native Codex profile."
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
        from vibearr_executor.codex_task import auth_material, context_data
        from vibearr_executor.contracts import Manifest, canonical, digest
        from vibearr_executor.secret_material import contains_secret

        selected = ExecutionPolicy.from_data(policy)
        try:
            reference, material = credential["credential"], credential["material"]
            kind, secret = reference["kind"], material["secret"]
            if reference["provider"] != "codex":
                raise ValueError
            for key in ("id", "version"):
                if type(reference[key]) is not int or reference[key] < 1:
                    raise ValueError
            runtime_secret = auth_material(kind, secret)
        except (KeyError, TypeError, ValueError):
            raise NativeSessionError(
                "Selected native Codex credential is unavailable or invalid."
            ) from None
        account = {
            "backend": "codex",
            "credential_id": reference["id"],
            "credential_version": reference["version"],
            "model": model,
        }
        context = context_data(
            {
                "schema_version": 1,
                "adapter": "codex",
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
                "adapter": "codex",
                "agent_version": CODEX_VERSION,
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
            session.adapter != "codex"
            or session.agent_version != CODEX_VERSION
            or session.account["backend"] != "codex"
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
            "adapter": "codex",
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
            "adapter": "codex",
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

    def read_events(self, session: NativeSession) -> tuple[CodexEvents, bytes]:
        operation = self.bound(session)
        content = bytearray()
        while len(content) <= MAX_OUTPUT:
            chunk = self.client.logs(operation, len(content), 65536)
            if not chunk:
                parser = CodexEvents()
                parser.feed(bytes(content), final=True)
                return parser, bytes(content)
            content.extend(chunk)
        raise NativeSessionError("Native Codex output exceeds its byte limit.")

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
            "agent_version": CODEX_VERSION,
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
            from vibearr_executor.codex_task import auth_material

            reference = credential["credential"]
            if reference["provider"] != "codex":
                raise NativeSessionError("Select a native Codex credential.")
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
                    "backend": "codex",
                    "credential_id": reference["id"],
                    "credential_version": reference["version"],
                    "model": model,
                }
                capabilities = self.client.capabilities()
                matches = (
                    proof.get("schema_version") == 1
                    and proof.get("authenticated") is True
                    and proof.get("adapter") == "codex"
                    and proof.get("account") == expected
                    and model is not None
                    and proof.get("auth_kind") == reference["kind"]
                    and proof.get("agent_version") == CODEX_VERSION
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
                    "agent_version": CODEX_VERSION,
                    "profile_digest": profile.digest,
                    "reason": "Native authentication probe passed for this selected account, model and installed runtime.",
                }
            return {
                "native_adapter_available": True,
                "auth_available": True,
                "authenticated": False,
                "native_agent_ready": False,
                "agent_version": CODEX_VERSION,
                "profile_digest": profile.digest,
                "reason": "Run a bounded native authentication/task qualification; configured credentials alone do not prove live auth.",
            }
        except (ImportError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            return {
                "native_adapter_available": True,
                "auth_available": False,
                "authenticated": False,
                "native_agent_ready": False,
                "reason": str(exc) or "Native Codex diagnostics are unavailable.",
            }
