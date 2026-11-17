"""Fresh, fixed-profile validation of one exact committed candidate tree."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import os
import json
import math
import base64
import subprocess
import selectors
import time
from uuid import UUID, uuid5

from .policy import ExecutionPolicy


def load_validation_profile(path: Path | None = None):
    from vibearr_executor.contracts import Rejected
    from vibearr_executor.validation_profile import ValidationProfile

    selected = (
        str(path)
        if path is not None
        else os.environ.get("VIBEARR_VALIDATION_PROFILE_FILE", "")
    )
    if not selected:
        raise Rejected(
            "Set VIBEARR_VALIDATION_PROFILE_FILE to a private operator-owned validation profile JSON file."
        )
    descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_mode & 0o077 or info.st_size > 65536:
            raise Rejected("Validation profile file must be private and bounded.")
        return ValidationProfile.parse(json.loads(stream.read()))


def validation_diagnostics(policy, client=None, profile=None, *, capabilities=None):
    try:
        from vibearr_executor.client import Client, ExecutorUnavailable
        from vibearr_executor.contracts import Rejected
        from vibearr_executor import SLOT_PROTOCOL_VERSION, SLOT_QUALIFICATION_VERSION

        selected = (
            policy
            if isinstance(policy, ExecutionPolicy)
            else ExecutionPolicy.from_data(policy)
        )
        profile = profile or load_validation_profile()
        if profile.profile_id != selected.validation_profile:
            raise Rejected(
                "Configured validation recipe differs from the selected execution policy."
            )
        observed = capabilities or (client or Client.from_environment()).capabilities()
        if (
            observed.get("schema_version") != 1
            or observed.get("protocol_version") != SLOT_PROTOCOL_VERSION
            or observed.get("qualification_version") != SLOT_QUALIFICATION_VERSION
            or observed.get("runtime") != "compose-slot-v2"
        ):
            raise Rejected(
                "The installed executor validation protocol or qualification version is unsupported."
            )
        if (
            observed.get("qualified") is not True
            or observed.get("accepting_work") is not True
        ):
            raise Rejected("Executor qualification or capacity blocks validation.")
        installed = {
            entry["profile_id"]: entry for entry in observed.get("profiles", [])
        }
        for fixed in profile.executor_profiles():
            actual = installed.get(fixed.profile_id)
            if not actual or actual.get("profile_digest") != fixed.digest:
                raise Rejected(
                    "Register and qualify every required validation command/profile on the executor."
                )
        return {
            "ready": True,
            "reason": "All required offline validation commands are installed and qualified.",
            "profile": profile.data(),
            "profile_digest": profile.digest,
        }
    except ImportError as exc:
        return {"ready": False, "reason": str(exc)}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ExecutorUnavailable,
    ) as exc:
        return {"ready": False, "reason": str(exc)}


@dataclass(frozen=True)
class CandidateSource:
    commit_sha: str
    tree_sha: str
    archive: bytes

    @property
    def digest(self) -> str:
        import hashlib

        return hashlib.sha256(self.archive).hexdigest()

    @classmethod
    def from_git(cls, checkout: Path, commit_sha: str) -> "CandidateSource":
        from vibearr_executor.artifacts import pack
        from vibearr_executor.contracts import MAX_ARCHIVE, MAX_FILES, Rejected

        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit_sha):
            raise Rejected("Select an exact candidate commit SHA.")

        def git(*arguments: str) -> bytes:
            environment = {
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("GIT_")
                },
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1",
            }
            limit = MAX_ARCHIVE if arguments[:2] == ("cat-file", "blob") else 1024
            if arguments[0] == "ls-tree":
                limit = MAX_FILES * 640
            process = subprocess.Popen(
                [
                    "git",
                    "--no-replace-objects",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(checkout),
                    *arguments,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            selector = selectors.DefaultSelector()
            try:
                assert process.stdout is not None
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 60
                content = bytearray()
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Rejected(
                            "Exact candidate Git read exceeded its deadline."
                        )
                    for key, _ in selector.select(min(0.2, remaining)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        content.extend(chunk)
                        if len(content) > limit:
                            raise Rejected(
                                "Git source metadata exceeds its byte limit."
                            )
                if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                    raise Rejected("The exact candidate Git object is unavailable.")
                return bytes(content)
            finally:
                selector.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()

        actual = git("rev-parse", "--verify", commit_sha + "^{commit}").decode().strip()
        if actual != commit_sha:
            raise Rejected("Candidate identity is not an exact commit.")
        tree = git("rev-parse", commit_sha + "^{tree}").decode().strip()
        entries = git("ls-tree", "-rz", "--full-tree", commit_sha).split(b"\0")
        files: dict[str, tuple[int, bytes]] = {}
        size = 0
        for entry in filter(None, entries):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split()
            if mode not in {"100644", "100755"} or kind != "blob":
                raise Rejected("Candidate source cannot contain links or submodules.")
            declared = int(git("cat-file", "-s", blob))
            size += declared
            if size > MAX_ARCHIVE or len(files) >= MAX_FILES:
                raise Rejected("Committed candidate exceeds source limits.")
            content = git("cat-file", "blob", blob)
            if len(content) != declared:
                raise Rejected("Committed candidate blob changed.")
            files[name.decode("utf-8")] = (int(mode, 8) & 0o777, content)
        return cls(commit_sha, tree, pack(files))


class ValidationRunner:
    def __init__(self, client):
        self.client = client
        self.verified_exports: dict[str, str] = {}

    def prepare(
        self,
        *,
        run_id: str,
        attempt_id: str,
        source: CandidateSource,
        profile,
        policy: ExecutionPolicy,
        deadline: str,
    ) -> dict:
        from vibearr_executor.contracts import Limits, Rejected, identifier

        identifier(run_id)
        identifier(attempt_id)
        if policy.validation_profile != profile.profile_id:
            raise Rejected("Validation profile differs from the frozen policy.")
        capabilities = self.client.capabilities()
        diagnostic = validation_diagnostics(
            policy, self.client, profile, capabilities=capabilities
        )
        if not diagnostic["ready"]:
            raise Rejected(diagnostic["reason"])
        registered = {value["profile_id"]: value for value in capabilities["profiles"]}
        if not capabilities["accepting_work"]:
            raise Rejected("Runtime qualification or capacity blocks validation.")
        from vibearr_executor.artifacts import unpack
        from vibearr_executor.contracts import digest

        requirements = unpack(source.archive).get("requirements.txt")
        if requirements is None or digest(requirements[1]) != profile.dependency_digest:
            raise Rejected("Validation image does not bind the candidate dependencies.")
        self.client.upload("source", source.archive)
        context = {
            "schema_version": 1,
            "kind": "validation",
            "tree_sha": source.tree_sha,
            "source_digest": source.digest,
            "validation_profile_digest": profile.digest,
        }
        from vibearr_executor.contracts import canonical

        context_digest = self.client.upload("context", canonical(context))["sha256"]
        operations = []
        from vibearr_executor.validation_profile import CHECK_IDS

        for check_id, fixed in zip(CHECK_IDS, profile.executor_profiles()):
            if (
                registered.get(fixed.profile_id, {}).get("profile_digest")
                != fixed.digest
            ):
                raise Rejected(
                    "The exact validation recipe/image is not registered and qualified."
                )
            limits = Limits(
                cpu=min(policy.cpu_limit, fixed.limits.cpu),
                memory_mb=min(policy.memory_mb, fixed.limits.memory_mb),
                pids=min(policy.pids_limit, fixed.limits.pids),
                workspace_mb=min(policy.workspace_mb, fixed.limits.workspace_mb),
            )
            Limits.parse(asdict(limits))
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "operation_id": str(uuid5(UUID(attempt_id), check_id)),
                "profile_id": fixed.profile_id,
                "profile_digest": fixed.digest,
                "source_digest": source.digest,
                "context_digest": context_digest,
                "phase": "validation",
                "limits": asdict(limits),
                "deadline": deadline,
                "network_hosts": [],
            }
            self.client.prepare(manifest)
            operations.append({"id": check_id, "manifest": manifest})
        return {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "profile": profile.data(),
            "profile_digest": profile.digest,
            "tree_sha": source.tree_sha,
            "source_digest": source.digest,
            "operations": operations,
        }

    @staticmethod
    def checked(session: dict) -> list[dict]:
        from vibearr_executor.contracts import (
            Rejected,
            canonical,
            digest,
            identifier,
            sha256,
        )
        from vibearr_executor.validation_profile import CHECK_IDS, ValidationProfile

        expected = {
            "schema_version",
            "run_id",
            "attempt_id",
            "profile",
            "profile_digest",
            "tree_sha",
            "source_digest",
            "operations",
        }
        if (
            not isinstance(session, dict)
            or set(session) != expected
            or session["schema_version"] != 1
        ):
            raise Rejected("Supply the complete frozen validation session.")
        profile = ValidationProfile.parse(session["profile"])
        if profile.digest != session["profile_digest"] or not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", session["tree_sha"]
        ):
            raise Rejected("Validation profile or tree identity changed.")
        identifier(session["run_id"])
        identifier(session["attempt_id"])
        sha256(session["source_digest"])
        entries = session["operations"]
        if not isinstance(entries, list) or [
            value.get("id") for value in entries
        ] != list(CHECK_IDS):
            raise Rejected("Validation must include every required check in order.")
        context_digest = digest(
            canonical(
                {
                    "schema_version": 1,
                    "kind": "validation",
                    "tree_sha": session["tree_sha"],
                    "source_digest": session["source_digest"],
                    "validation_profile_digest": profile.digest,
                }
            )
        )
        for entry, fixed in zip(entries, profile.executor_profiles()):
            if set(entry) != {"id", "manifest"}:
                raise Rejected("Validation command identity changed.")
            manifest = entry["manifest"]
            values = {
                "run_id": session["run_id"],
                "operation_id": str(uuid5(UUID(session["attempt_id"]), entry["id"])),
                "profile_id": fixed.profile_id,
                "profile_digest": fixed.digest,
                "phase": "validation",
                "source_digest": session["source_digest"],
                "network_hosts": [],
                "schema_version": 1,
            }
            if not isinstance(manifest, dict) or any(
                manifest.get(key) != value for key, value in values.items()
            ):
                raise Rejected("Validation manifest differs from its required command.")
            sha256(manifest.get("context_digest"))
            if context_digest != manifest["context_digest"]:
                raise Rejected(
                    "Validation context differs from its frozen tree/source/profile."
                )
        return entries

    def status(self, session: dict) -> dict:
        from vibearr_executor.contracts import Rejected, sha256

        checks = []
        for entry in self.checked(session):
            self.client.prepare(entry["manifest"])
            observed = self.client.status(entry["manifest"]["operation_id"])
            if observed.get("operation_id") != entry["manifest"]["operation_id"]:
                raise Rejected(
                    "Validation status belongs to another executor operation."
                )
            if observed["state"] != "exited":
                return {
                    "state": observed["state"],
                    "check_id": entry["id"],
                    "operation": observed,
                }
            result = observed.get("result", {})
            if (
                not isinstance(result, dict)
                or result.get("operation_id") != entry["manifest"]["operation_id"]
                or result.get("run_id") != entry["manifest"]["run_id"]
            ):
                raise Rejected(
                    "Protected validation result differs from its original run/operation."
                )
            if (
                observed.get("descendants_terminated") is not True
                or type(result.get("exit_code")) is not int
                or result.get("exit_code") != 0
                or result.get("timed_out") is not False
                or result.get("canceled") is not False
                or result.get("log_truncated") is not False
                or result.get("error")
                or result.get("export_error")
                or not isinstance(result.get("artifact"), dict)
            ):
                return {
                    "state": "failed",
                    "check_id": entry["id"],
                    "operation": observed,
                }
            operation = entry["manifest"]["operation_id"]
            self.verify_export(entry["manifest"], result["artifact"])
            sha256(result.get("log_digest"))
            try:
                timestamps = [result["started_at"], result["ended_at"]]
                if any(
                    type(value) not in {int, float}
                    or not math.isfinite(value)
                    or value <= 0
                    for value in timestamps
                ):
                    raise ValueError
                if timestamps[0] > timestamps[1]:
                    raise ValueError
                started = datetime.fromtimestamp(
                    timestamps[0], timezone.utc
                ).isoformat()
                ended = datetime.fromtimestamp(timestamps[1], timezone.utc).isoformat()
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise Rejected(
                    "Protected validation timestamps are invalid or unordered."
                ) from None
            checks.append(
                {
                    "id": entry["id"],
                    "operation_id": operation,
                    "exit_code": result["exit_code"],
                    "timed_out": False,
                    "started_at": started,
                    "finished_at": ended,
                    "log_reference": f"executor:{operation}:logs",
                    "log_digest": result["log_digest"],
                }
            )
        return {
            "state": "passed",
            "validation": {
                "profile_id": session["profile"]["profile_id"],
                "profile_digest": session["profile_digest"],
                "image_digest": session["profile"]["image"],
                "tree_sha": session["tree_sha"],
                "checks": checks,
            },
        }

    def verify_export(self, job: dict, protected: dict):
        from vibearr_executor.artifacts import inventory, safe_path
        from vibearr_executor.contracts import (
            MAX_ARCHIVE,
            Rejected,
            canonical,
            digest,
            sha256,
        )

        expected = {
            "schema_version",
            "source_digest",
            "result_digest",
            "files",
            "deleted",
        }
        if (
            set(protected) != expected
            or type(protected["schema_version"]) is not int
            or protected["schema_version"] != 1
            or protected["source_digest"] != job["source_digest"]
        ):
            raise Rejected(
                "Protected validation export is missing or bound to another source."
            )
        sha256(protected["result_digest"])
        signature = digest(canonical(protected))
        operation = job["operation_id"]
        if self.verified_exports.get(operation) == signature:
            return
        response = self.client.operation(operation, "result")
        try:
            content = base64.b64decode(response["content"], validate=True)
            valid = (
                len(content) <= MAX_ARCHIVE
                and canonical(response["manifest"]) == canonical(protected)
                and digest(content) == protected["result_digest"]
                and inventory(content) == protected["files"]
            )
            deleted = protected["deleted"]
            valid = (
                valid and isinstance(deleted, list) and deleted == sorted(set(deleted))
            )
            if valid:
                for name in deleted:
                    safe_path(name)
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise Rejected(
                "Validation export bytes or file inventory differ from the protected result."
            )
        self.verified_exports[operation] = signature

    def start(self, session: dict) -> dict:
        observed = self.status(session)
        if observed["state"] == "not-started":
            self.client.start(observed["operation"]["operation_id"])
            return self.status(session)
        return observed

    def stop(self, session: dict) -> dict:
        operations = self.identities(session)
        for operation in operations:
            self.client.stop(operation)
        return self.reconcile(session)

    @staticmethod
    def identities(session: dict) -> list[str]:
        from vibearr_executor.contracts import Rejected, identifier

        run_id = identifier(session["run_id"])
        attempt = UUID(identifier(session["attempt_id"]))
        entries = session["operations"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
            raise Rejected(
                "Validation recovery requires its bounded original operation list."
            )
        operations = []
        for entry in entries:
            check_id = entry["id"]
            if not isinstance(check_id, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,99}", check_id
            ):
                raise Rejected("Validation recovery command identity is invalid.")
            manifest = entry["manifest"]
            operation = identifier(manifest["operation_id"])
            if (
                manifest["run_id"] != run_id
                or operation != str(uuid5(attempt, check_id))
                or operation in operations
            ):
                raise Rejected(
                    "Validation recovery operation differs from its original run."
                )
            operations.append(operation)
        return operations

    def reconcile(self, session: dict) -> dict:
        operations = [
            self.client.status(operation) for operation in self.identities(session)
        ]
        return {
            "operations": operations,
            "terminated": all(
                value.get("state") == "exited"
                and value.get("descendants_terminated") is True
                for value in operations
            ),
        }


class DependencyBuilder:
    def __init__(self, client):
        self.client = client

    def prepare(self, *, run_id, build_id, source, profile, policy, deadline):
        from vibearr_executor.contracts import Limits, Rejected, identifier
        from vibearr_executor.validation_profile import PACKAGE_HOSTS

        identifier(run_id)
        identifier(build_id)
        if policy.validation_profile != profile.profile_id or not set(
            PACKAGE_HOSTS
        ) <= set(policy.network_hosts):
            raise Rejected(
                "The frozen policy does not authorize this validation dependency build."
            )
        setup = profile.setup_profile()
        limits = Limits(
            cpu=min(policy.cpu_limit, setup.limits.cpu),
            memory_mb=min(policy.memory_mb, setup.limits.memory_mb),
            pids=min(policy.pids_limit, setup.limits.pids),
            workspace_mb=min(policy.workspace_mb, setup.limits.workspace_mb),
        )
        Limits.parse(asdict(limits))
        self.client.upload("source", source.archive)
        request = {
            "schema_version": 1,
            "run_id": run_id,
            "build_id": build_id,
            "source_digest": source.digest,
            "setup_profile_id": setup.profile_id,
            "validation_profile": profile.data(),
            "limits": asdict(limits),
            "deadline": deadline,
            "network_hosts": list(PACKAGE_HOSTS),
        }
        self.client.prepare_dependencies(request)
        return request

    def start(self, request):
        self.client.prepare_dependencies(request)
        return self.client.dependencies(request["build_id"], "start")

    def status(self, request):
        self.client.prepare_dependencies(request)
        return self.client.dependencies(request["build_id"], "status")

    def stop(self, request):
        return self.client.dependencies(request["build_id"], "stop")
