"""Subscription and local-model CLIs in the integrated Docker runner."""

from datetime import datetime, timedelta, timezone
import json

from vibearr_executor.contracts import Manifest, Profile, canonical, digest
from vibearr_executor.integrated_profiles import RUNTIME, VERSIONS

from .native_codex import (
    AUTH_PROMPT,
    MAX_OUTPUT,
    CodexEvents,
    NativeCodexAdapter,
    auth_source,
)
from .native_sessions import NativeSession, NativeSessionError
from .policy import ExecutionPolicy
from .opencode_connection import valid_model


class NativeEvents(CodexEvents):
    def __init__(self, provider):
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
        if self.provider == "codex":
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
        if not line.strip():
            return
        if len(self.events) >= 2048:
            raise NativeSessionError("Native CLI event count exceeds its limit.")
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            self.events.append({"kind": "diagnostic", "text": "Native CLI diagnostic."})
            return
        if not isinstance(event, dict):
            raise NativeSessionError("Native CLI events must be objects.")
        identity = (
            event.get("session_id")
            if self.provider == "claude"
            else event.get("sessionID")
        )
        if identity:
            if (
                not isinstance(identity, str)
                or len(identity) > 200
                or self.session_id
                and self.session_id != identity
            ):
                raise NativeSessionError("Native CLI session identity changed.")
            if not self.session_id:
                self.events.append({"kind": "session.started", "session_id": identity})
            self.session_id = identity
        kind = event.get("type", "diagnostic")
        if kind == "error" or event.get("is_error") is True:
            self.failed = True
            self.failure_reason = self.classify_failure(event)
        if self.provider == "claude" and kind == "result":
            self.completed = (
                event.get("subtype") == "success" and event.get("is_error") is False
            )
            self.failed = not self.completed
            usage = event.get("usage", {})
            if isinstance(usage, dict):
                self.usage = {
                    key: value
                    for key, value in usage.items()
                    if type(value) is int and 0 <= value < 2**63
                }
        part = event.get("part", {})
        if not isinstance(part, dict):
            raise NativeSessionError("Native CLI event part must be an object.")
        if (
            self.provider == "opencode"
            and kind == "step_finish"
            and isinstance(part, dict)
        ):
            self.completed = part.get("reason") == "stop"
            tokens = part.get("tokens", {})
            if isinstance(tokens, dict):
                self.usage = {
                    key: value
                    for key, value in tokens.items()
                    if type(value) is int and 0 <= value < 2**63
                }
        normalized = {"kind": str(kind)[:80], "session_id": self.session_id}
        if self.failure_reason:
            normalized["text"] = self.failure_reason
        elif (
            self.provider == "opencode"
            and kind == "text"
            and isinstance(part.get("text"), str)
        ):
            normalized["text"] = part["text"][:4000]
        elif self.provider == "claude" and kind == "assistant":
            message = event.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if isinstance(content, list):
                normalized["text"] = " ".join(
                    item["text"]
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                )[:4000]
        self.events.append(normalized)


class NativeCLIAdapter(NativeCodexAdapter):
    def __init__(self, client, provider):
        if provider not in VERSIONS:
            raise NativeSessionError("Unsupported native CLI.")
        super().__init__(client)
        self.provider = provider
        self.version = VERSIONS[provider]

    def connection(self, credential):
        reference = credential["credential"]
        required = "connection" if self.provider == "opencode" else "login"
        if reference["provider"] != self.provider or reference["kind"] != required:
            raise NativeSessionError(
                "Select a runner subscription login or external Ollama connection."
            )
        if self.provider == "opencode":
            from vibearr_executor.ollama_relay import external_endpoint

            connection = json.loads(credential["material"]["secret"])
            if connection["provider"] != "ollama" or connection.get("api_key"):
                raise NativeSessionError(
                    "OpenCode requires external Ollama without cloud API keys."
                )
            return {
                "provider": "opencode",
                "endpoint": external_endpoint(connection["base_url"]),
                "credential_id": reference["id"],
            }
        return {"provider": self.provider, "credential_id": reference["id"]}

    def selected_profile(self, policy, _kind):
        capabilities = self.client.capabilities()
        if (
            capabilities.get("runtime")
            not in {
                RUNTIME,
                "compact-worker-v1",
            }
            or capabilities.get("qualified") is not True
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

    def context(self, account, prompt, kind):
        return {
            "schema_version": 1,
            "adapter": self.provider,
            "model": account["model"],
            "prompt": prompt,
            "auth_kind": kind,
            "account_digest": digest(canonical(account)),
            "credential_id": account["credential_id"],
        }

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
        context = canonical(self.context(account, prompt, reference["kind"]))
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
        self.client.supply_secret(operation_id, json.dumps(connection))
        return NativeSession.from_data(
            {
                "adapter": self.provider,
                "agent_version": self.version,
                "account": account,
                "auth_kind": reference["kind"],
                "image_digest": profile.image,
                "runtime_binding_digest": capabilities["binding_digest"],
                "manifest": manifest,
                "execution_identity": prepared["execution_identity"],
            }
        )

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

    def outcome(self, session, source):
        result = super().outcome(session, source)
        result["agent_version"] = self.version
        return result

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
        verified = datetime.fromtimestamp(outcome["result"]["ended_at"], timezone.utc)
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
