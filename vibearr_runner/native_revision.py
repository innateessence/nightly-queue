"""Bounded publication ancestry without rewriting a delivered branch."""

import hashlib

from .delivery import DeliveryError
from .merge_github import MergeGitHub
from .native_contract import intent_document
from .native_validation import CandidateSource
from .recovery import ReconciliationRequired
from .workspaces import WorkspaceError, git


class NativeRevision:
    def __init__(self, host):
        self.host, self.native = host, host.native

    def prepare(self, reason, detail):
        if self.native["attempt"] >= min(2, self.host.policy.repair_limit):
            raise DeliveryError(
                "Remote CI/base repair exhausted the shared repair allowance: " + detail
            )
        from .native_merge import NativeMerge

        NativeMerge(self.host).terminal()
        delivery = self.native["delivery"]
        workspace = self.native["candidate"]["workspace"]
        grant = self.host.secret("repository")
        github = MergeGitHub(grant["material"]["secret"], read=self.host.read)
        base = (
            github.base_sha(workspace)
            if reason == "base_update"
            else workspace["base_revision"]
        )
        binding = {**self.host.context_binding(), "attempt": self.native["attempt"] + 1}
        payload = intent_document(
            "revision",
            **binding,
            data={
                "publication_id": delivery["publication_id"],
                "workspace_id": self.native["workspace_id"],
                "workspace": workspace,
                "pull_request": delivery["pull_request"],
                "base_revision": base,
                "reason": reason,
            },
        )
        identity = self.host.intent(
            payload, f"revision:{delivery['publication_id']}:{binding['attempt']}"
        )
        self.native["publication_revision"] = {
            "intent_id": identity,
            "intent": payload,
            "detail": detail,
        }
        self.native["phase"] = "publication_revision"
        self.host.save()
        return "continue"

    def step(self):
        from .processes import ProcessTracker

        ProcessTracker(self.host.journal, self.host.state).recover()
        record = self.native["publication_revision"]
        intent = record["intent"]
        if intent["data"]["reason"] == "ci_repair":
            return self.agent_repair(record)
        if not self.host.checkpoint(force_source=True):
            return "wait"
        workspace = self.host.workspace()
        previous = self.native["candidate"]
        old_head = intent["data"]["workspace"]["commit_sha"]
        base = intent["data"]["base_revision"]
        message = f"Merge current base for validated publication\n\nVibearr-Revision: {record['intent_id']}"
        if not record.get("fetched"):
            from .native_delivery import NativeDelivery

            _, transport = NativeDelivery(self.host).clients()
            transport.remote(
                workspace,
                [
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--",
                    workspace.repository_url,
                    base,
                ],
            )
            record["fetched"] = True
            self.host.save()
        head = git(["rev-parse", "HEAD"], workspace.checkout)
        if head == old_head:
            if not self.host.checkpoint(force_source=True):
                return "wait"
            record["merge_started"] = True
            self.host.save()
            try:
                git(
                    [
                        "-c",
                        "user.name=vibearr",
                        "-c",
                        "user.email=runner@localhost",
                        "-c",
                        "commit.gpgsign=false",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        "--no-verify",
                        "--message",
                        message,
                        base,
                    ],
                    workspace.checkout,
                )
            except WorkspaceError:
                if (workspace.checkout / ".git" / "MERGE_HEAD").exists():
                    git(["merge", "--abort"], workspace.checkout)
                raise DeliveryError(
                    "Base update conflicted; preserve the delivered PR and inspect the bounded repair diagnostic."
                ) from None
            head = git(["rev-parse", "HEAD"], workspace.checkout)
        parents = git(
            ["rev-list", "--parents", "-n", "1", head], workspace.checkout
        ).split()[1:]
        if (
            parents != [old_head, base]
            or git(["log", "-1", "--format=%B"], workspace.checkout) != message
        ):
            raise ReconciliationRequired(
                "Base update did not produce the original journaled two-parent merge commit."
            )
        source = CandidateSource.from_git(workspace.checkout, head)
        from vibearr_executor.artifacts import unpack

        files = unpack(source.archive)
        if "requirements.txt" not in files:
            raise DeliveryError(
                "Publication revision removed required validation inputs."
            )
        identity = {
            **previous["workspace"],
            "base_revision": base,
            "commit_sha": head,
            "tree_sha": source.tree_sha,
        }
        payload = {
            "schema_version": 1,
            "intent_id": record["intent_id"],
            "context_id": intent["context_id"],
            "context_digest": intent["context_digest"],
            "attempt": intent["attempt"],
            "previous_workspace_id": intent["data"]["workspace_id"],
            "agent_intent_id": previous["agent_intent_id"],
            "workspace": identity,
            "source_digest": source.digest,
            "export_digest": previous["export_digest"],
            "requirements_digest": hashlib.sha256(
                files["requirements.txt"][1]
            ).hexdigest(),
            "parents": parents,
        }
        new_id = self.host.artifact(
            "native_revision", payload, "revision-workspace:" + record["intent_id"]
        )
        self.native["workspace_id"] = new_id
        self.native["candidate"] = payload
        self.native["attempt"] = intent["attempt"]
        self.native["publication_base"] = base
        for key in ("validation", "dependency", "commit", "merge"):
            self.native.pop(key, None)
        self.native["phase"] = "validation"
        record["activated"] = True
        self.activate_candidate()
        return "continue"

    def agent_repair(self, record):
        if self.native["attempt"] != record["intent"]["attempt"] - 1:
            raise ReconciliationRequired(
                "Remote repair attempt differs from its immutable revision intent."
            )
        self.native["repair_diagnostic"] = record["detail"][-16000:]
        self.native["attempt"] = record["intent"]["attempt"]
        for key in ("agent", "validation", "dependency", "commit", "merge"):
            self.native.pop(key, None)
        self.native["phase"] = "agent"
        record["activated"] = True
        self.host.save()
        return "continue"

    def activate_candidate(self):
        delivery = self.native.get("delivery")
        candidate = self.native["candidate"]["workspace"]
        if (
            not delivery
            or not delivery.get("workspace")
            or delivery["workspace"]["commit_sha"] == candidate["commit_sha"]
        ):
            return
        record = self.native.get("publication_revision")
        if not record or not record.get("activated"):
            raise ReconciliationRequired(
                "Published head changed without a registered publication revision."
            )
        previous = delivery["workspace"]["commit_sha"]
        git(
            ["merge-base", "--is-ancestor", previous, candidate["commit_sha"]],
            self.host.workspace().checkout,
        )
        self.native.setdefault("publication_history", []).append(delivery)
        self.native["delivery"] = {
            "publication_id": delivery["publication_id"],
            "previous_head": previous,
            "existing_pull_number": delivery["pull_request"]["number"],
        }
        self.host.save()
