"""Owned JSON review reports on a separate persistent control branch."""

from pathlib import Path
import hashlib
import shutil
from uuid import uuid4

from .delivery import DeliveryError, DeliveryUnknown, DeliveryRejected, SupervisorGit
from .merge_github import MergeGitHub, exact_pull
from .native_contract import canonical, intent_document
from .native_github import native_pull
from .processes import ProcessTracker
from .recovery import ReconciliationRequired
from .review_contract import BUILTIN_ROLES
from .workspaces import Workspace, git, private_directory


class ReviewPublication:
    def __init__(self, host, reviews):
        self.host, self.reviews = host, reviews
        self.data = reviews.setdefault("publication", {})

    def clients(self, *, reconciliation=False):
        grant = self.host.secret("repository", reconciliation=reconciliation)
        token = grant["material"]["secret"]
        return MergeGitHub(
            token, read_only=reconciliation, read=self.host.read
        ), SupervisorGit(token=token, read_only=reconciliation, read=self.host.read)

    def local(self):
        binding = self.data["binding"]
        return Workspace(
            self.host.run_id,
            f"https://github.com/{binding['repository']}.git",
            binding["control_base"],
            binding["control_base"],
            binding["base_revision"],
            binding["branch"],
            Path(self.data["directory"]),
        )

    def prepare(self, profile, github, transport):
        if self.data.get("binding"):
            return
        ProcessTracker(self.host.journal, self.host.state).recover()
        workspace = self.host.native["candidate"]["workspace"]
        roles = {
            value["role"]: value
            for value in profile["reviews"]
            if value["role"] in BUILTIN_ROLES
        }
        configured = roles["independent"]
        if configured["report_repository"] != workspace["repository"]:
            raise DeliveryError(
                "Built-in report namespace differs from the selected stable repository."
            )
        if "publication_id" not in self.data:
            self.data["publication_id"] = str(uuid4())
            self.data["directory"] = str(
                self.host.workspace().directory
                / "review-control"
                / self.data["publication_id"]
            )
            self.host.save()
        directory = private_directory(Path(self.data["directory"]))
        checkout = directory / "checkout"
        if not self.data.get("cloned"):
            ProcessTracker(self.host.journal, self.host.state).recover()
            if checkout.exists():
                if checkout.is_symlink() or not self.data.get("clone_prepared"):
                    raise ReconciliationRequired(
                        "An unregistered review control checkout requires reconciliation."
                    )
                shutil.rmtree(checkout)
            self.data["clone_prepared"] = True
            self.host.save()
            transport.command(
                [
                    "clone",
                    "--no-checkout",
                    "--no-recurse-submodules",
                    "--",
                    f"https://github.com/{workspace['repository']}.git",
                    str(checkout),
                ],
                f"https://github.com/{workspace['repository']}.git",
                directory,
            )
            self.data["cloned"] = True
            self.host.save()
        if "base_revision" not in self.data:
            self.data["base_revision"] = github.base_sha(
                {**workspace, "base_branch": profile["control_base"]}
            )
            self.host.save()
        base = self.data["base_revision"]
        branch = f"vibearr/review-run/{self.host.run_id}/{workspace['commit_sha'][:12]}"
        if not self.data.get("checked_out"):
            transport.command(
                [
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--",
                    f"https://github.com/{workspace['repository']}.git",
                    base,
                ],
                f"https://github.com/{workspace['repository']}.git",
                directory,
                checkout,
            )
            git(["checkout", "-B", branch, base], checkout)
            self.data["checked_out"] = True
            self.host.save()
        prefix = f"{configured['report_prefix']}{self.host.run_id}/{workspace['commit_sha']}/"
        files, reports = [], {}
        for role in BUILTIN_ROLES:
            job = self.reviews["roles"][role]
            for suffix, value in (
                (".json", job["result"]["report"]),
                (".receipt.json", job["result"]),
            ):
                relative = prefix + role + suffix
                target = checkout / relative
                current = target.parent
                while current != checkout:
                    if current.is_symlink():
                        raise DeliveryError(
                            "Linked control report paths are forbidden."
                        )
                    current = current.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    raise DeliveryError("Linked control report files are forbidden.")
                content = canonical(value)
                target.write_bytes(content)
                files.append(
                    {"path": relative, "digest": hashlib.sha256(content).hexdigest()}
                )
                if suffix == ".json":
                    reports[role] = relative
        message = (
            "Record isolated native AI reviews\n\nVibearr-Review-Publication: "
            + self.data["publication_id"]
        )
        commit = self.data.get("commit")
        if commit is None:
            git(["add", "--force", "--", *[value["path"] for value in files]], checkout)
            tree = git(["write-tree"], checkout)
            commit = {"parent": base, "tree": tree, "message": message}
            self.data["commit"] = commit
            self.host.save()
        current = git(["rev-parse", "HEAD"], checkout)
        if current == commit["parent"]:
            git(
                [
                    "-c",
                    "user.name=vibearr",
                    "-c",
                    "user.email=runner@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--no-verify",
                    "--message",
                    message,
                ],
                checkout,
            )
            current = git(["rev-parse", "HEAD"], checkout)
        if (
            git(["rev-parse", "HEAD^"], checkout) != commit["parent"]
            or git(["rev-parse", "HEAD^{tree}"], checkout) != commit["tree"]
            or git(["log", "-1", "--format=%B"], checkout) != message
        ):
            raise ReconciliationRequired(
                "Control report commit differs from its original journaled data tree."
            )
        changed = git(
            ["diff", "--name-only", base, current, "--"], checkout
        ).splitlines()
        if set(changed) != {entry["path"] for entry in files}:
            raise DeliveryError(
                "Control report commit contains changes outside its declared JSON/PNG artifacts."
            )
        self.data["binding"] = {
            "publication_id": self.data["publication_id"],
            "workspace_id": self.host.native["workspace_id"],
            "workspace": workspace,
            "pull_request": self.host.native["delivery"]["pull_request"],
            "review_ids": [
                self.reviews["roles"][role]["fact_id"] for role in BUILTIN_ROLES
            ],
            "repository_id": workspace["repository_id"],
            "repository": workspace["repository"],
            "control_base": profile["control_base"],
            "base_revision": base,
            "branch": branch,
            "commit_sha": current,
            "files": files,
            "report_paths": reports,
        }
        self.data["profile"] = profile
        self.host.save()

    def register(self, stage, role=None):
        key = stage + (":" + role if role else "")
        payload = intent_document(
            "review_publication",
            **self.host.context_binding(),
            data={**self.data["binding"], "stage": stage, "role": role},
        )
        identity = self.host.intent(
            payload, f"review-publication:{self.data['publication_id']}:{key}"
        )
        self.data.setdefault("intents", {})[key] = identity
        self.host.save()
        return key, identity

    def fact(self, key, *, pull=None, status=None):
        payload = {
            "schema_version": 1,
            "intent_id": self.data["intents"][key],
            "publication_id": self.data["publication_id"],
            "stage": key.split(":")[0],
            "pushed_sha": self.data["binding"]["commit_sha"],
            "pull_request": pull,
            "status": status,
        }
        self.host.artifact(
            "native_review_publication",
            payload,
            f"review-publication-fact:{self.data['publication_id']}:{key}",
        )
        self.data.setdefault("facts", {})[key] = payload
        self.host.save()

    def observe_pull(self, github):
        binding = self.data["binding"]
        if self.data.get("pull_request"):
            raw = github.pull(
                binding["repository"], self.data["pull_request"]["number"]
            )
            expected = {
                "repository_id": binding["repository_id"],
                "repository": binding["repository"],
                "branch": binding["branch"],
                "base_branch": binding["control_base"],
                "commit_sha": binding["commit_sha"],
            }
            return exact_pull(raw, expected, self.data["pull_request"]["number"])
        owner = binding["repository"].split("/")[0]
        from urllib.parse import urlencode

        query = urlencode(
            {
                "state": "all",
                "head": owner + ":" + binding["branch"],
                "base": binding["control_base"],
                "per_page": 100,
            }
        )
        values = github.request("GET", f"/repos/{binding['repository']}/pulls?{query}")
        if not isinstance(values, list) or len(values) > 1:
            raise DeliveryError("Control report PR lookup is invalid or ambiguous.")
        if not values:
            return None
        raw = {**values[0], "state": "open"}
        return native_pull(
            raw,
            binding["repository_id"],
            binding["repository"],
            binding["branch"],
            binding["control_base"],
            binding["commit_sha"],
        )

    def status(self, github, role):
        configured = next(
            value for value in self.data["profile"]["reviews"] if value["role"] == role
        )
        binding = self.data["binding"]
        url = f"https://github.com/{binding['repository']}/blob/{binding['commit_sha']}/{binding['report_paths'][role]}"
        values = github.pages(
            f"/repos/{binding['repository']}/commits/{binding['workspace']['commit_sha']}/statuses"
        )
        values = [
            value for value in values if value.get("context") == configured["context"]
        ]
        if not values:
            return None
        latest = max(values, key=lambda value: value.get("id", 0))
        if (
            latest.get("creator", {}).get("id") != configured["creator_id"]
            or latest.get("target_url") != url
            or latest.get("state") != "success"
        ):
            raise ReconciliationRequired(
                "Control review status differs from its owned exact-head report publication."
            )
        return {
            "id": latest["id"],
            "role": role,
            "context": configured["context"],
            "creator_id": configured["creator_id"],
            "target_url": url,
            "state": "success",
        }

    def step(self, profile):
        if self.data.get("complete"):
            return "complete"
        if not self.host.checkpoint(force_source=True):
            return "wait"
        github, transport = self.clients()
        self.prepare(profile, github, transport)
        binding = self.data["binding"]
        local = self.local()
        if "push" not in self.data.get("facts", {}):
            key, _ = self.register("push")
            observed = transport.observed_remote(local)
            if observed and observed != binding["commit_sha"]:
                raise ReconciliationRequired(
                    "Control report branch has an unexpected remote commit."
                )
            if observed != binding["commit_sha"]:
                if self.data.get("push_dispatched"):
                    raise ReconciliationRequired(
                        "Control report push outcome remains unknown; retain its original publication."
                    )
                if not self.host.checkpoint(force_source=True):
                    return "wait"

                def dispatched():
                    self.data["push_dispatched"] = True
                    self.host.save()

                try:
                    transport.push_once(
                        local, binding["commit_sha"], on_launch=dispatched
                    )
                except DeliveryError:
                    if not self.data.get("push_dispatched"):
                        raise DeliveryError(
                            "Control report Git push could not start."
                        ) from None
                    raise ReconciliationRequired(
                        "Control report push outcome is uncertain; reconcile the same owned ref."
                    ) from None
                return "continue"
            self.fact(key)
            return "continue"
        if "create_pr" not in self.data.get("facts", {}):
            key, _ = self.register("create_pr")
            pull = self.observe_pull(github)
            if pull is None:
                if self.data.get("create_dispatched"):
                    raise ReconciliationRequired(
                        "Control PR response was lost; reconcile the same branch before any retry."
                    )
                if not self.host.checkpoint(force_source=True):
                    return "wait"
                self.data["create_dispatched"] = True
                self.host.save()
                try:
                    github.create_native(
                        binding["repository_id"],
                        binding["repository"],
                        binding["branch"],
                        binding["control_base"],
                        binding["commit_sha"],
                        "Native AI review evidence for "
                        + binding["workspace"]["commit_sha"][:12],
                        "Protected independent and primary AI review receipts for original run `"
                        + self.host.run_id
                        + "`. This PR changes only control report data; the active app candidate head is unchanged.",
                    )
                except (DeliveryUnknown, DeliveryRejected):
                    pass
                return "continue"
            if not self.data.get("create_dispatched"):
                raise ReconciliationRequired(
                    "An unowned control report PR cannot be adopted."
                )
            self.data["pull_request"] = pull
            self.host.save()
            self.fact(key, pull=pull)
            return "continue"
        for role in BUILTIN_ROLES:
            key = "status:" + role
            if key in self.data.get("facts", {}):
                continue
            self.register("status", role)
            observed = self.status(github, role)
            if observed:
                if not self.data.get(key + ":dispatched"):
                    raise ReconciliationRequired(
                        "Unowned review status cannot replace the registered publication."
                    )
                self.fact(key, pull=self.data["pull_request"], status=observed)
                return "continue"
            if self.data.get(key + ":dispatched"):
                raise ReconciliationRequired(
                    "Review status outcome remains unknown; do not repeat its mutation."
                )
            configured = next(
                value for value in profile["reviews"] if value["role"] == role
            )
            url = f"https://github.com/{binding['repository']}/blob/{binding['commit_sha']}/{binding['report_paths'][role]}"
            self.data[key + ":dispatched"] = True
            self.host.save()
            try:
                github.request(
                    "POST",
                    f"/repos/{binding['repository']}/statuses/{binding['workspace']['commit_sha']}",
                    {
                        "state": "success",
                        "context": configured["context"],
                        "target_url": url,
                        "description": "Protected native "
                        + role
                        + " AI review: no P2 or higher",
                    },
                )
            except (DeliveryUnknown, DeliveryRejected):
                pass
            return "continue"
        self.data["complete"] = True
        self.host.save()
        return "complete"

    def terminal(self):
        intents = self.data.get("intents", {})
        if not intents:
            return
        github, transport = self.clients(reconciliation=True)
        for key, identity in intents.items():
            if key in self.data.get("facts", {}):
                continue
            flag = (
                key + ":dispatched"
                if key.startswith("status:")
                else "push_dispatched" if key == "push" else "create_dispatched"
            )
            if not self.data.get(flag):
                self.host.artifact(
                    "native_review_publication_terminal",
                    {
                        "schema_version": 1,
                        "intent_id": identity,
                        "state": "not_started",
                    },
                    "unstarted-review-publication:" + identity,
                )
                continue
            if key == "push":
                if (
                    transport.observed_remote(self.local())
                    != self.data["binding"]["commit_sha"]
                ):
                    raise ReconciliationRequired(
                        "Canceled review push remains uncertain; keep the original claim."
                    )
                self.fact(key)
            elif key == "create_pr":
                pull = self.observe_pull(github)
                if pull is None:
                    raise ReconciliationRequired(
                        "Canceled control PR creation remains uncertain."
                    )
                self.fact(key, pull=pull)
            else:
                role = key.split(":")[1]
                observed = self.status(github, role)
                if observed is None:
                    raise ReconciliationRequired(
                        "Canceled review status publication remains uncertain."
                    )
                self.fact(key, pull=self.data.get("pull_request"), status=observed)
