"""Eligible-work preflight caches actual auth proof, never a readiness toggle."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from .client import APIError
from .delivery import DeliveryError
from .native_github import preflight_repository
from .native_resources import NativeResources
from .native_sessions import NativeBackendRegistry, NativeSession, NativeSessionError
from .policy import ExecutionPolicy
from .workspaces import WorkspaceError


def now():
    return datetime.now(timezone.utc)


class NativePreflight:
    def __init__(
        self,
        client,
        executor,
        journal,
        *,
        registry=None,
        profile=None,
        github_check=preflight_repository,
    ):
        self.client, self.executor, self.journal = client, executor, journal
        self.registry = registry or NativeBackendRegistry()
        self.profile = profile
        self.github_check = github_check
        self.resources = NativeResources(journal.directory)

    def selected_grant(self, project, purpose):
        reference = project["effective"][f"{purpose}_credential_id"]
        if purpose == "implementation":
            payload = {
                "scope": "preflight",
                "backend": project["effective"]["implementation_backend"],
                "credential_id": reference,
            }
        else:
            payload = {
                "scope": "preflight",
                "backend": "github",
                "credential_id": reference,
                "project_id": project["project_id"],
            }
        return self.client.request("POST", "/api/v1/runner/secrets/", payload)

    def select(self, configuration, state):
        from vibearr_executor.client import ExecutorUnavailable
        from .native_validation import validation_diagnostics

        candidates = self.client.request("GET", "/api/v1/runner/candidates/")
        if candidates.get("configuration_revision") != configuration["revision"]:
            return []
        identities = candidates.get("project_ids")
        if not isinstance(identities, list) or any(
            type(value) is not int or value < 1 for value in identities
        ):
            raise NativeSessionError("Source candidate identities are invalid.")
        data = state.setdefault("native_preflight", {"accounts": {}, "projects": {}})
        data["projects"] = {}
        self.settle_pending(data, state, configuration)
        self.controls_owned(state, pause=False)
        ready = []
        pending = False
        for project in configuration["projects"]:
            project_id = project["project_id"]
            if project_id not in identities or project.get("errors"):
                continue
            report = {"ready": False, "reason": "Native preflight has not completed."}
            data["projects"][str(project_id)] = report
            try:
                policy = ExecutionPolicy.from_data(project["execution_policy"])
                validation = validation_diagnostics(policy, self.executor, self.profile)
                if validation.get("ready") is not True:
                    raise NativeSessionError(validation["reason"])
                from .factory_routing import require_factory_capabilities

                require_factory_capabilities(
                    project["effective"].get("factory"), self.executor.capabilities()
                )
                repository_grant = self.selected_grant(project, "repository")
                report["repository"] = self.github_check(repository_grant, project)
                if policy.delivery_mode == "merge":
                    from .merge_contract import merge_profile
                    from .merge_github import MergeGitHub

                    profile = merge_profile(
                        project["effective"].get("merge_profile", {})
                    )
                    github = MergeGitHub(
                        repository_grant["material"]["secret"], read_only=True
                    )
                    identity, _ = github.repository(
                        repository_id=profile["repository_id"]
                    )
                    workspace = {
                        "repository_id": identity["repository_id"],
                        "repository": identity["repository"],
                        "base_branch": project["base_branch"]
                        or identity["default_branch"],
                    }
                    workspace["base_revision"] = github.base_sha(workspace)
                    workspace["commit_sha"] = workspace["base_revision"]
                    report["merge_policy"] = github.policy(workspace, profile)
                self.selected_grant(project, "issue")
                grant = self.selected_grant(project, "implementation")
                provider = project["effective"]["implementation_backend"]
                model = project["effective"]["implementation_model"]
                adapter = self.registry.create(provider, self.executor)
                capabilities = self.executor.capabilities()
                key_data = [
                    provider,
                    grant["credential"]["id"],
                    grant["credential"]["version"],
                    model,
                    policy.runtime_profile,
                    capabilities.get("binding_digest"),
                    capabilities.get("instance_id"),
                ]
                key = hashlib.sha256(json.dumps(key_data).encode()).hexdigest()
                entry = data["accounts"].setdefault(key, {})
                diagnostic = adapter.diagnostics(
                    policy.to_data(), grant, model=model, proof=entry.get("proof")
                )
                if diagnostic.get("native_agent_ready") is True:
                    report.update(ready=True, reason=diagnostic["reason"])
                    ready.append(project_id)
                    continue
                if entry.get("proof"):
                    self.cleanup_probe(entry)
                    entry.clear()
                    diagnostic = adapter.diagnostics(
                        policy.to_data(), grant, model=model
                    )
                if diagnostic.get("auth_available") is not True:
                    raise NativeSessionError(diagnostic["reason"])
                if (
                    entry.get("retry_at")
                    and datetime.fromisoformat(entry["retry_at"]) > now()
                ):
                    raise NativeSessionError(entry["reason"])
                if entry.get("retry_at"):
                    failures = entry.get("failures", 0)
                    if failures >= 3:
                        raise NativeSessionError(
                            "Authentication failed repeatedly; replace the selected credential."
                        )
                    self.cleanup_probe(entry)
                    entry.clear()
                    entry["failures"] = failures
                if pending and not entry.get("session"):
                    report["reason"] = "Another selected account diagnostic is running."
                    continue
                pending = self.probe(entry, adapter, project, grant, state)
                report["reason"] = entry.get(
                    "reason", "Selected account authentication is running."
                )
                if entry.get("proof"):
                    report.update(
                        ready=True,
                        reason="Live selected-account authentication passed.",
                    )
                    ready.append(project_id)
            except (
                APIError,
                DeliveryError,
                WorkspaceError,
                NativeSessionError,
                ExecutorUnavailable,
                ValueError,
                OSError,
            ) as exc:
                report["reason"] = str(exc)[:500]
            self.journal.save(state)
        self.journal.save(state)
        if pending or self.pending_accounts(data):
            return []
        return ready

    @staticmethod
    def pending_accounts(data):
        return any(entry.get("pending") for entry in data["accounts"].values())

    def probe(self, entry, adapter, project, grant, state):
        from vibearr_executor.client import ExecutorUnavailable

        if not entry.get("request"):
            entry["request"] = {
                "run_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "deadline": (now() + timedelta(seconds=120)).isoformat(),
                "model": project["effective"]["implementation_model"],
                "policy": project["execution_policy"],
            }
            entry["pending"] = True
            self.journal.save(state)
        self.resources.bind(entry["request"]["run_id"], self.executor)
        self.resources.reserve(
            entry["request"]["run_id"], entry["request"]["operation_id"]
        )
        try:
            if not entry.get("session"):
                session = adapter.prepare_auth_probe(
                    **entry["request"], credential=grant
                )
                self.resources.operation(
                    session.manifest["run_id"], session.manifest, kind="auth"
                )
                entry["session"] = session.to_data()
                self.journal.save(state)
            session = NativeSession.from_data(entry["session"])
            if not callable(getattr(self.executor, "termination_receipt", None)):
                raise NativeSessionError(
                    "Install the matching executor SDK with protected operation presence readback."
                )
            self.resources.verify_presence(
                self.resources.load(session.manifest["run_id"]),
                session.manifest["operation_id"],
                self.executor,
            )
            self.resources.verify_dispatched(session.manifest["run_id"], self.executor)
            observed = adapter.reconcile(session)
            if observed["state"] == "not-started":
                entry["start_intent"] = True
                self.journal.save(state)
                self.resources.dispatch(
                    session.manifest["run_id"], session.manifest["operation_id"]
                )
                adapter.start(session)
                return True
            if observed["state"] in {"running", "unknown"}:
                entry["reason"] = (
                    "Authentication operation is running or awaiting exact-operation reconciliation."
                )
                return True
            if observed.get("descendants_terminated") is not True:
                entry["reason"] = (
                    "Authentication descendant termination is unconfirmed; retain the original operation."
                )
                self.journal.save(state)
                return True
            entry["pending"] = False
            try:
                entry["proof"] = adapter.authentication_result(session)
                entry["reason"] = "Live selected-account authentication passed."
            except (NativeSessionError, ExecutorUnavailable, ValueError) as exc:
                entry["reason"] = str(exc)[:500]
                entry["retry_at"] = (now() + timedelta(minutes=5)).isoformat()
                entry["failures"] = entry.get("failures", 0) + 1
            self.journal.save(state)
            return False
        except (NativeSessionError, ExecutorUnavailable, OSError) as exc:
            entry["reason"] = str(exc)[:500]
            self.journal.save(state)
            return True

    def settle_pending(self, data, state, configuration):
        from vibearr_executor.client import ExecutorUnavailable

        for entry in data["accounts"].values():
            if not entry.get("pending"):
                continue
            try:
                self.resources.bind(entry["request"]["run_id"], self.executor)
                if not entry.get("session"):
                    request = entry["request"]
                    if now() >= datetime.fromisoformat(request["deadline"]):
                        observed = self.executor.stop(request["operation_id"])
                        entry["pending"] = observed.get("state") not in {
                            "exited",
                            "not-started",
                        }
                    continue
                session = NativeSession.from_data(entry["session"])
                self.resources.verify_dispatched(
                    session.manifest["run_id"], self.executor
                )
                adapter = self.registry.create(session.adapter, self.executor)
                current = self.executor.capabilities()
                selected = any(
                    project["effective"].get("implementation_backend")
                    == session.account["backend"]
                    and project["effective"].get("implementation_credential_id")
                    == session.account["credential_id"]
                    and project["effective"].get("implementation_model")
                    == session.account["model"]
                    for project in configuration["projects"]
                )
                credential = next(
                    (
                        value
                        for value in configuration["credentials"]
                        if value["id"] == session.account["credential_id"]
                    ),
                    {},
                )
                current_credential = credential.get(
                    "secret_version"
                ) == session.account["credential_version"] and not credential.get(
                    "revoked_at"
                )
                if (
                    current.get("binding_digest") != session.runtime_binding_digest
                    or not selected
                    or not current_credential
                ):
                    observed = self.executor.stop(session.manifest["operation_id"])
                    entry["pending"] = not (
                        observed.get("state") == "exited"
                        and observed.get("descendants_terminated") is True
                    )
                    entry["reason"] = (
                        "Selected credential or runtime changed; previous authentication operation is being stopped."
                    )
                    continue
                observed = adapter.reconcile(session)
                if (
                    observed.get("state") != "exited"
                    or observed.get("descendants_terminated") is not True
                ):
                    continue
                entry["pending"] = False
                try:
                    entry["proof"] = adapter.authentication_result(session)
                except (NativeSessionError, ExecutorUnavailable, ValueError) as exc:
                    entry["reason"] = str(exc)[:500]
                    entry["retry_at"] = (now() + timedelta(minutes=5)).isoformat()
                    entry["failures"] = entry.get("failures", 0) + 1
            except (NativeSessionError, ExecutorUnavailable, OSError) as exc:
                entry["reason"] = str(exc)[:500]
        self.journal.save(state)

    def cleanup_probe(self, entry):
        session = entry.get("session")
        if not session:
            return
        record = NativeSession.from_data(session)
        observed = self.executor.status(record.manifest["operation_id"])
        if observed.get("state") != "exited" or not observed.get(
            "descendants_terminated"
        ):
            raise NativeSessionError(
                "Previous authentication resources are not terminated."
            )
        self.executor.purge(
            record.manifest["operation_id"], run_id=record.manifest["run_id"]
        )
        self.resources.path(record.manifest["run_id"]).unlink(missing_ok=True)

    def stop_owned(self, state):
        observations = []
        for entry in state.get("native_preflight", {}).get("accounts", {}).values():
            request = entry.get("request")
            if not request or not entry.get("pending"):
                continue
            operation = request["operation_id"]
            self.resources.bind(request["run_id"], self.executor)
            self.resources.verify_dispatched(request["run_id"], self.executor)
            observed = self.executor.stop(operation)
            observations.append(observed)
            entry["pending"] = not (
                observed.get("state") == "exited"
                and observed.get("descendants_terminated") is True
            )
        self.journal.save(state)
        return observations

    def controls_owned(self, state, *, pause):
        for entry in state.get("native_preflight", {}).get("accounts", {}).values():
            if not entry.get("pending") or not entry.get("session"):
                continue
            self.resources.controls(
                entry["request"]["run_id"], self.executor, pause=pause
            )

    def invalidate_account(self, state, account, reason):
        for entry in state.get("native_preflight", {}).get("accounts", {}).values():
            if entry.get("proof", {}).get("account") == account:
                entry.pop("proof", None)
                entry["reason"] = reason[:500]
                entry["retry_at"] = (now() + timedelta(minutes=5)).isoformat()
        self.journal.save(state)
