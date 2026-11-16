"""One owned merge, followed by idempotent provider completion reporting."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .delivery import DeliveryError, DeliveryUnknown, DeliveryRejected
from .merge_contract import merge_profile
from .merge_github import (
    MergeGitHub,
    MergeRepair,
    MergeWait,
    exact_pull,
    merged_observation,
)
from .native_contract import intent_document
from .recovery import ReconciliationRequired


class NativeMerge:
    def __init__(self, host):
        self.host = host
        self.native = host.native
        self.delivery = self.native["delivery"]
        self.data = self.native.setdefault("merge", {})

    @property
    def workspace(self):
        return self.delivery["workspace"]

    @property
    def pull(self):
        return self.delivery["pull_request"]

    def client(self, *, reconciliation=False, completion=False):
        grant = self.host.secret(
            "repository", reconciliation=reconciliation, completion=completion
        )
        if completion:
            from .merge_github import CompletionGitHub

            return CompletionGitHub(
                grant["material"]["secret"],
                repository=self.workspace["repository"],
                issue_number=self.native["context"]["document"]["source"].get(
                    "issue_number"
                ),
                branch=self.workspace["branch"],
                read=self.host.read,
            )
        return MergeGitHub(
            grant["material"]["secret"], read_only=reconciliation, read=self.host.read
        )

    def register(self, stage):
        payload = intent_document(
            "merge",
            **self.host.context_binding(),
            data={
                "publication_id": self.delivery["publication_id"],
                "workspace_id": self.native["workspace_id"],
                "workspace": self.workspace,
                "pull_request": self.pull,
                "stage": stage,
                "profile": self.data["profile"],
                "receipt": self.data["receipt"],
            },
        )
        if "authorization_id" not in self.data:
            self.data["authorization_id"] = str(uuid4())
            self.host.save()
        identity = self.host.intent(
            payload,
            f"merge:{self.delivery['publication_id']}:{self.data['authorization_id']}:{stage}",
        )
        self.data.setdefault("intents", {})[stage] = identity
        self.host.save()
        return identity

    def step(self):
        if self.data.get("dispatched") or self.data.get("merge"):
            return self.reconcile()
        if not self.host.checkpoint(force_source=True):
            return "wait"
        profile = merge_profile(
            self.host.snapshot["effective"].get("merge_profile", {})
        )
        if self.data.get("profile") not in (None, profile):
            raise ReconciliationRequired(
                "Trusted merge profile changed during this owned run."
            )
        self.data["profile"] = profile
        if "deadline" not in self.data:
            if "ci_started_at" not in self.native:
                raise DeliveryError(
                    "Original server PR publication timestamp is required before CI/merge."
                )
            deadline = datetime.fromisoformat(self.native["ci_started_at"]) + timedelta(
                seconds=profile["ci_timeout_seconds"]
            )
            if "ci_deadline" not in self.native:
                self.native["ci_deadline"] = min(
                    deadline, datetime.fromisoformat(self.native["deadline"])
                ).isoformat()
            self.data["deadline"] = self.native["ci_deadline"]
            self.host.save()
        if datetime.now(timezone.utc) >= datetime.fromisoformat(self.data["deadline"]):
            raise DeliveryError("Required remote checks exceeded the bounded CI wait.")
        github = self.client()
        raw = github.pull(self.workspace["repository"], self.pull["number"])
        exact_pull(raw, self.workspace, self.pull["number"])
        if raw.get("merged"):
            raise ReconciliationRequired(
                "An unrequested merge requires provider reconciliation."
            )
        if raw.get("state") != "open" or raw.get("draft") is not False:
            raise DeliveryError("Automatic merge requires the original open ready PR.")
        try:
            if github.base_sha(self.workspace) != self.workspace["base_revision"]:
                return self.repair(
                    "base_update", "Base branch changed after candidate validation."
                )
            policy = github.policy(self.workspace, profile)
            checks = github.checks(self.workspace, self.pull["number"], profile)
            from .native_reviews import NativeReviews

            progress = NativeReviews(self.host).step(profile)
            if progress != "complete":
                return progress
            reviews = github.reviews(
                self.workspace,
                self.pull["number"],
                profile,
                run_id=self.host.run_id,
                context_digest=self.native["context"]["digest"],
            )
        except MergeWait as exc:
            self.host.status("waiting", reason=str(exc), validation_status="passed")
            return "wait"
        except MergeRepair as exc:
            return self.repair("ci_repair", str(exc))
        if raw.get("mergeable") is None:
            self.host.status(
                "waiting",
                reason="Provider mergeability is still being calculated.",
                validation_status="passed",
            )
            return "wait"
        if raw.get("mergeable") is not True or raw.get("mergeable_state") != "clean":
            return self.repair(
                "ci_repair", "Provider reports a conflict or blocked merge."
            )
        if not self.host.checkpoint(force_source=True):
            return "wait"
        # Reobserve immediately before intent: no candidate or provider result may
        # replace this exact base/head/producer binding after a wait.
        raw = github.pull(self.workspace["repository"], self.pull["number"])
        exact_pull(raw, self.workspace, self.pull["number"])
        if github.base_sha(self.workspace) != self.workspace["base_revision"]:
            return self.repair("base_update", "Base moved before merge dispatch.")
        if (
            raw.get("state") != "open"
            or raw.get("draft")
            or raw.get("merged")
            or raw.get("mergeable") is not True
            or raw.get("mergeable_state") != "clean"
        ):
            raise MergeWait("Provider merge state changed before dispatch.")
        policy = github.policy(self.workspace, profile)
        checks = github.checks(self.workspace, self.pull["number"], profile)
        reviews = github.reviews(
            self.workspace,
            self.pull["number"],
            profile,
            run_id=self.host.run_id,
            context_digest=self.native["context"]["digest"],
        )
        receipt = {"policy": policy, "checks": checks, "reviews": reviews}
        if self.data.get("intents") and self.data.get("receipt") != receipt:
            self.terminal()
            self.data.pop("intents", None)
            self.data.pop("authorization_id", None)
        self.data["receipt"] = receipt
        self.host.save()
        from .native_lifecycle import NativeMetadataRejected

        try:
            self.register("merge")
        except NativeMetadataRejected:
            self.data["authorization_rejections"] = (
                self.data.get("authorization_rejections", 0) + 1
            )
            self.data.pop("authorization_id", None)
            self.host.save()
            if self.data["authorization_rejections"] >= 3:
                raise DeliveryError(
                    "Current server merge authorization was rejected after three bounded attempts."
                ) from None
            if not self.host.checkpoint(force_source=True):
                return "wait"
            if github.base_sha(self.workspace) != self.workspace["base_revision"]:
                return self.repair(
                    "base_update", "Base moved during independent server authorization."
                )
            self.host.status(
                "waiting",
                reason="Provider authorization changed; reread current checks and AI reviews.",
                validation_status="passed",
            )
            return "wait"
        if not self.host.checkpoint(force_source=True):
            return "wait"
        self.host.status(
            "delivery",
            detail="Squash merging the exact validated and AI-reviewed head.",
            validation_status="passed",
            force=True,
        )
        self.data["dispatched"] = True
        self.host.save()
        try:
            github.merge_once(self.workspace, self.pull["number"])
        except (DeliveryUnknown, DeliveryRejected):
            # A405 may be an in-progress provider merge left by a timed-out call.
            # Only observation resolves ownership; this mutation is never replayed.
            pass
        return "continue"

    def reconcile(self):
        github = self.client(reconciliation=True)
        raw = github.pull(self.workspace["repository"], self.pull["number"])
        observed = merged_observation(raw, self.workspace, self.pull["number"])
        if observed is None:
            raise ReconciliationRequired(
                "Merge was dispatched and its provider outcome remains unresolved; retain the same claim and repository fence."
            )
        if self.data.get("merge") not in (None, observed):
            raise ReconciliationRequired(
                "Provider merged identity conflicts with the original merge observation."
            )
        self.data["merge"] = observed
        self.host.save()
        payload = {
            "schema_version": 1,
            "intent_id": self.data["intents"]["merge"],
            "workspace_id": self.native["workspace_id"],
            "publication_id": self.delivery["publication_id"],
            "workspace": self.workspace,
            "pull_request": self.pull,
            "receipt": self.data["receipt"],
            "merge": observed,
        }
        self.host.artifact(
            "native_merge", payload, "merged:" + self.workspace["commit_sha"]
        )
        if self.data.get("checkpoint") != "merged":
            self.data["checkpoint"] = "merged"
            self.host.save()
            return "continue"
        return self.complete()

    def issue(self, github):
        source = self.native["context"]["document"]["source"]
        number = source.get("issue_number")
        issue_id = source.get("metadata", {}).get("issue_id")
        if (
            type(number) is not int
            or number < 1
            or type(issue_id) is not int
            or issue_id < 1
        ):
            raise ReconciliationRequired(
                "Automatic issue completion requires the frozen GitHub source issue identity."
            )
        raw = github.request(
            "GET", f"/repos/{self.workspace['repository']}/issues/{number}"
        )
        if (
            raw.get("id") != issue_id
            or raw.get("number") != number
            or raw.get("pull_request")
        ):
            raise ReconciliationRequired(
                "Provider issue does not match the original source identity."
            )
        return number, issue_id, raw

    def complete(self):
        self.host.status(
            "delivery",
            detail="Reporting verified merge "
            + self.data["merge"]["commit_sha"]
            + " and completing the original source issue.",
            validation_status="passed",
        )
        source = self.native["context"]["document"]["source"]
        if (
            self.host.run.get("item", {}).get("source") == "manual"
            and source.get("issue_number") is None
        ):
            return self.finalize()
        github = self.client(completion=True)
        number, issue_id, issue = self.issue(github)
        marker = f"<!-- vibearr-run:{self.host.run_id}:complete -->"
        comments = github.pages(
            f"/repos/{self.workspace['repository']}/issues/{number}/comments"
        )
        matches = [value for value in comments if marker in value.get("body", "")]
        if len(matches) > 1:
            raise ReconciliationRequired(
                "Completion marker has duplicate provider comments."
            )
        if matches:
            comment = matches[0]
            if comment.get("user", {}).get("id") != self.data["profile"]["actor_id"]:
                raise ReconciliationRequired(
                    "Completion marker was published by an unexpected actor."
                )
            if not self.data.get("comment_dispatched"):
                raise ReconciliationRequired(
                    "Unowned completion marker cannot be adopted."
                )
        else:
            self.register("comment")
            if self.data.get("comment_dispatched"):
                raise ReconciliationRequired(
                    "Completion comment response was lost; reconcile its stable marker before any retry."
                )
            self.data["comment_dispatched"] = True
            self.host.save()
            body = f"{marker}\nMerged {self.pull['url']} as `{self.data['merge']['commit_sha']}` after exact-head validation and AI review."
            try:
                github.request(
                    "POST",
                    f"/repos/{self.workspace['repository']}/issues/{number}/comments",
                    {"body": body},
                )
            except (DeliveryUnknown, DeliveryRejected):
                pass
            return "continue"
        self.data["comment_id"] = comment["id"]
        if issue.get("state") != "closed":
            self.register("close")
            if self.data.get("close_dispatched"):
                raise ReconciliationRequired(
                    "Issue closure outcome remains unresolved; keep reporting ownership."
                )
            self.data["close_dispatched"] = True
            self.host.save()
            try:
                github.request(
                    "PATCH",
                    f"/repos/{self.workspace['repository']}/issues/{number}",
                    {"state": "closed", "state_reason": "completed"},
                )
            except (DeliveryUnknown, DeliveryRejected):
                pass
            return "continue"
        if "close" not in self.data.get("intents", {}):
            self.register("close")
        self.data["issue_completion"] = {
            "operation_id": self.data["intents"]["close"],
            "issue_number": number,
            "state": "closed",
        }
        deleted = self.delete_branch(github)
        if deleted is None:
            return "continue"
        payload = {
            "schema_version": 1,
            "intent_ids": [
                identity
                for stage, identity in self.data["intents"].items()
                if stage != "merge"
            ],
            "merge_intent_id": self.data["intents"]["merge"],
            "issue_number": number,
            "issue_id": issue_id,
            "comment_id": comment["id"],
            "marker": marker,
            "state": "closed",
            "branch_deleted": deleted,
        }
        self.host.artifact("native_completion", payload, "issue-completed")
        return self.finalize()

    def finalize(self):
        self.data["completed"] = True
        self.host.save()
        from .native_delivery import NativeDelivery

        delivery = NativeDelivery(self.host)
        evidence = delivery.evidence(self.workspace, self.pull)
        self.host.artifact("verified_delivery", evidence, "verified-delivery")
        self.host.terminate()
        self.delivery["checkpoint"] = "reporting_pending"
        self.host.save()
        return "continue"

    def delete_branch(self, github):
        if not self.data["profile"]["delete_branch"]:
            return False
        from urllib.parse import quote

        path = f"/repos/{self.workspace['repository']}/git/refs/heads/{quote(self.workspace['branch'], safe='')}"
        try:
            raw = github.request("GET", path)
        except DeliveryRejected as exc:
            if exc.status != 404 or not self.data.get("delete_dispatched"):
                raise
            return True
        if (
            raw.get("object", {}).get("sha") != self.workspace["commit_sha"]
            or self.workspace["branch"] == self.workspace["base_branch"]
        ):
            raise ReconciliationRequired(
                "Owned branch changed before optional deletion."
            )
        self.register("delete")
        if self.data.get("delete_dispatched"):
            raise ReconciliationRequired("Branch deletion remains unresolved.")
        self.data["delete_dispatched"] = True
        self.host.save()
        try:
            github.request("DELETE", path)
        except (DeliveryUnknown, DeliveryRejected):
            pass
        return None

    def repair(self, reason, detail):
        from .native_revision import NativeRevision

        return NativeRevision(self.host).prepare(reason, detail)

    def terminal(self):
        if not self.data.get("intents"):
            return
        if self.data.get("dispatched"):
            if not self.data.get("completed"):
                raise ReconciliationRequired(
                    "Dispatched automatic merge must finish provider reconciliation before releasing ownership."
                )
            return
        for identity in self.data["intents"].values():
            self.host.artifact(
                "native_merge_terminal",
                {"schema_version": 1, "intent_id": identity, "state": "not_started"},
                "unstarted-merge:" + identity,
            )
