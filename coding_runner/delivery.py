"""Supervisor-owned delivery; no model output is executable here."""

import json
import os
import re
import subprocess
import tempfile
import threading
import shlex
import sys
import time
from http.client import HTTPException
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from .backends import BackendError
from . import processes
from .client import APIError, AmbiguousRequest, NoRedirects
from .context import repository_path
from .workspaces import Workspace, WorkspaceError, clean_environment, git


class DeliveryError(Exception):
    pass


class DeliveryUnknown(DeliveryError):
    pass


class DeliveryWait(DeliveryError):
    pass


class PullRequestChanged(DeliveryError):
    def __init__(self, pull_request: dict):
        super().__init__(
            "Pull request revision or state changed; operator reconciliation is required."
        )
        self.pull_request = pull_request


def pull_identity(
    value: Any, repository: str, head: str, base: str, commit: str
) -> dict:
    if not isinstance(value, dict):
        raise DeliveryError("GitHub did not return a pull request identity.")
    number = value.get("number")
    url = f"https://github.com/{repository}/pull/{number}"
    valid = type(number) is int and number > 0 and value.get("html_url") == url
    try:
        valid = valid and value["head"]["repo"]["full_name"] == repository
        valid = valid and value["base"]["repo"]["full_name"] == repository
        valid = valid and value["head"]["ref"] == head and value["base"]["ref"] == base
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise DeliveryError(
            "Pull request repository, branch, revision, or URL did not match."
        )
    observed = {
        "number": number,
        "url": url,
        "repository": repository,
        "head": head,
        "base": base,
        "commit_sha": value["head"].get("sha"),
    }
    if observed["commit_sha"] != commit or value.get("state") != "open":
        raise PullRequestChanged(observed)
    return observed


