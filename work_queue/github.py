import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from projects.validators import normalize_repository_url


class GitHubAPIError(Exception):
    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_time(headers):
    now = datetime.now(timezone.utc)
    try:
        seconds = int(headers.get("Retry-After", 0))
        reset = int(headers.get("X-RateLimit-Reset", 0))
    except (TypeError, ValueError):
        return now + timedelta(minutes=1)
    return max(
        now + timedelta(seconds=max(60, seconds)),
        datetime.fromtimestamp(min(reset, 2**32), timezone.utc),
    )


@sensitive_variables()
def github_read(path, token, etag=""):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "vibearr",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    request = Request(f"https://api.github.com{path}", headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise GitHubAPIError(
                    "GitHub source response exceeds the context limit."
                )
            result = json.loads(payload)
            return result, {
                key.lower(): value
                for key, value in dict(getattr(response, "headers", {})).items()
            }
    except HTTPError as exc:
        status = exc.code
        response_headers = exc.headers or {}
        exc.close()
        if status == 304:
            return None, {"not_modified": "true"}
        raise GitHubAPIError(
            f"GitHub returned HTTP {status}.", retry_time(response_headers)
        ) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise GitHubAPIError("Unable to read GitHub issues.") from None


def repository_metadata(repository_url, token, repository_id=None):
    path = urlsplit(normalize_repository_url(repository_url)).path.removesuffix(".git")
    endpoint = f"/repositories/{repository_id}" if repository_id else f"/repos{path}"
    data, _ = github_read(endpoint, token)
    try:
        owner = data["owner"]["id"]
        identity = data["id"]
        url = normalize_repository_url(data["html_url"])
        branch = data["default_branch"]
        if (
            type(owner) is not int
            or type(identity) is not int
            or not all(0 < value < 2**63 for value in (owner, identity))
        ):
            raise ValueError
        if not isinstance(branch, str) or not branch:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GitHubAPIError("GitHub returned invalid repository metadata.") from None
    return {
        "repository_id": identity,
        "owner_id": owner,
        "repository_url": url,
        "default_branch": branch,
    }


@sensitive_variables()
def repository_issues(repository_url, token, *, etags=None):
    path = urlsplit(normalize_repository_url(repository_url)).path.removesuffix(".git")
    page = 1
    while True:
        query = urlencode(
            {
                "state": "all",
                "sort": "created",
                "direction": "asc",
                "per_page": 100,
                "page": page,
            }
        )
        key = str(page)
        cached = (etags or {}).get(key, {})
        issues, headers = github_read(
            f"/repos{path}/issues?{query}", token, cached.get("etag", "")
        )
        if issues is None:
            if "issues" not in cached:
                raise GitHubAPIError(
                    "GitHub returned an uncached conditional response."
                )
            issues = cached["issues"]
        if not isinstance(issues, list):
            raise GitHubAPIError("GitHub returned an invalid issue list.")
        for issue in issues:
            if not isinstance(issue, dict):
                raise GitHubAPIError("GitHub returned an invalid issue.")
            if "pull_request" not in issue:
                yield issue_data(issue)
        if etags is not None:
            etags[key] = {
                "etag": headers.get("etag", cached.get("etag", "")),
                "issues": issues,
            }
        if len(issues) < 100:
            return
        page += 1


def issue_data(issue):
    number = issue.get("number")
    title = issue.get("title")
    body = issue.get("body")
    if body is None:
        body = ""
    state = issue.get("state")
    if type(number) is not int or not 0 < number < 2**63:
        raise GitHubAPIError("GitHub returned an invalid issue number.")
    if not isinstance(title, str) or not title.strip():
        raise GitHubAPIError("GitHub returned an invalid issue title.")
    if not isinstance(body, str) or state not in ("open", "closed"):
        raise GitHubAPIError("GitHub returned invalid issue content.")
    result = {"number": number, "title": title, "body": body, "state": state}
    try:
        identity = issue["id"]
        author = issue["user"]["id"]
        login = issue["user"]["login"]
        labels = [label["name"] for label in issue["labels"]]
        created = issue["created_at"]
        updated = issue["updated_at"]
        dates = [parse_datetime(value) for value in (created, updated)]
        if not all(date and date.utcoffset() is not None for date in dates):
            raise ValueError
        if not all(
            type(value) is int and 0 < value < 2**63 for value in (identity, author)
        ):
            raise ValueError
        if not isinstance(login, str) or not all(
            isinstance(label, str) for label in labels
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GitHubAPIError(
            "GitHub returned incomplete issue identity or context."
        ) from None
    result.update(
        issue_id=identity,
        author_id=author,
        author_login=login,
        labels=labels,
        created_at=created,
        updated_at=updated,
    )
    result["revision"] = hashlib.sha256(
        json.dumps(issue, sort_keys=True).encode()
    ).hexdigest()
    return result


def read_issue(project, number, token):
    if not project.github_repository_id:
        raise GitHubAPIError("Repository identity has not been synchronized.")
    repository = repository_metadata(
        project.repository_url, token, project.github_repository_id
    )
    if repository["repository_id"] != project.github_repository_id:
        raise GitHubAPIError("Repository identity changed.")
    path = urlsplit(repository["repository_url"]).path.removesuffix(".git")
    data, _ = github_read(f"/repos{path}/issues/{number}", token)
    if not isinstance(data, dict) or "pull_request" in data:
        raise GitHubAPIError("GitHub returned invalid issue context.")
    return issue_data(data)


def issue_comments(project, number, token):
    repository = repository_metadata(
        project.repository_url, token, project.github_repository_id
    )
    if repository["repository_id"] != project.github_repository_id:
        raise GitHubAPIError("Repository identity changed.")
    path = urlsplit(repository["repository_url"]).path.removesuffix(".git")
    comments = []
    for page in range(1, 11):
        data, _ = github_read(
            f"/repos{path}/issues/{number}/comments?per_page=100&page={page}", token
        )
        if not isinstance(data, list):
            raise GitHubAPIError("GitHub returned invalid comments.")
        for comment in data:
            try:
                entry = {
                    key: comment[key]
                    for key in ("id", "body", "created_at", "updated_at")
                }
                entry["author_id"] = comment["user"]["id"]
                if (
                    type(entry["id"]) is not int
                    or type(entry["author_id"]) is not int
                    or not isinstance(entry["body"], str)
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise GitHubAPIError(
                    "GitHub returned incomplete comment context."
                ) from None
            comments.append(entry)
        if len(json.dumps(comments).encode()) > 4 * 1024 * 1024:
            raise GitHubAPIError("Issue comments exceed the context limit.")
        if len(data) < 100:
            return comments
    raise GitHubAPIError("Issue comments exceed the pagination limit.")
