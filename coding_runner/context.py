"""Complete issue discussions fetched by the supervisor, never by a backend."""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .client import NoRedirects

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class ContextError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ContextError(f"Invalid {name}.")
    return value


def timestamp(value: Any) -> str:
    text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContextError("Invalid timestamp.") from None
    if parsed.tzinfo is None:
        raise ContextError("Timestamp must include a timezone.")
    return value


def checkout(item: dict[str, Any]) -> dict[str, str]:
    return {
        key: text(item.get(key), key, empty=key == "base_branch")
        for key in ("repository_url", "base_branch", "branch_name")
    }


def repository_path(repository: Any) -> str:
    parsed = urlsplit(text(repository, "issue repository"))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ContextError("Expected a GitHub issue repository URL.")
    path = parsed.path.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise ContextError("Invalid issue repository.")
    return path


def manual_context(
    item: dict[str, Any], *, fetched_at: str | None = None
) -> dict[str, Any]:
    if item.get("source") != "manual":
        raise ContextError("Expected a manual item.")
    return {
        "schema_version": 1,
        "source": "manual",
        "title": text(item.get("title"), "title"),
        "instructions": text(item.get("instructions"), "instructions", empty=True),
        "issue": None,
        "comments": [],
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "checkout": checkout(item),
    }


def discussion_digest(context: dict[str, Any]) -> str:
    return digest({key: value for key, value in context.items() if key != "fetched_at"})


class GitHubContextClient:
    def __init__(self, token: str, *, timeout: float = 15, max_pages: int = 1000):
        if not token or any(character.isspace() for character in token):
            raise ContextError("An issue-read credential is required.")
        if not 0 < timeout <= 60 or not 1 <= max_pages <= 1000:
            raise ContextError("Invalid context request limits.")
        self.token = token
        self.timeout = timeout
        self.max_pages = max_pages
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def _get(self, url: str) -> tuple[Any, str]:
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ContextError("Issue discussion unavailable.")
                deadline = time.monotonic() + self.timeout
                raw = bytearray()
                while True:
                    chunk = response.read1(
                        min(65536, MAX_DOCUMENT_BYTES + 1 - len(raw))
                    )
                    raw.extend(chunk)
                    if len(raw) > MAX_DOCUMENT_BYTES or time.monotonic() > deadline:
                        raise ContextError("Issue discussion exceeds request limits.")
                    if not chunk:
                        break
                return json.loads(raw), response.headers.get("Link", "")
        except (HTTPError, URLError, OSError, HTTPException, ValueError):
            raise ContextError("Complete issue discussion could not be read.") from None

    def fetch(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("source") == "manual":
            return manual_context(item)
        if (
            item.get("source") != "github"
            or type(item.get("issue_number")) is not int
            or item["issue_number"] <= 0
        ):
            raise ContextError("Invalid claimed issue identity.")
        path = repository_path(item.get("issue_repository"))
        number = item["issue_number"]
        api = f"https://api.github.com/repos{path}/issues/{number}"
        issue, _ = self._get(api)
        original = f"https://github.com{path}/issues/{number}"
        if (
            not isinstance(issue, dict)
            or issue.get("number") != number
            or issue.get("html_url") != original
            or "pull_request" in issue
        ):
            raise ContextError("Issue identity does not match the claim.")
        if issue.get("state") != "open":
            raise ContextError("Issue is closed or has an unreadable state.")
        current = self._entry(issue, original)
        current.update(
            {"number": number, "repository": item["issue_repository"], "state": "open"}
        )
        count = issue.get("comments")
        if type(count) is not int or count < 0:
            raise ContextError("Invalid issue comment count.")
        comments: list[dict[str, Any]] = []
        url = api + "/comments?per_page=100&page=1"
        seen = set()
        total_bytes = len(canonical_bytes(current))
        for _ in range(self.max_pages):
            if url in seen:
                raise ContextError("Repeated comment page.")
            seen.add(url)
            page, links = self._get(url)
            if not isinstance(page, list):
                raise ContextError("Invalid comments page.")
            for entry in page:
                if (
                    not isinstance(entry, dict)
                    or type(entry.get("id")) is not int
                    or entry["id"] <= 0
                ):
                    raise ContextError("Invalid comment.")
                comment_url = original + f"#issuecomment-{entry['id']}"
                comment = self._entry(entry, comment_url)
                comment["id"] = entry["id"]
                if any(previous["id"] == comment["id"] for previous in comments):
                    raise ContextError("Duplicate comment.")
                comments.append(comment)
                total_bytes += len(canonical_bytes(comment))
                if total_bytes > MAX_DOCUMENT_BYTES:
                    raise ContextError("Complete discussion exceeds size limit.")
            next_links = re.findall(r'<([^>]+)>;\s*rel="next"', links)
            if not next_links:
                break
            if len(next_links) != 1 or not re.fullmatch(
                re.escape(api)
                + r"/comments\?(?:per_page=100&page=[1-9][0-9]*|page=[1-9][0-9]*&per_page=100)",
                next_links[0],
            ):
                raise ContextError("Invalid comment pagination URL.")
            url = next_links[0]
        else:
            raise ContextError("Comment pagination limit reached.")
        if len(comments) != count:
            raise ContextError(
                "Discussion changed or comments are incomplete; refresh it."
            )
        return {
            "schema_version": 1,
            "source": "github",
            "title": text(issue.get("title"), "title"),
            "instructions": current["body"],
            "issue": current,
            "comments": comments,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "checkout": checkout(item),
        }

    @staticmethod
    def _entry(entry: dict[str, Any], expected_url: str) -> dict[str, Any]:
        author = entry.get("user")
        if not isinstance(author, dict) or entry.get("html_url") != expected_url:
            raise ContextError("Invalid discussion source or author.")
        return {
            "body": text(
                "" if entry.get("body") is None else entry["body"], "body", empty=True
            ),
            "author": text(author.get("login"), "author"),
            "created_at": timestamp(entry.get("created_at")),
            "updated_at": timestamp(entry.get("updated_at")),
            "url": expected_url,
        }
