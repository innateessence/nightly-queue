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
from email.utils import parsedate_to_datetime
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from . import processes
from .client import NoRedirects
from .context import repository_path
from .workspaces import Workspace, clean_environment


class DeliveryError(Exception):
    pass


class DeliveryRejected(DeliveryError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub delivery returned HTTP {status}.")


class DeliveryUnknown(DeliveryError):
    pass


class DeliveryWait(DeliveryError):
    pass


class DeliveryReadUnavailable(DeliveryError):
    def __init__(self, message, *, retry_at=0):
        super().__init__(message)
        self.retry_at = retry_at


def github_retry_at(headers):
    values = [0.0]
    retry = headers.get("Retry-After", "")
    try:
        values.append(time.time() + max(0, float(retry)))
    except (ValueError, TypeError):
        try:
            values.append(parsedate_to_datetime(retry).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            values.append(float(headers.get("X-RateLimit-Reset", "")))
        except (ValueError, TypeError):
            pass
    return max(value for value in values if 0 <= value < float("inf"))


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
    def __init__(self, token: str, *, timeout: float = 15, read_only=False):
        if (
            not token
            or any(character.isspace() for character in token)
            or not 0 < timeout <= 60
        ):
            raise DeliveryError("Invalid repository credential or request limits.")
        self.token = token
        self.timeout = timeout
        self.read_only = read_only
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.request_lock = threading.Lock()

    def request(self, method: str, path: str, payload: dict | None = None):
        numeric_repository = method == "GET" and re.fullmatch(
            r"/repositories/[1-9][0-9]*", path
        )
        if (
            (
                not path.startswith("/repos/")
                and not numeric_repository
                and not (method == "GET" and path == "/user")
            )
            or "#" in path
            or "\n" in path
        ):
            raise DeliveryError("Invalid GitHub API path.")
        if self.read_only and method != "GET":
            raise DeliveryError(
                "Reconciliation credentials permit read-only operations."
            )
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
        error = DeliveryUnknown if method != "GET" else DeliveryReadUnavailable
        raise error(
            "GitHub response unavailable; reconcile delivery before retrying."
        ) from None

    def send(self, request: Request, method: str, deadline: float):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if method == "DELETE" and response.status == 204:
                    return None
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
            retry_at = github_retry_at(exc.headers)
            limited = (
                status == 429
                or status == 403
                and (
                    exc.headers.get("Retry-After") is not None
                    or exc.headers.get("X-RateLimit-Remaining") == "0"
                )
            )
            exc.close()
            if method == "GET" and (status >= 500 or limited):
                raise DeliveryReadUnavailable(
                    f"GitHub read returned HTTP {status}.", retry_at=retry_at
                ) from None
            if method != "GET" and status >= 500:
                raise DeliveryUnknown(
                    "GitHub mutation outcome requires reconciliation."
                ) from None
            raise DeliveryRejected(status) from None
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
        self,
        repository: str,
        head: str,
        base: str,
        commit: str,
        title: str,
        body: str,
        *,
        draft=True,
    ) -> dict:
        payload = {
            "title": title[:200],
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
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
    def __init__(
        self,
        *,
        token: str = "",
        trusted_local: bool = False,
        read_only=False,
        read=None,
    ):
        self.token = token
        self.trusted_local = trusted_local
        self.read_only = read_only
        self.read = read

    def remote(
        self, workspace: Workspace, arguments: list[str], *, on_launch=None
    ) -> str:
        launch = {} if on_launch is None else {"on_launch": on_launch}
        return self.command(
            arguments,
            workspace.repository_url,
            workspace.directory,
            workspace.checkout,
            **launch,
        )

    def command(
        self,
        arguments: list[str],
        repository_url: str,
        directory,
        cwd=None,
        *,
        on_launch=None,
    ) -> str:
        if self.read and arguments and arguments[0] in {"ls-remote", "clone", "fetch"}:
            key = "git:" + json.dumps([repository_url, arguments])
            return self.read(
                key, lambda: self._command(arguments, repository_url, directory, cwd)
            )
        launch = {} if on_launch is None else {"on_launch": on_launch}
        return self._command(arguments, repository_url, directory, cwd, **launch)

    def _command(self, arguments, repository_url, directory, cwd, *, on_launch=None):
        if self.read_only and (
            not arguments or arguments[0] not in {"ls-remote", "clone", "fetch"}
        ):
            raise DeliveryError(
                "Reconciliation credentials permit read-only Git transport."
            )
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
            launch = {} if on_launch is None else {"on_launch": on_launch}
            attempt = processes.current_read_attempt.get()
            if attempt is not None and arguments[0] in {"clone", "fetch", "ls-remote"}:
                launch = {
                    "on_launch": attempt.launched,
                    "on_no_launch": attempt.rejected,
                }
            result = processes.run(
                command + arguments,
                cwd=cwd,
                env=clean_environment(),
                capture_output=True,
                timeout=120,
                **launch,
            )
            if result.returncode or len(result.stdout) > 1048576:
                diagnostics = result.stderr.lower()
                transient = any(
                    value in diagnostics
                    for value in (
                        b"could not resolve",
                        b"failed to connect",
                        b"connection reset",
                        b"connection timed out",
                        b"operation timed out",
                        b"http 500",
                        b"http 502",
                        b"http 503",
                        b"http 504",
                        b"error: 500",
                        b"error: 502",
                        b"error: 503",
                        b"error: 504",
                    )
                )
                if arguments[0] in {"ls-remote", "clone", "fetch"} and transient:
                    raise DeliveryReadUnavailable(
                        "Git remote read is temporarily unavailable."
                    )
                raise DeliveryError(
                    "Repository operation failed; branch and any pull request are preserved."
                )
            return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            if arguments[0] in {"ls-remote", "clone", "fetch"}:
                raise DeliveryReadUnavailable(
                    "Git remote read is temporarily unavailable."
                ) from None
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
        self.push_once(workspace, commit)
        if self.observed_remote(workspace) != commit:
            raise DeliveryUnknown("Pushed branch could not be verified.")

    def push_once(self, workspace: Workspace, commit: str, *, on_launch=None):
        # Push itself remains a non-forced fast-forward check, including races.
        launch = {} if on_launch is None else {"on_launch": on_launch}
        self.remote(
            workspace,
            [
                "push",
                "--porcelain",
                "--",
                workspace.repository_url,
                commit + ":refs/heads/" + workspace.branch_name,
            ],
            **launch,
        )
