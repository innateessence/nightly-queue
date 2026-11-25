"""One journaled publication, reconciled before any repeated mutation."""

from dataclasses import replace
import time
from uuid import uuid4

from .delivery import DeliveryError, DeliveryUnknown, SupervisorGit
from .merge_github import MergeGitHub
from .native_sessions import NativeSession
from .processes import current_execution
from .recovery import ReconciliationRequired
from .workspaces import git
from .factory_delivery import (
    PublicationBaseChanged,
    factory_delivery_barrier,
    factory_enabled,
    require_human_delivery,
)


class NativeDelivery:
    def __init__(self, lifecycle):
        self.host = lifecycle
        self.native = lifecycle.native
        self.data = self.native.setdefault("delivery", {})

    def clients(self, *, reconciliation=False):
        grant = self.host.secret("repository", reconciliation=reconciliation)
        token = grant["material"]["secret"]
        return MergeGitHub(
            token, read_only=reconciliation, read=self.host.read
        ), SupervisorGit(token=token, read_only=reconciliation, read=self.host.read)

    def identity(self, github):
        workspace = self.native["candidate"]["workspace"]
        identity, _ = github.repository(repository_id=workspace["repository_id"])
        current = self.native["repository"]
        if identity["repository_url"] != current["repository_url"]:
            raise ReconciliationRequired(
                "Repository namespace changed; refresh source authority before publication."
            )
        frozen = self.native.get(
            "repository_at_checkout", self.native["context"]["document"]["repository"]
        )
        expected_default = frozen.get("default_branch", frozen.get("base_branch"))
        if identity["default_branch"] != expected_default:
            raise DeliveryError(
                "Repository default branch changed; retry with a fresh task context."
            )
        current_base = github.base_sha(workspace)
        if current_base != workspace["base_revision"]:
            raise PublicationBaseChanged(workspace, current_base)
        return {**workspace, "repository": identity["repository"]}, replace(
            self.host.workspace(), repository_url=identity["repository_url"]
        )

    def verify(self):
        from .native_validation import CandidateSource

        workspace = self.host.workspace()
        candidate = self.native["candidate"]
        head = git(["rev-parse", "HEAD"], workspace.checkout)
        if head != candidate["workspace"]["commit_sha"]:
            raise DeliveryError(
                "Publication checkout differs from the validated commit."
            )
        exact = CandidateSource.from_git(workspace.checkout, head)
        if (
            exact.digest != candidate["source_digest"]
            or exact.tree_sha != self.native["validation"]["evidence"]["tree_sha"]
        ):
            raise DeliveryError("Publication tree differs from independent validation.")
        if git(["status", "--porcelain", "--untracked-files=all"], workspace.checkout):
            raise DeliveryError("Publication checkout contains uncommitted changes.")
        factory_delivery_barrier(self.host)

    def register(self, stage, workspace):
        from .native_contract import delivery_intent

        if "publication_id" not in self.data:
            self.data["publication_id"] = str(uuid4())
            self.host.save()
        payload = delivery_intent(
            **self.host.context_binding(),
            publication_id=self.data["publication_id"],
            stage=stage,
            workspace_id=self.native["workspace_id"],
            workspace=workspace,
        )
        identity = self.host.intent(
            payload, f"publication:{self.data['publication_id']}:{stage}"
        )
        self.data.setdefault("intents", {})[stage] = identity
        self.data.setdefault("workspace", workspace)
        if self.data["workspace"] != workspace:
            raise ReconciliationRequired(
                "Publication namespace changed after its immutable intent."
            )
        self.host.save()
        return identity

    def fact(self, workspace, *, pull=None):
        from .native_contract import delivery_record

        payload = delivery_record(
            intent_ids=(
                list(self.data["intents"].values())
                if pull
                else [self.data["intents"]["push"]]
            ),
            workspace_id=self.native["workspace_id"],
            publication_id=self.data["publication_id"],
            workspace=workspace,
            pushed_sha=workspace["commit_sha"],
            pull_request=pull,
        )
        suffix = "pr" if pull else "push"
        if self.native.get("publication_history"):
            suffix = workspace["commit_sha"] + ":" + suffix
        self.host.artifact("native_delivery", payload, "publication-fact:" + suffix)

    def step(self):
        try:
            return self._step()
        except PublicationBaseChanged as observation:
            if not factory_enabled(getattr(self.host, "snapshot", {})):
                raise
            from .factory_recovery import FactoryRecovery

            return FactoryRecovery(self.host, self.native["factory"]).base_changed(
                observation
            )

    def _step(self):
        require_human_delivery(self.host.policy)
        if self.data.get(
            "checkpoint"
        ) == "reporting_pending" or "termination" in self.native.get("metadata", {}):
            self.host.terminate()
            return self.report()
        if not self.host.checkpoint(force_source=True):
            return "wait"
        self.verify()
        github, transport = self.clients()
        workspace, checkout = self.identity(github)
        commit = workspace["commit_sha"]
        self.host.status(
            "delivery", detail="Publishing the independently validated commit."
        )
        self.register("push", workspace)
        remote = transport.observed_remote(checkout)
        previous_head = self.data.get("previous_head")
        if remote and remote not in {commit, previous_head}:
            raise ReconciliationRequired(
                "Owned task branch contains an unexpected remote commit."
            )
        if remote != commit:
            if self.data.get("push_dispatched"):
                raise ReconciliationRequired(
                    "Push response was lost and its ref is not visible; retain publication ownership."
                )
            if not self.host.checkpoint(force_source=True):
                return "wait"
            self.verify()
            self.identity(github)

            def dispatched():
                self.data["push_dispatched"] = True
                self.host.save()

            try:
                transport.push_once(checkout, commit, on_launch=dispatched)
            except DeliveryError:
                if not self.data.get("push_dispatched"):
                    raise DeliveryError(
                        "Git push could not start; inspect the selected repository transport."
                    ) from None
                # Even a nonzero Git exit can follow a completed remote receive.
                raise ReconciliationRequired(
                    "Push outcome requires read-only reconciliation of the same ref."
                ) from None
            if transport.observed_remote(checkout) != commit:
                raise ReconciliationRequired(
                    "Pushed branch is not visible; retain the same publication."
                )
        self.data["pushed_sha"] = commit
        self.host.save()
        self.fact(workspace)
        if not self.data.get("checkpoint"):
            self.data["checkpoint"] = "pushed"
            self.host.save()
            return "continue"
        pull = github.find_native(
            workspace["repository"],
            workspace["branch"],
            workspace["base_branch"],
            commit,
            workspace["repository_id"],
        )
        if pull is None:
            self.register("create_pr", workspace)
            if self.data.get("create_dispatched"):
                raise ReconciliationRequired(
                    "PR creation response was lost; retain the original publication and keep looking up its identity."
                )
            if not self.host.checkpoint(force_source=True):
                return "wait"
            self.verify()
            self.identity(github)
            self.data["create_dispatched"] = True
            self.host.save()
            try:
                pull = github.create_native(
                    workspace["repository_id"],
                    workspace["repository"],
                    workspace["branch"],
                    workspace["base_branch"],
                    commit,
                    self.native["context"]["document"]["source"]["title"],
                    self.description(),
                )
            except DeliveryUnknown:
                raise ReconciliationRequired(
                    "PR creation outcome requires lookup of the same branch and commit."
                ) from None
            except DeliveryError:
                self.data["create_rejected"] = True
                self.host.save()
                raise
        elif "create_pr" not in self.data["intents"] and pull[
            "number"
        ] != self.data.get("existing_pull_number"):
            raise ReconciliationRequired(
                "An unowned pre-existing PR cannot be adopted by this run."
            )
        self.data["pull_request"] = pull
        self.host.save()
        self.fact(workspace, pull=pull)
        if self.data.get("checkpoint") == "pushed":
            self.data["checkpoint"] = "pr_verified"
            self.host.save()
            return "continue"
        self.verify()
        evidence = self.evidence(workspace, pull)
        self.host.artifact("verified_delivery", evidence, "verified-delivery")
        self.host.terminate()
        self.data["checkpoint"] = "reporting_pending"
        self.host.save()
        return "continue"

    def report(self):
        require_human_delivery(self.host.policy)
        pull = self.data["pull_request"]
        remote = self.host.client.reconcile(self.host.run_id)["run"]
        if remote["state"] == "completed":
            if (
                remote["result_status"] != "succeeded"
                or remote["result_url"] != pull["url"]
                or remote.get("native_outcome") != "delivered"
            ):
                raise ReconciliationRequired(
                    "Recorded completion conflicts with the original native delivery."
                )
            return "reporting_pending"
        if remote["state"] != "active":
            raise ReconciliationRequired(
                "Native publication no longer owns its completion record."
            )
        factory_delivery_barrier(self.host)
        self.host.status(
            "delivery", detail=pull["url"], validation_status="passed", force=True
        )
        self.host.client.request(
            "POST",
            f"/api/v1/runner/runs/{self.host.run_id}/finish/",
            {
                "claim_token": self.host.token,
                "status": "succeeded",
                "result_url": pull["url"],
                "error_summary": "",
                "outcome": "delivered",
            },
        )
        return "reporting_pending"

    def description(self):
        context = self.native["context"]["document"]
        source = context["source"]
        issue = source.get("issue_url", "")
        if type(source.get("issue_number")) is int and source["issue_number"] > 0:
            repository = self.data["workspace"]["repository"]
            issue = f"https://github.com/{repository}/issues/{source['issue_number']}"
        lines = [
            source["title"],
            "",
            f"Run: `{self.host.run_id}`",
            f"Context: `{self.native['context']['digest']}`",
        ]
        if issue:
            lines.extend(["", "Source: " + issue])
        lines.extend(
            ["", "Validation passed independently for the exact committed tree:"]
        )
        lines.extend(
            "- " + entry["id"]
            for entry in self.native["validation"]["evidence"]["checks"]
        )
        return "\n".join(lines)

    def evidence(self, workspace, pull):
        session = NativeSession.from_data(self.native["agent"]["session"])
        if "evidence_id" not in self.data:
            self.data["evidence_id"] = str(uuid4())
            self.host.save()
        return {
            "schema_version": 2,
            "execution_kind": "native",
            "run_id": self.host.run_id,
            "operation_id": self.data["evidence_id"],
            "runner_id": self.host.state["runner_id"],
            "configuration_revision": self.host.run["run"]["configuration_revision"],
            "policy_digest": self.host.policy.digest,
            "context": {
                "id": self.native["context"]["id"],
                "digest": self.native["context"]["digest"],
            },
            "runtime": {
                "image_digest": session.image_digest,
                "profile_id": session.manifest["profile_id"],
                "agent_version": self.native["agent"]["agent_version"],
                "session_id": self.native["agent"]["session_id"],
            },
            "workspace": workspace,
            "validation": self.native["validation"]["evidence"],
            "delivery": {
                "mode": self.host.policy.delivery_mode,
                "pull_request": pull,
                "pushed_sha": workspace["commit_sha"],
            },
        }

    def reconcile_publications(self):
        if not self.data.get("intents"):
            return []

        def checkpoint():
            return not self.host.host.stop.is_set()

        token = current_execution.set((time.monotonic() + 60, checkpoint, checkpoint))
        try:
            return self._reconcile_publications()
        finally:
            current_execution.reset(token)

    def _reconcile_publications(self):
        facts = {}
        for record in self.native.get("metadata", {}).values():
            if record["kind"] != "native_delivery" or not record.get("acknowledged"):
                continue
            payload = record["payload"]
            for identity in payload["intent_ids"]:
                intent = self.native["intents"].get(identity, {})
                if intent.get("kind") != "delivery":
                    continue
                facts[identity] = {
                    "intent_id": identity,
                    "publication_id": payload["publication_id"],
                    "stage": intent["data"]["stage"],
                    "state": "known_pr" if payload["pull_request"] else "known_push",
                    "pushed_sha": payload["pushed_sha"],
                    "pull_request": payload["pull_request"],
                }
        expected = {
            identity
            for identity, value in self.native.get("intents", {}).items()
            if value["kind"] == "delivery"
        }
        if expected <= facts.keys():
            return list(facts.values())
        if not self.data.get("intents"):
            return []
        if not self.data.get("push_dispatched") and not self.data.get(
            "create_dispatched"
        ):
            return [
                {
                    "intent_id": identity,
                    "publication_id": self.data["publication_id"],
                    "stage": stage,
                    "state": "not_started",
                    "pushed_sha": "",
                    "pull_request": None,
                }
                for stage, identity in self.data["intents"].items()
            ]
        github, transport = self.clients(reconciliation=True)
        workspace, checkout = self.identity(github)
        remote = transport.observed_remote(checkout)
        if remote not in {"", workspace["commit_sha"]}:
            raise ReconciliationRequired(
                "Publication remote ref changed; retain ownership."
            )
        pull = github.find_native(
            workspace["repository"],
            workspace["branch"],
            workspace["base_branch"],
            workspace["commit_sha"],
            workspace["repository_id"],
        )
        if pull and remote != workspace["commit_sha"]:
            raise ReconciliationRequired(
                "Observed PR has no matching current pushed ref."
            )
        if remote:
            if pull:
                self.data["pull_request"] = pull
                self.host.save()
            self.fact(workspace, pull=pull)
        results = []
        for stage, intent_id in self.data["intents"].items():
            dispatched = self.data.get(
                "push_dispatched" if stage == "push" else "create_dispatched"
            )
            if (
                not dispatched
                or stage == "create_pr"
                and self.data.get("create_rejected")
            ):
                state, pushed, observed = "not_started", "", None
            elif pull:
                state, pushed, observed = "known_pr", remote, pull
            elif remote and stage == "push":
                state, pushed, observed = "known_push", remote, None
            else:
                raise ReconciliationRequired(
                    "Publication outcome is unknown; cancellation retains its claim and slot."
                )
            results.append(
                {
                    "intent_id": intent_id,
                    "publication_id": self.data["publication_id"],
                    "stage": stage,
                    "state": state,
                    "pushed_sha": pushed,
                    "pull_request": observed,
                }
            )
        return list(facts.values()) + [
            entry for entry in results if entry["intent_id"] not in facts
        ]
