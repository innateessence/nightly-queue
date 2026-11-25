"""Pre-publication base changes create immutable generations within one budget."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from .budgets import BudgetExceeded
from .native_contract import digest, sha, validate_context
from .native_validation import CandidateSource
from .recovery import ReconciliationRequired
from .workspaces import WorkspaceManager, git


class PublicationBaseChanged(ReconciliationRequired):
    def __init__(self, workspace, new_base):
        self.workspace = dict(workspace)
        self.old_base = workspace["base_revision"]
        self.new_base = new_base
        super().__init__(
            "Publication base changed; a fresh inspected candidate and review chain are required."
        )


class FactoryBaseUpdate:
    def __init__(self, native):
        self.host, self.native = native, native.native

    def unpublished(self):
        if self.native.get("delivery") or self.native.get("publication_history"):
            raise ReconciliationRequired(
                "Reconcile the original publication before any factory base repair."
            )
        if any(
            value.get("kind") not in {"agent", "validation", "dependency"}
            for value in self.native.get("intents", {}).values()
        ):
            raise ReconciliationRequired(
                "Prepared publication operations retain their original identity and fence."
            )
        if any(
            value.get("kind") in {"native_delivery", "verified_delivery"}
            for value in self.native.get("metadata", {}).values()
        ):
            raise ReconciliationRequired(
                "Delivered factory work requires an explicit new attempt."
            )

    def prepare(self, observation):
        if not isinstance(observation, PublicationBaseChanged):
            raise ReconciliationRequired(
                "Factory base repair requires a verified provider observation."
            )
        self.unpublished()
        candidate, context = self.native["candidate"], self.native["context"]
        if (
            observation.workspace != candidate["workspace"]
            or observation.old_base
            != context["document"]["repository"]["base_revision"]
        ):
            raise ReconciliationRequired(
                "Factory base observation belongs to another candidate or context."
            )
        sha(observation.new_base, git=True)
        if observation.new_base == observation.old_base:
            raise ReconciliationRequired(
                "Factory base repair requires an actual changed base."
            )
        request = {
            "previous_context_id": context["id"],
            "previous_context_digest": context["digest"],
            "previous_workspace_id": self.native["workspace_id"],
            "attempt": self.native["attempt"] + 1,
            "old_base": observation.old_base,
            "new_base": observation.new_base,
            "reason": "base_update",
        }
        pending = self.native.get("factory_base_update")
        if pending is not None and pending["generation"] != request:
            raise ReconciliationRequired(
                "Reconcile the original factory base observation before replacing it."
            )
        self.native.setdefault(
            "factory_base_update",
            {
                "generation": request,
                "stage": "cleanup",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self.native["phase"] = "factory_base_update"
        self.host.save()
        return "continue"

    def step(self):
        if not self.host.checkpoint(force_source=True):
            return "wait"
        self.unpublished()
        request = self.native["factory_base_update"]
        if request["stage"] == "cleanup":
            self.cleanup(request)
        if request["stage"] != "checkout":
            raise ReconciliationRequired("Unsupported factory base-generation stage.")
        workspace = self.prepare_workspace(request)
        source = CandidateSource.from_git(workspace.checkout, workspace.base_revision)
        previous = self.native["context"]["document"]
        document = deepcopy(previous)
        document.update(
            source_digest=source.digest,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            factory_generation=deepcopy(request["generation"]),
        )
        document["repository"].update(
            base_revision=workspace.base_revision,
            base_tree_sha=source.tree_sha,
        )
        pending = request.setdefault(
            "context_request",
            {
                "operation_id": str(uuid4()),
                "document": validate_context(document),
            },
        )
        self.host.save()
        saved = self.host.metadata_request(
            f"/api/v1/runner/runs/{self.host.run_id}/contexts/",
            {
                "claim_token": self.host.token,
                **pending,
                "base_commit": workspace.base_revision,
                "resolved_base_branch": workspace.resolved_base_branch,
                "reason": "Bounded factory base update.",
            },
        )["context"]
        if saved.get("document") != pending["document"] or saved.get(
            "digest"
        ) != digest(pending["document"]):
            raise ReconciliationRequired(
                "Replacement context differs from its immutable base-generation request."
            )
        recovery = self.native["factory"]["recovery"]
        previous_specification = deepcopy(
            self.native["factory"]["artifacts"]["specification"]
        )
        self.native["context"] = saved
        self.native["workspace"] = {
            **asdict(workspace),
            "directory": str(workspace.directory),
        }
        for key in (
            "plan",
            "plan_request",
            "context_request",
            "agent",
            "validation",
            "dependency",
            "commit",
            "candidate",
            "workspace_id",
            "import",
        ):
            self.native.pop(key, None)
        self.native.get("metadata", {}).pop("termination", None)
        self.native.pop("delivery", None)
        self.native["factory"] = {
            "schema_version": 1,
            "stage": "intake",
            "round": self.native["attempt"] + 1,
            "roles": {},
            "artifacts": {},
            "recovery": recovery,
            "previous_specification": previous_specification,
        }
        request["stage"] = "completed"
        self.native.setdefault("factory_generations", [])[-1]["new_context"] = deepcopy(
            saved
        )
        self.native.pop("factory_base_update")
        self.native["phase"] = "factory"
        self.host.save()
        return "continue"

    def cleanup(self, request):
        from .factory_recovery import FactoryRecovery

        recovery = FactoryRecovery(self.host, self.native["factory"])
        recovery.stop()
        if self.native["attempt"] >= recovery.limit:
            raise BudgetExceeded("Factory shared repair allowance is exhausted.")
        if recovery.budget.remaining("implementation") <= 0:
            raise BudgetExceeded("Factory aggregate agent budget is exhausted.")
        if request["generation"]["attempt"] != self.native["attempt"] + 1:
            raise ReconciliationRequired(
                "Factory base generation differs from its shared repair allowance."
            )
        if "prior_diff" not in request:
            candidate = self.native["candidate"]["workspace"]
            changes = git(
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--binary",
                    candidate["base_revision"],
                    candidate["commit_sha"],
                    "--",
                ],
                self.host.workspace().checkout,
            )
            if len(changes.encode()) > 256 * 1024:
                raise ReconciliationRequired(
                    "Prior factory changes exceed the bounded repair prompt; preserve the original candidate."
                )
            request["prior_diff"] = changes
            self.host.save()
        self.host.terminate()
        archive = {
            key: deepcopy(self.native.get(key))
            for key in (
                "context",
                "plan",
                "workspace",
                "workspace_id",
                "candidate",
                "factory",
                "clone",
                "validation",
                "agent",
            )
        }
        archive.update(
            generation=deepcopy(request["generation"]), prior_diff=request["prior_diff"]
        )
        self.native.setdefault("factory_generations", []).append(archive)
        attempt = request["generation"]["attempt"]
        recovery.data["repairs"].append(
            {
                "attempt": attempt,
                "reason": "base_update",
                "trigger": deepcopy(request["generation"]),
                "evidence": {
                    "workspace": deepcopy(self.native["candidate"]["workspace"]),
                    "observed_at": request["observed_at"],
                },
            }
        )
        self.native["factory_base_changes"] = {
            "generation": deepcopy(request["generation"]),
            "prior_candidate": deepcopy(self.native["candidate"]),
            "prior_diff": request["prior_diff"],
        }
        self.native["repair_diagnostic"] = (
            "The protected base advanced; rebuild the prior requested changes against the new base.\n"
            + request["prior_diff"]
        )
        self.native["attempt"] = attempt
        self.native["factory"]["round"] = attempt + 1
        self.native.pop("clone", None)
        request["stage"] = "checkout"
        self.host.save()

    def prepare_workspace(self, request):
        from .native_clone import NativeClone
        from .native_lifecycle import SupervisorGit

        previous = self.host.host.workspaces
        directory = (
            previous.directory
            / "factory-generations"
            / str(request["generation"]["attempt"])
        )
        manager = WorkspaceManager(directory, require_new_branch=True)
        item = {
            **self.host.run["item"],
            "repository_url": self.native["context"]["document"]["repository"][
                "repository_url"
            ],
        }
        token = self.host.secret("repository")["material"]["secret"]
        clone = NativeClone(
            self.host, manager, SupervisorGit(token=token, read=self.host.read), item
        )
        manager.resume_clone = clone.recover()
        manager.remote_git = clone.command
        workspace = manager.prepare(self.host.run_id, item)
        if workspace.base_revision != request["generation"]["new_base"]:
            raise ReconciliationRequired(
                "The base changed again while preparing the frozen factory generation."
            )
        return workspace
