"""Read-only reconciliation of historical automatic merge dispatches."""

import time
from uuid import uuid4

from .delivery import DeliveryError, DeliveryUnknown, DeliveryRejected
from .merge_contract import completion_marker
from .merge_github import (
    MergeGitHub,
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
        raise DeliveryError(
            "Automatic merge is disabled; a human must merge the pull request."
        )

    def wait(self, reason):
        interval = self.host.snapshot.get("global", {}).get(
            "polling_interval_seconds", 30
        )
        self.data["next_poll_at"] = time.time() + max(15, min(300, interval))
        self.host.save()
        self.host.status("waiting", reason=reason, validation_status="passed")
        return "wait"

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
        self.data["checkpoint"] = "historical_merge_observed"
        self.host.save()
        self.host.status(
            "waiting",
            reason="Historical automatic merge verified; operator reconciliation is required.",
            validation_status="passed",
        )
        return "wait"

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
        marker = completion_marker(
            self.host.run_id, self.native.get("delivery_recovery_generation")
        )
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
            body = f"{marker}\nMerged {self.pull['url']} as `{self.data['merge']['commit_sha']}` after exact-head validation and required checks."
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
