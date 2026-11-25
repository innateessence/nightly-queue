"""Factory roles require authenticated worker evidence, not advertised permissions."""

from copy import deepcopy
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
from uuid import UUID

from .native_sessions import NativeSession, NativeSessionError


FACTORY_ROLES = (
    "intake",
    "specification",
    "build",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
)
MAX_TRANSCRIPT = 1024 * 1024
MAX_RESULT = 256 * 1024
MAX_EVENTS = 2048


def source_access(role):
    if role not in FACTORY_ROLES and role != "browser":
        raise NativeSessionError("Unknown factory role.")
    return "read_write" if role == "build" else "read_only"


def validate_browser_capture(role, value):
    if (
        not isinstance(role, str)
        or role not in {"reviewer-ux", "reviewer-ui"}
        or not isinstance(value, dict)
        or set(value) != {"operation_id", "archive_digest"}
        or not _identifier(value["operation_id"])
        or not _sha(value["archive_digest"])
    ):
        raise NativeSessionError(
            "Factory browser capture reference is invalid for this role."
        )
    return dict(value)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _identifier(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_role_binding(
    entry, role, *, runtime_binding_digest, execution_identity=None
):
    if not isinstance(entry, dict):
        raise NativeSessionError(f"Factory {role} has no qualified worker profile.")
    access = source_access(role)
    proof = entry.get("qualification")
    if (
        entry.get("source_access") != access
        or not isinstance(proof, dict)
        or not isinstance(entry.get("profile_id"), str)
        or not 1 <= len(entry["profile_id"]) <= 200
        or not _sha(entry.get("profile_digest"))
        or not _sha(runtime_binding_digest)
    ):
        raise NativeSessionError(f"Factory {role} source permissions are unqualified.")
    expected = {
        "kind": "worker-boundary",
        "role": role,
        "source_access": access,
        "profile_digest": entry["profile_digest"],
        "runtime_binding_digest": runtime_binding_digest,
    }
    image = proof.get("image_digest")
    if (
        type(proof.get("schema_version")) is not int
        or proof["schema_version"] != 1
        or proof.get("passed") is not True
        or not _sha(proof.get("proof_digest"))
        or not isinstance(image, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
        or not _identifier(proof.get("instance_id"))
        or not _identifier(proof.get("worker_boot"))
        or any(proof.get(key) != value for key, value in expected.items())
    ):
        raise NativeSessionError(f"Factory {role} lacks current worker-boundary proof.")
    if execution_identity is not None and (
        not isinstance(execution_identity, dict)
        or execution_identity.get("binding_digest") != runtime_binding_digest
        or any(
            proof[key] != execution_identity.get(key)
            for key in ("instance_id", "worker_boot")
        )
    ):
        raise NativeSessionError(
            f"Factory {role} permission proof belongs to another worker."
        )
    checks = proof.get("checks")
    required = {"control_paths_denied", "credentials_scoped", "descendants_terminated"}
    required.add("candidate_write_only" if role == "build" else "source_write_denied")
    if role == "browser":
        required.add("browser_rendered")
    if not isinstance(checks, dict) or any(
        checks.get(key) is not True for key in required
    ):
        raise NativeSessionError(f"Factory {role} permission or cleanup probes failed.")
    return deepcopy(entry)


def _require_role(capabilities, role, entry):
    from vibearr_executor.contracts import Profile

    selected = validate_role_binding(
        entry,
        role,
        runtime_binding_digest=capabilities["binding_digest"],
        execution_identity={
            key: capabilities[key]
            for key in ("instance_id", "worker_boot", "binding_digest")
        },
    )
    profiles = capabilities.get("profiles")
    if not isinstance(profiles, list):
        raise NativeSessionError("Factory worker profiles are unavailable.")
    matching = [
        value
        for value in profiles
        if isinstance(value, dict) and value.get("profile_id") == selected["profile_id"]
    ]
    if len(matching) != 1:
        raise NativeSessionError(f"Factory {role} profile is missing or ambiguous.")
    registered = matching[0]
    try:
        profile = Profile.parse(
            {key: value for key, value in registered.items() if key != "profile_digest"}
        )
    except (ValueError, TypeError, KeyError):
        raise NativeSessionError("Factory worker profile is invalid.") from None
    expected_phase = "validation" if role == "browser" else "agent"
    if (
        profile.phase != expected_phase
        or profile.digest != registered.get("profile_digest")
        or profile.digest != selected["profile_digest"]
        or profile.image != selected["qualification"]["image_digest"]
    ):
        raise NativeSessionError(
            f"Factory {role} profile differs from its registration."
        )
    return selected


def require_factory_capabilities(capabilities, roles, *, browser=False):
    if (
        not isinstance(capabilities, dict)
        or capabilities.get("runtime") != "compact-worker-v1"
        or any(
            type(capabilities.get(key)) is not int or capabilities[key] != version
            for key, version in (
                ("schema_version", 1),
                ("protocol_version", 2),
                ("qualification_version", 1),
            )
        )
        or capabilities.get("qualified") is not True
        or not _sha(capabilities.get("binding_digest"))
        or not _identifier(capabilities.get("instance_id"))
        or not _identifier(capabilities.get("worker_boot"))
    ):
        raise NativeSessionError(
            "Factory requires a supported runtime and the current qualified worker boot."
        )
    if not isinstance(roles, (list, tuple, set)) or not roles:
        raise NativeSessionError("Factory requires an explicit role set.")
    installed = capabilities.get("factory_roles")
    if not isinstance(installed, dict):
        raise NativeSessionError("Factory role permission probes are unavailable.")
    selected = {}
    for role in roles:
        source_access(role)
        selected[role] = _require_role(capabilities, role, installed.get(role))
    if browser:
        selected["browser"] = _require_role(
            capabilities, "browser", capabilities.get("factory_browser")
        )
    return selected


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("Nonfinite JSON value")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON value")
    return result


def _json(content):
    try:
        return json.loads(
            content,
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise NativeSessionError(
            "Factory output is malformed or ambiguous JSON."
        ) from None


def _usage(value, provider):
    if not isinstance(value, dict):
        raise NativeSessionError("Factory provider token usage is invalid.")
    fields = {
        "codex": {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        },
        "claude": {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        },
        "opencode": {"input", "output", "reasoning"},
    }[provider]
    result = {}
    for key, amount in value.items():
        if key not in fields:
            continue
        if type(amount) is not int or not 0 <= amount < 2**63:
            raise NativeSessionError("Factory provider token usage is invalid.")
        result[key] = amount
    if provider == "opencode" and "cache" in value:
        cache = value["cache"]
        if not isinstance(cache, dict):
            raise NativeSessionError("Factory provider cache token usage is invalid.")
        for key in ("read", "write"):
            if key not in cache:
                continue
            amount = cache[key]
            if type(amount) is not int or not 0 <= amount < 2**63:
                raise NativeSessionError(
                    "Factory provider cache token usage is invalid."
                )
            result["cache." + key] = amount
    return result


def parse_role_result(provider, transcript):
    if provider not in {"codex", "claude", "opencode"}:
        raise NativeSessionError("Unsupported factory native provider.")
    if not isinstance(transcript, bytes) or not 0 < len(transcript) <= MAX_TRANSCRIPT:
        raise NativeSessionError(
            "Factory native transcript exceeds its bound or is empty."
        )
    lines = [line for line in transcript.splitlines() if line.strip()]
    if len(lines) > MAX_EVENTS:
        raise NativeSessionError("Factory native transcript has too many events.")
    session_id, text, completed, usage = "", "", False, {}
    thread_started = False
    for line in lines:
        event = _json(line)
        if not isinstance(event, dict):
            raise NativeSessionError("Factory native events must be objects.")
        kind = event.get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 200:
            raise NativeSessionError("Factory native event type is invalid.")
        if completed:
            raise NativeSessionError(
                "Factory native stream continued after completion."
            )
        key = "session_id" if provider == "claude" else "sessionID"
        identity = event.get(key)
        if provider == "codex" and kind == "thread.started":
            identity = event.get("thread_id")
            if thread_started or not _identifier(identity):
                raise NativeSessionError("Factory Codex thread identity is invalid.")
            thread_started = True
        if identity is not None:
            if (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 200
                or (session_id and session_id != identity)
            ):
                raise NativeSessionError("Factory native session identity changed.")
            session_id = identity
        if kind in {"error", "turn.failed"} or event.get("is_error") is True:
            raise NativeSessionError(
                "Factory native provider reported a failed operation."
            )
        if provider == "codex":
            if kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise NativeSessionError("Factory Codex item is invalid.")
                if item.get("type") == "agent_message":
                    text = item.get("text")
            if kind == "turn.completed":
                completed, usage = True, _usage(event.get("usage", {}), provider)
        elif provider == "claude":
            if kind == "result":
                if (
                    event.get("subtype") != "success"
                    or event.get("is_error") is not False
                ):
                    raise NativeSessionError("Factory Claude result is not successful.")
                text = event.get("result")
                completed, usage = True, _usage(event.get("usage", {}), provider)
        else:
            part = event.get("part", {})
            if not isinstance(part, dict):
                raise NativeSessionError("Factory OpenCode event part is invalid.")
            if kind == "step_start":
                text = ""
            if kind == "text":
                piece = part.get("text")
                if not isinstance(piece, str):
                    raise NativeSessionError("Factory OpenCode text is invalid.")
                text += piece
            if kind == "step_finish" and part.get("reason") == "stop":
                completed, usage = True, _usage(part.get("tokens", {}), provider)
        try:
            size = len(text.encode()) if isinstance(text, str) else MAX_RESULT + 1
        except UnicodeError:
            size = MAX_RESULT + 1
        if size > MAX_RESULT:
            raise NativeSessionError("Factory native role result exceeds its bound.")
    if not completed or not session_id or not text.strip():
        raise NativeSessionError("Factory native role result is incomplete.")
    payload = _json(text)
    if not isinstance(payload, dict):
        raise NativeSessionError("Factory native role result must be a JSON object.")
    return {
        "session_id": session_id,
        "payload": payload,
        "usage": usage,
        "usage_unit": "tokens",
        "log_digest": hashlib.sha256(transcript).hexdigest(),
    }


class FactoryNativeAdapter:
    def __init__(self, adapter, role, capabilities):
        self.adapter, self.role = adapter, role
        self.capabilities = deepcopy(capabilities)
        self.profile = require_factory_capabilities(capabilities, [role])[role]
        self.prepared = {}

    def _session(self, session):
        session = NativeSession.from_data(session.to_data())
        operation = session.manifest["operation_id"]
        if (
            session.manifest["profile_id"] != self.profile["profile_id"]
            or session.manifest["profile_digest"] != self.profile["profile_digest"]
            or session.runtime_binding_digest != self.capabilities["binding_digest"]
            or session.image_digest != self.profile["qualification"]["image_digest"]
            or session.execution_identity is None
            or session.execution_identity["runtime"] != self.capabilities["runtime"]
            or session.execution_identity["binding_digest"]
            != session.runtime_binding_digest
            or session.execution_identity["instance_id"]
            != self.capabilities["instance_id"]
            or session.execution_identity["worker_boot"]
            != self.capabilities["worker_boot"]
            or (
                operation in self.prepared
                and session.to_data() != self.prepared[operation]
            )
        ):
            raise NativeSessionError("Factory session differs from its qualified role.")
        return session

    def prepare(self, **request):
        request = {**request, "policy": dict(request["policy"])}
        request["policy"]["runtime_profile"] = self.profile["profile_id"]
        request["factory_role"] = self.role
        session = self._session(self.adapter.prepare(**request))
        self.prepared[session.manifest["operation_id"]] = session.to_data()
        return session

    def inspect(self, session):
        session = self._session(session)
        observed = self.adapter.inspect(session)
        if not isinstance(observed, dict):
            raise NativeSessionError("Factory native observation must be an object.")
        if observed.get("operation_id") != session.manifest["operation_id"]:
            raise NativeSessionError("Factory observation changed operation identity.")
        if observed.get("execution_identity") != session.execution_identity:
            raise NativeSessionError("Factory observation changed worker identity.")
        if observed.get("state") not in {"not-started", "running", "unknown", "exited"}:
            raise NativeSessionError("Factory native operation state is invalid.")
        return observed

    def reconcile(self, session):
        return self.inspect(session)

    def start(self, session):
        observed = self.reconcile(session)
        if observed["state"] == "unknown":
            raise NativeSessionError(
                "Factory operation is uncertain; retain its fence."
            )
        if observed["state"] != "not-started":
            return observed
        self.adapter.start(session)
        return self.inspect(session)

    def cancel(self, session):
        self._session(session)
        self.adapter.cancel(session)
        return self.inspect(session)

    def events(self, session):
        self._session(session)
        return self.adapter.events(session)

    def outcome(self, session, source):
        from .native_contract import canonical, receipt_passed, receipt_summary
        from vibearr_executor.artifacts import result_manifest

        self._session(session)
        observed = self.inspect(session)
        receipt = receipt_summary(observed)
        if not receipt_passed(receipt):
            raise NativeSessionError("Factory native receipt did not pass.")
        outcome = self.adapter.outcome(session, source)
        parser, transcript = self.adapter.read_events(session)
        del parser
        structured = parse_role_result(session.adapter, transcript)
        if (
            outcome.get("successful") is not True
            or outcome.get("session_id") != structured["session_id"]
            or outcome.get("agent_version") != session.agent_version
            or canonical(outcome.get("result")) != canonical(observed["result"])
            or structured["log_digest"] != receipt["log_digest"]
            or hashlib.sha256(source).hexdigest() != session.manifest["source_digest"]
        ):
            raise NativeSessionError(
                "Factory role result differs from its protected session."
            )
        artifact = result_manifest(source, outcome["source"])
        if canonical(artifact) != canonical(outcome.get("artifact")) or canonical(
            artifact
        ) != canonical(observed["result"].get("artifact")):
            raise NativeSessionError(
                "Factory role source differs from its protected export."
            )
        if source_access(self.role) == "read_only" and outcome["source"] != source:
            raise NativeSessionError("Read-only factory role changed candidate source.")
        return {**outcome, "role": self.role, "role_result": structured["payload"]}


def executable_probe(provider, *, directory=Path("/usr/local/bin"), timeout=15):
    from vibearr_executor.integrated_profiles import VERSIONS

    if provider not in VERSIONS or type(timeout) is not int or not 1 <= timeout <= 30:
        raise NativeSessionError("Unknown native executable probe or invalid deadline.")
    version = re.escape(VERSIONS[provider])
    expected = {
        "codex": rf"codex-cli {version}",
        "claude": rf"{version}(?: \(Claude Code\))?",
        "opencode": version,
    }[provider]
    command = [str(Path(directory) / provider), "--version"]
    with tempfile.TemporaryDirectory(prefix="factory-cli-probe-") as home:
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
            "XDG_CONFIG_HOME": home + "/config",
            "XDG_DATA_HOME": home + "/data",
            "XDG_CACHE_HOME": home + "/cache",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        }
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = _probe_output(process, timeout)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()
        except (OSError, subprocess.SubprocessError):
            raise NativeSessionError(
                "Pinned native executable probe failed or timed out."
            ) from None
    if process.returncode != 0 or len(stdout) > 65536 or len(stderr) > 65536:
        raise NativeSessionError(
            "Pinned native executable probe did not complete cleanly."
        )
    try:
        output = stdout.decode().strip()
    except UnicodeError:
        raise NativeSessionError(
            "Pinned native executable version is invalid."
        ) from None
    if re.fullmatch(expected, output) is None:
        raise NativeSessionError(
            "Installed native executable differs from its pinned version."
        )
    return {
        "schema_version": 1,
        "kind": "executable-version",
        "provider": provider,
        "agent_version": VERSIONS[provider],
        "output_digest": hashlib.sha256(stdout + stderr).hexdigest(),
        "authenticated": False,
        "worker_boundary_verified": False,
        "live_provider_verified": False,
    }


def _probe_output(process, timeout):
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selected:
        for stream in streams:
            selected.register(stream, selectors.EVENT_READ)
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeSessionError("Pinned native executable probe timed out.")
            for key, _mask in selected.select(min(remaining, 0.1)):
                content = os.read(key.fd, 16384)
                if not content:
                    selected.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(content)
                if sum(len(value) for value in streams.values()) > 65536:
                    raise NativeSessionError(
                        "Pinned native executable output exceeds its bound."
                    )
    process.wait(timeout=max(deadline - time.monotonic(), 0.001))
    return bytes(streams[process.stdout]), bytes(streams[process.stderr])


def main():
    parser = argparse.ArgumentParser(
        description="Probe a pinned CLI without authenticating or running a model."
    )
    parser.add_argument(
        "--provider", choices=("codex", "claude", "opencode"), required=True
    )
    parser.add_argument("--version-probe", action="store_true", required=True)
    arguments = parser.parse_args()
    print(json.dumps(executable_probe(arguments.provider), sort_keys=True))


if __name__ == "__main__":
    main()
