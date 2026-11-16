import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.views.decorators.debug import sensitive_variables

from projects.validators import normalize_repository_url


class GitHubAPIError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@sensitive_variables()
def repository_issues(repository_url, token):
    repository = normalize_repository_url(repository_url)
    path = urlsplit(repository).path.removesuffix(".git")
    opener = build_opener(NoRedirect())
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
        url = f"https://api.github.com/repos{path}/issues?{query}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "vibearr",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                issues = json.load(response)
        except HTTPError as exc:
            exc.close()
            raise GitHubAPIError(f"GitHub returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise GitHubAPIError("Unable to read GitHub issues.") from None
        if not isinstance(issues, list):
            raise GitHubAPIError("GitHub returned an invalid issue list.")
        for issue in issues:
            if not isinstance(issue, dict):
                raise GitHubAPIError("GitHub returned an invalid issue.")
            if "pull_request" in issue:
                continue
            yield issue_data(issue)
        if len(issues) < 100:
            return
        page += 1


def issue_data(issue):
    number = issue.get("number")
    title = issue.get("title")
    body = issue.get("body") or ""
    state = issue.get("state")
    if type(number) is not int or not 0 < number < 2**63:
        raise GitHubAPIError("GitHub returned an invalid issue number.")
    if not isinstance(title, str) or not title.strip():
        raise GitHubAPIError("GitHub returned an invalid issue title.")
    if not isinstance(body, str) or state not in ("open", "closed"):
        raise GitHubAPIError("GitHub returned invalid issue content.")
    return {"number": number, "title": title, "body": body, "state": state}