class GitHubDeliveryClient:
    def __init__(self, token: str, *, timeout: float = 15):
        if (
            not token
            or any(character.isspace() for character in token)
            or not 0 < timeout <= 60
        ):
            raise DeliveryError("Invalid repository credential or request limits.")
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.request_lock = threading.Lock()

    def request(self, method: str, path: str, payload: dict | None = None):
        if not path.startswith("/repos/") or "#" in path or "\n" in path:
            raise DeliveryError("Invalid GitHub API path.")
        body = (
            None if payload is None else json.dumps(payload, allow_nan=False).encode()
        )
        if body is not None and len(body) > 65536:
            raise DeliveryError("Pull request description exceeds request limits.")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            "https://api.github.com" + path, data=body, headers=headers, method=method
        )
        deadline = time.monotonic() + self.timeout
        if not self.request_lock.acquire(blocking=False):
            self.unavailable(method)
        finished = threading.Event()
        outcome: list[Any] = []

        def send():
            try:
                outcome.append(self.send(request, method, deadline))
            except Exception as exc:
                outcome.append(exc)
            finally:
                self.request_lock.release()
                finished.set()

        # One worker bounds DNS and headers without accumulating timed-out requests.
        threading.Thread(target=send, daemon=True).start()
        if not finished.wait(max(0, deadline - time.monotonic())):
            self.unavailable(method)
        result = outcome[0]
        if isinstance(result, Exception):
            raise result
        return result

    def unavailable(self, method: str) -> NoReturn:
        error = DeliveryUnknown if method != "GET" else DeliveryError
        raise error(
            "GitHub response unavailable; reconcile delivery before retrying."
        ) from None

    def send(self, request: Request, method: str, deadline: float):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if (
                    response.status not in {200, 201}
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError
                raw = bytearray()
                while True:
                    chunk = response.read1(min(65536, 1048577 - len(raw)))
                    raw.extend(chunk)
                    if len(raw) > 1048576 or time.monotonic() > deadline:
                        raise ValueError
                    if not chunk:
                        break
                return json.loads(raw)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if method != "GET" and status >= 500:
                raise DeliveryUnknown(
                    "GitHub mutation outcome requires reconciliation."
                ) from None
            raise DeliveryError(f"GitHub delivery returned HTTP {status}.") from None
        except (URLError, OSError, HTTPException, ValueError, RecursionError):
            self.unavailable(method)

    def find(self, repository: str, head: str, base: str, commit: str) -> dict | None:
        owner = repository.split("/")[0]
        query = urlencode(
            {"state": "all", "head": f"{owner}:{head}", "base": base, "per_page": 100}
        )
        values = self.request("GET", f"/repos/{repository}/pulls?{query}")
        if not isinstance(values, list) or len(values) >= 100:
            raise DeliveryError("Pull request lookup is invalid or incomplete.")
        if len(values) > 1:
            raise DeliveryError(
                "Multiple pull requests require operator reconciliation."
            )
        return (
            pull_identity(values[0], repository, head, base, commit) if values else None
        )

    def create(
        self, repository: str, head: str, base: str, commit: str, title: str, body: str
    ) -> dict:
        payload = {
            "title": title[:200],
            "body": body,
            "head": head,
            "base": base,
            "draft": True,
        }
        value = self.request("POST", f"/repos/{repository}/pulls", payload)
        try:
            return pull_identity(value, repository, head, base, commit)
        except PullRequestChanged:
            raise
        except DeliveryError:
            raise DeliveryUnknown(
                "Created pull request identity is unavailable; reconcile delivery before retrying."
            ) from None


class SupervisorGit:
    def __init__(self, *, token: str = "", trusted_local: bool = False):
        self.token = token
        self.trusted_local = trusted_local

    def remote(self, workspace: Workspace, arguments: list[str]) -> str:
        return self.command(
            arguments, workspace.repository_url, workspace.directory, workspace.checkout
        )

    def command(
        self, arguments: list[str], repository_url: str, directory, cwd=None
    ) -> str:
        if not self.trusted_local:
            repository_path(repository_url)
            if (
                not self.token
                or len(self.token) > 65536
                or any(character.isspace() for character in self.token)
            ):
                raise DeliveryError("A selected repository credential is required.")
        credential = tempfile.TemporaryFile(dir=directory)
        descriptor = credential.fileno()
        try:
            credential.write(self.token.encode())
            credential.flush()
            helper_path = shlex.quote(os.path.dirname(__file__) + "/git_credential.py")
            scope = repository_path(repository_url).lstrip("/")
            interpreter = shlex.quote(sys.executable)
            helper = f"!{interpreter} -I {helper_path} {os.getpid()} {descriptor} {shlex.quote(scope)}"
            command = [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "http.followRedirects=false",
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=" + helper,
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.file.allow=" + ("always" if self.trusted_local else "never"),
                "-c",
                "core.sshCommand=/usr/bin/false",
            ]
            result = processes.run(
                command + arguments,
                cwd=cwd,
                env=clean_environment(),
                capture_output=True,
                timeout=120,
            )
            if result.returncode or len(result.stdout) > 1048576:
                raise DeliveryError(
                    "Repository operation failed; branch and any pull request are preserved."
                )
            return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            raise DeliveryUnknown(
                "Repository operation outcome requires reconciliation."
            ) from None
        finally:
            credential.close()

    def observed_remote(self, workspace: Workspace) -> str:
        output = self.remote(
            workspace,
            [
                "ls-remote",
                "--refs",
                "--",
                workspace.repository_url,
                "refs/heads/" + workspace.branch_name,
            ],
        )
        if not output:
            return ""
        expected = "refs/heads/" + workspace.branch_name
        entries = output.splitlines()
        if len(entries) != 1 or entries[0].split()[1:] != [expected]:
            raise DeliveryError("Remote branch lookup was inconsistent.")
        revision = entries[0].split()[0]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise DeliveryError("Remote branch revision was invalid.")
        return revision

    def push(self, workspace: Workspace, commit: str, *, checkpoint=None):
        remote = self.observed_remote(workspace)
        if remote == commit:
            return
        if checkpoint is not None:
            checkpoint()
        # Push itself remains a non-forced fast-forward check, including races.
        self.remote(
            workspace,
            [
                "push",
                "--porcelain",
                "--",
                workspace.repository_url,
                commit + ":refs/heads/" + workspace.branch_name,
            ],
        )
        if self.observed_remote(workspace) != commit:
            raise DeliveryUnknown("Pushed branch could not be verified.")


def description(plan: dict, evidence: dict) -> str:
    lines = ["## Problem", plan["problem"], "", "## Implementation"]
    approach = plan.get("approach", "Implemented the saved plan.")
    lines.append(approach if isinstance(approach, str) else json.dumps(approach))
    lines.extend(["", "## Validation"])
    for entry in evidence["validation"]:
        lines.append(
            f"- {entry['phase']}: exit {entry['exit_code']}; evidence {entry['output_digest']}"
        )
    issue = plan.get("original_issue")
    if issue:
        lines.extend(
            [
                "",
                "Original issue: "
                + (issue if isinstance(issue, str) else json.dumps(issue)),
            ]
        )
    return "\n".join(lines)


class DeliveryStage:
    def __init__(
        self, journal, client, github, *, repository_git=None, gate=None, verify=None
    ):
        self.journal = journal
        self.client = client
        self.github = github
        self.repository_git = repository_git or SupervisorGit()
        self.gate = gate
        self.verify = verify

    def save(self, state: dict, delivery: dict):
        state["delivery"] = delivery
        self.journal.save(state)

    def allowed(self):
        if self.gate is None or not self.gate():
            raise DeliveryWait("Delivery is waiting for current execution gates.")

    def step(
        self,
        state: dict,
        run_id: str,
        workspace: Workspace,
        evidence: dict,
        plan: dict,
        claim_token: str,
    ) -> dict:
        delivery = state.setdefault(
            "delivery",
            {"run_id": run_id, "phase": "ready", "operation_id": str(uuid4())},
        )
        if delivery["run_id"] != run_id:
            raise DeliveryError("Delivery journal belongs to another run.")
        if delivery.get("phase") in {"delivered", "reporting_pending", "finished"}:
            return self.report(state, run_id, claim_token)
        try:
            self.allowed()
            with workspace.locked():
                return self.deliver(
                    state, run_id, workspace, evidence, plan, claim_token
                )
        except (DeliveryError, APIError, WorkspaceError, BackendError) as exc:
            if isinstance(exc, PullRequestChanged):
                delivery["pull_request"] = exc.pull_request
            delivery["error"] = str(exc)
            self.save(state, delivery)
            return {
                "status": (
                    "wait"
                    if isinstance(exc, DeliveryWait)
                    else (
                        "reconciliation"
                        if isinstance(exc, (DeliveryUnknown, AmbiguousRequest))
                        else "failed"
                    )
                ),
                "error": str(exc),
                "pull_request": delivery.get("pull_request"),
            }

    def deliver(self, state, run_id, workspace, evidence, plan, claim_token):
        if self.verify is None:
            from .implementation import verify_delivery_ready

            self.verify = verify_delivery_ready
        delivery = state["delivery"]
        repository = repository_path(workspace.repository_url).lstrip("/")
        current = git(["rev-parse", "HEAD"], workspace.checkout)
        committed = delivery.get("commit_sha")
        intent = delivery.get("commit_intent")
        if intent and not committed and current != intent["parent"]:
            tree = git(["rev-parse", "HEAD^{tree}"], workspace.checkout)
            parent = git(["rev-parse", "HEAD^"], workspace.checkout)
            message = git(["log", "-1", "--format=%B"], workspace.checkout)
            if (
                tree != intent["tree"]
                or parent != intent["parent"]
                or message != intent["message"]
            ):
                raise DeliveryError(
                    "Unexpected commit requires operator reconciliation."
                )
            committed = current
            delivery["commit_sha"] = committed
            self.save(state, delivery)
        self.verify(workspace, evidence, committed_head=committed, already_locked=True)
        if not committed:
            self.allowed()
            git(
                ["-c", "core.autocrlf=false", "add", "--all", "--", "."],
                workspace.checkout,
            )
            tree = git(["write-tree"], workspace.checkout)
            message = (
                "Implement queued task\n\nRunner-Delivery: " + delivery["operation_id"]
            )
            delivery["commit_intent"] = {
                "parent": current,
                "tree": tree,
                "message": message,
            }
            delivery["phase"] = "commit_pending"
            self.save(state, delivery)
            self.allowed()
            git(
                [
                    "-c",
                    "user.name=Nightly Queue",
                    "-c",
                    "user.email=runner@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--no-verify",
                    "--message",
                    message,
                ],
                workspace.checkout,
            )
            committed = git(["rev-parse", "HEAD"], workspace.checkout)
            delivery["commit_sha"] = committed
            self.save(state, delivery)
        self.verify(workspace, evidence, committed_head=committed, already_locked=True)
        self.allowed()
        delivery["phase"] = "push_pending"
        self.save(state, delivery)
        self.repository_git.push(workspace, committed, checkpoint=self.allowed)
        delivery["phase"] = "pushed"
        self.save(state, delivery)
        self.allowed()
        pull = self.github.find(
            repository, workspace.branch_name, workspace.resolved_base_branch, committed
        )
        if pull is None:
            if delivery.get("create_unknown"):
                raise DeliveryUnknown(
                    "Pull request creation remains unknown; retry lookup or request operator reconciliation."
                )
            self.allowed()
            delivery["phase"] = "pr_pending"
            delivery["create_unknown"] = True
            self.save(state, delivery)
            pull = self.github.create(
                repository,
                workspace.branch_name,
                workspace.resolved_base_branch,
                committed,
                plan["problem"],
                description(plan, evidence),
            )
        delivery["pull_request"] = pull
        delivery["phase"] = "pr_verified"
        self.save(state, delivery)
        self.verify(workspace, evidence, committed_head=committed, already_locked=True)
        payload = {
            "schema_version": 1,
            "implementation": evidence,
            "repository": repository,
            "branch": workspace.branch_name,
            "base_branch": workspace.resolved_base_branch,
            "base_revision": workspace.base_revision,
            "commit_sha": committed,
            "tree_sha": git(["rev-parse", "HEAD^{tree}"], workspace.checkout),
            "pull_request": pull,
            "pushed_revision": committed,
        }
        delivery["evidence"] = payload
        delivery["phase"] = "evidence_pending"
        self.save(state, delivery)
        self.client.request(
            "POST",
            f"/api/v1/runner/runs/{run_id}/artifacts/",
            {
                "claim_token": claim_token,
                "operation_id": delivery["operation_id"],
                "kind": "verified_delivery",
                "data": payload,
            },
        )
        delivery["phase"] = "delivered"
        self.save(state, delivery)
        return self.report(state, run_id, claim_token)

    def report(self, state: dict, run_id: str, claim_token: str) -> dict:
        delivery = state["delivery"]
        if delivery["phase"] == "finished":
            return {"status": "finished", "pull_request": delivery["pull_request"]}
        delivery["phase"] = "reporting_pending"
        self.save(state, delivery)
        url = delivery["pull_request"]["url"]
        try:
            remote = self.client.reconcile(run_id)["run"]
            if remote["state"] == "completed":
                if (
                    remote["result_status"] != "succeeded"
                    or remote["result_url"] != url
                ):
                    raise DeliveryError(
                        "Remote completion conflicts with verified delivery."
                    )
            else:
                self.client.request(
                    "POST",
                    f"/api/v1/runner/runs/{run_id}/finish/",
                    {
                        "claim_token": claim_token,
                        "status": "succeeded",
                        "result_url": url,
                        "error_summary": "",
                    },
                )
        except DeliveryError as exc:
            delivery["error"] = str(exc)
            self.save(state, delivery)
            return {
                "status": "reconciliation",
                "error": str(exc),
                "pull_request": delivery["pull_request"],
            }
        except APIError:
            return {
                "status": "reporting_pending",
                "pull_request": delivery["pull_request"],
            }
        delivery["phase"] = "finished"
        self.save(state, delivery)
        return {"status": "finished", "pull_request": delivery["pull_request"]}
