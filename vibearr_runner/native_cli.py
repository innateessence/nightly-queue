"""Selected subscription and external Ollama CLIs in the qualified worker."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from vibearr_executor.contracts import Manifest, Profile, canonical, digest
from vibearr_executor import SLOT_QUALIFICATION_VERSION
from vibearr_executor.compact_profiles import RUNTIME
from vibearr_executor.integrated_profiles import VERSIONS

from .native_codex import (
    AUTH_PROMPT,
    MAX_OUTPUT,
    CodexEvents,
    auth_source,
)
from .native_sessions import NativeSession, NativeSessionError
from .policy import ExecutionPolicy
from .opencode_connection import valid_model
from .native_claude import consume_claude


class NativeEvents(CodexEvents):
    def __init__(self, provider):
        if provider not in VERSIONS:
            raise NativeSessionError("Unsupported native CLI.")
        super().__init__()
        self.provider = provider
        self.failure_reason = ""

    def classify_failure(self, event):
        text = json.dumps(event).lower()
        if any(
            word in text
            for word in (
                "rate_limit",
                "rate limit",
                "usage limit",
                "quota",
                "limit reached",
            )
        ):
            return "Selected agent allowance or capacity is exhausted; wait for reset or adjust the configured schedule."
        if any(
            word in text
            for word in (
                "unauthorized",
                "authentication",
                "token expired",
                "login required",
                "401",
            )
        ):
            return "Selected subscription login is unavailable or expired; reconnect the account inside the runner."
        if self.provider == "opencode":
            return "Selected external Ollama endpoint or installed model is unavailable; check the connection and model."
        return "Selected native CLI failed; inspect its protected diagnostics before retrying."

    def line(self, line):
        if self.provider == "claude":
            return consume_claude(self, line)
        if self.provider == "opencode":
            from .native_opencode import consume

            return consume(self, line)
        try:
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") in {
                "error",
                "turn.failed",
            }:
                self.failure_reason = self.classify_failure(event)
        except ValueError:
            pass
        return super().line(line)


class NativeCLIAdapter:
    def __init__(self, client, provider):
        if provider not in VERSIONS:
            raise NativeSessionError("Unsupported native CLI.")
        self.client = client
        self.provider = provider
        self.version = VERSIONS[provider]

    def start(self, session: NativeSession) -> dict:
        capabilities = self.client.capabilities()
        if (
            capabilities.get("qualified") is not True
            or capabilities.get("binding_digest") != session.runtime_binding_digest
        ):
            raise NativeSessionError(
                "Runtime qualification changed before native start; reconcile existing execution."
            )
        operation = self.bound(session)
        if (
            capabilities.get("accepting_work") is not True
            and self.client.status(operation)["state"] == "not-started"
        ):
            raise NativeSessionError(
                "Runtime capacity blocks a new native start; reconcile the original operation."
            )
        return self.client.start(operation)

    def prepare_auth_probe(self, **request) -> NativeSession:
        return self.prepare(**request, source=auth_source(), prompt=AUTH_PROMPT)

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

    def events(self, session: NativeSession) -> list[dict]:
        return self.read_events(session)[0].events

    def connection(self, credential):
        reference = credential.get("credential", {})
        required = "connection" if self.provider == "opencode" else "login"
        if (
            reference.get("provider") != self.provider
            or reference.get("kind") != required
        ):
            raise NativeSessionError(
                "Select a runner subscription login or external Ollama connection."
            )
        if self.provider == "opencode":
            from .native_opencode import connection

            return {
                "provider": "opencode",
                "endpoint": connection(credential["material"]["secret"]),
                "credential_id": reference["id"],
            }
        return {"provider": self.provider, "credential_id": reference["id"]}

    def selected_profile(self, policy, _kind):
        capabilities = self.client.capabilities()
        versions = {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": SLOT_QUALIFICATION_VERSION,
        }
        invalid_version = any(
            type(capabilities.get(key)) is not int or capabilities[key] != expected
            for key, expected in versions.items()
        )
        if (
            capabilities.get("runtime") != RUNTIME
            or capabilities.get("qualified") is not True
            or invalid_version
        ):
            raise NativeSessionError(
                "Qualify the current execution worker boot before coding."
            )
        for value in capabilities["profiles"]:
            if (
                value["profile_id"] != policy.runtime_profile
                or value["phase"] != "agent"
            ):
                continue
            profile = Profile.parse(
                {key: entry for key, entry in value.items() if key != "profile_digest"}
            )
            if value.get("profile_digest") != profile.digest:
                raise NativeSessionError("Registered native profile digest differs.")
            desired = {
                "cpu": policy.cpu_limit,
                "memory_mb": policy.memory_mb,
                "pids": policy.pids_limit,
                "workspace_mb": policy.workspace_mb,
            }
            if profile.data()["limits"] != desired or not set(
                profile.network_hosts
            ) <= set(policy.network_hosts):
                raise NativeSessionError(
                    "Policy differs from the installed runner limits or model hosts."
                )
            return profile
        raise NativeSessionError("Selected native runner profile is unavailable.")

    def context(
        self, account, prompt, kind, *, factory_role=None, browser_capture=None
    ):
        value = {
            "schema_version": 1,
            "adapter": self.provider,
            "model": account["model"],
            "prompt": prompt,
            "auth_kind": kind,
            "account_digest": digest(canonical(account)),
            "credential_id": account["credential_id"],
        }
        if factory_role is not None:
            from .native_conformance import FACTORY_ROLES

            if factory_role not in FACTORY_ROLES:
                raise NativeSessionError("Unknown native factory role.")
            value["factory_role"] = factory_role
        if browser_capture is not None:
            from .native_conformance import validate_browser_capture

            value["browser_capture"] = validate_browser_capture(
                factory_role, browser_capture
            )
        return value

    def prepare(
        self,
        *,
        run_id,
        operation_id,
        source,
        prompt,
        model,
        policy,
        deadline,
        credential,
        factory_role=None,
        browser_capture=None,
    ):
        if not valid_model(model):
            raise NativeSessionError(
                "Select a native CLI model identifier without URLs or arguments."
            )
        selected = ExecutionPolicy.from_data(policy)
        connection = self.connection(credential)
        if self.provider != "opencode":
            status = self.client.request(
                f"/v3/logins/{self.provider}/{connection['credential_id']}"
            )
            if not status.get("available"):
                raise NativeSessionError("Subscription login required.")
            connection["login_digest"] = status["login_digest"]
        reference = credential["credential"]
        profile = self.selected_profile(selected, reference["kind"])
        account = {
            "backend": self.provider,
            "credential_id": reference["id"],
            "credential_version": reference["version"],
            "model": model,
        }
        context = canonical(
            self.context(
                account,
                prompt,
                reference["kind"],
                factory_role=factory_role,
                browser_capture=browser_capture,
            )
        )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "operation_id": operation_id,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(source),
            "context_digest": digest(context),
            "phase": "agent",
            "limits": profile.data()["limits"],
            "deadline": deadline,
            "network_hosts": list(profile.network_hosts),
        }
        Manifest.parse(manifest, profile)
        capabilities = self.client.capabilities()
        self.client.upload("source", source)
        self.client.upload("context", context)
        prepared = self.client.prepare(manifest)
        session = NativeSession.from_data(
            {
                "adapter": self.provider,
                "agent_version": self.version,
                "account": account,
                "auth_kind": reference["kind"],
                "image_digest": profile.image,
                "runtime_binding_digest": capabilities["binding_digest"],
                "manifest": manifest,
                "execution_identity": prepared.get("execution_identity"),
            }
        )
        self.client.supply_secret(operation_id, json.dumps(connection))
        return session

    def bound(self, session):
        NativeSession.from_data(session.to_data())
        if (
            session.adapter != self.provider
            or session.agent_version != self.version
            or session.account["backend"] != self.provider
        ):
            raise NativeSessionError(
                "Native session belongs to another CLI or version."
            )
        self.client.prepare(session.manifest)
        return session.manifest["operation_id"]

    def read_events(self, session):
        operation = self.bound(session)
        content = bytearray()
        while len(content) <= MAX_OUTPUT:
            chunk = self.client.logs(operation, len(content), 65536)
            if not chunk:
                parser = NativeEvents(self.provider)
                parser.feed(bytes(content), final=True)
                return parser, bytes(content)
            content.extend(chunk)
        raise NativeSessionError("Native CLI output exceeds its bound.")

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

    def failure_reason(self, session):
        return (
            self.read_events(session)[0].failure_reason
            or "Selected native CLI failed, timed out or returned incomplete output."
        )

    def authentication_result(self, session):
        source = auth_source()
        context = canonical(
            self.context(session.account, AUTH_PROMPT, session.auth_kind)
        )
        if session.manifest["source_digest"] != digest(source) or session.manifest[
            "context_digest"
        ] != digest(context):
            raise NativeSessionError(
                "Authentication proof requires the fixed native probe."
            )
        outcome = self.outcome(session, source)
        if not outcome["successful"] or outcome["artifact"]["result_digest"] != digest(
            source
        ):
            raise NativeSessionError(
                "Selected login or external model failed its native probe."
            )
        capabilities = self.client.capabilities()
        if (
            capabilities.get("runtime") != RUNTIME
            or capabilities.get("qualified") is not True
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
        verified = datetime.fromtimestamp(completed_at, timezone.utc)
        if now >= verified + timedelta(minutes=15):
            raise NativeSessionError(
                "Native authentication probe expired; run a new bounded probe."
            )
        login_digest = ""
        if self.provider != "opencode":
            status = self.client.request(
                f"/v3/logins/{self.provider}/{session.account['credential_id']}"
            )
            login_digest = status.get("login_digest", "")
        return {
            "schema_version": 1,
            "adapter": self.provider,
            "authenticated": True,
            "account": session.account,
            "auth_kind": session.auth_kind,
            "runtime_binding_digest": session.runtime_binding_digest,
            "profile_id": session.manifest["profile_id"],
            "profile_digest": session.manifest["profile_digest"],
            "image_digest": session.image_digest,
            "agent_version": self.version,
            "operation_id": session.manifest["operation_id"],
            "session_id": outcome["session_id"],
            "log_digest": outcome["log_digest"],
            "source_digest": digest(source),
            "context_digest": digest(context),
            "verified_at": verified.isoformat(),
            "expires_at": (verified + timedelta(minutes=15)).isoformat(),
            "login_digest": login_digest,
        }

    def diagnostics(self, policy, credential, *, model=None, proof=None):
        try:
            if not valid_model(model):
                raise NativeSessionError("Select a valid native CLI model identifier.")
            connection = self.connection(credential)
            reference = credential["credential"]
            profile = self.selected_profile(
                ExecutionPolicy.from_data(policy), reference["kind"]
            )
            if self.provider != "opencode":
                status = self.client.request(
                    f"/v3/logins/{self.provider}/{connection['credential_id']}"
                )
                if not status.get("available"):
                    raise NativeSessionError(
                        "Subscription login required; connect the selected account inside the runner."
                    )
            ready = False
            if proof:
                account = {
                    "backend": self.provider,
                    "credential_id": reference["id"],
                    "credential_version": reference["version"],
                    "model": model,
                }
                now = datetime.now(timezone.utc)
                ready = all(
                    (
                        proof.get("adapter") == self.provider,
                        proof.get("schema_version") == 1,
                        proof.get("account") == account,
                        proof.get("auth_kind") == reference["kind"],
                        proof.get("authenticated") is True,
                        proof.get("agent_version") == self.version,
                        proof.get("profile_digest") == profile.digest,
                        proof.get("profile_id") == profile.profile_id,
                        proof.get("image_digest") == profile.image,
                        self.provider == "opencode"
                        or proof.get("login_digest") == status.get("login_digest"),
                        proof.get("runtime_binding_digest")
                        == self.client.capabilities()["binding_digest"],
                        datetime.fromisoformat(proof["verified_at"])
                        <= now
                        < datetime.fromisoformat(proof["expires_at"]),
                        (
                            datetime.fromisoformat(proof["expires_at"])
                            - datetime.fromisoformat(proof["verified_at"])
                        ).total_seconds()
                        <= 900,
                    )
                )
            return {
                "native_adapter_available": True,
                "auth_available": True,
                "authenticated": ready,
                "native_agent_ready": ready,
                "agent_version": self.version,
                "profile_digest": profile.digest,
                "reason": (
                    "Native CLI probe passed."
                    if ready
                    else "Selected connection needs a bounded native CLI probe."
                ),
            }
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return {
                "native_adapter_available": True,
                "auth_available": False,
                "authenticated": False,
                "native_agent_ready": False,
                "reason": str(exc)[:500],
            }
