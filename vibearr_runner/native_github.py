"""Stable repository identity and observed provider permissions."""

from urllib.parse import quote

from .context import repository_path
from .delivery import DeliveryError, DeliveryUnknown, GitHubDeliveryClient
from .workspaces import validate_branch


def repository_identity(value, *, expected_id=None):
    try:
        identity = value["id"]
        repository = value["full_name"]
        owner = value["owner"]["id"]
        branch = value["default_branch"]
        if (
            type(identity) is not int
            or identity < 1
            or type(owner) is not int
            or owner < 1
        ):
            raise ValueError
        if expected_id is not None and identity != expected_id:
            raise ValueError
        url = f"https://github.com/{repository}"
        if value["html_url"] != url or repository_path(url).lstrip("/") != repository:
            raise ValueError
        validate_branch(branch)
        if not branch:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DeliveryError(
            "GitHub repository identity does not match the selected project."
        ) from None
    return {
        "repository_id": identity,
        "repository": repository,
        "repository_url": url + ".git",
        "owner_id": owner,
        "default_branch": branch,
    }


class NativeGitHub(GitHubDeliveryClient):
    def repository(self, *, repository_id=None, repository_url=""):
        path = (
            f"/repositories/{repository_id}"
            if repository_id
            else "/repos" + repository_path(repository_url)
        )
        raw = self.request("GET", path)
        return repository_identity(raw, expected_id=repository_id), raw.get(
            "permissions", {}
        )

    def pull(self, repository, number):
        if type(number) is not int or number < 1:
            raise DeliveryError("Invalid pull request number.")
        return self.request("GET", f"/repos/{repository}/pulls/{number}")

    def find_native(self, repository, branch, base, commit, repository_id):
        owner = repository.split("/")[0]
        query = f"state=all&head={quote(owner + ':' + branch, safe='')}&base={quote(base, safe='')}&per_page=100"
        values = self.request("GET", f"/repos/{repository}/pulls?{query}")
        if not isinstance(values, list) or len(values) > 1:
            raise DeliveryError("Pull request lookup is ambiguous or invalid.")
        return (
            native_pull(values[0], repository_id, repository, branch, base, commit)
            if values
            else None
        )

    def create_native(
        self, repository_id, repository, branch, base, commit, title, body
    ):
        raw = self.request(
            "POST",
            f"/repos/{repository}/pulls",
            {
                "title": title[:200],
                "body": body,
                "head": branch,
                "base": base,
                "draft": False,
            },
        )
        try:
            return native_pull(raw, repository_id, repository, branch, base, commit)
        except DeliveryError:
            raise DeliveryUnknown(
                "Created PR identity is unavailable; reconcile the original publication."
            ) from None


def native_pull(value, repository_id, repository, branch, base, commit):
    try:
        number = value["number"]
        if type(number) is not int or number < 1 or value["state"] != "open":
            raise ValueError
        expected = f"https://github.com/{repository}/pull/{number}"
        valid = value["html_url"] == expected
        valid = valid and value["head"]["repo"]["id"] == repository_id
        valid = valid and value["base"]["repo"]["id"] == repository_id
        valid = valid and value["head"]["repo"]["full_name"] == repository
        valid = valid and value["base"]["repo"]["full_name"] == repository
        valid = (
            valid and value["head"]["ref"] == branch and value["base"]["ref"] == base
        )
        valid = valid and value["head"]["sha"] == commit
        if not valid:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DeliveryError(
            "Pull request identity does not match this native publication."
        ) from None
    return {
        "repository_id": repository_id,
        "number": number,
        "url": expected,
        "base": base,
        "head": branch,
        "head_sha": commit,
    }


def preflight_repository(grant, project):
    github = NativeGitHub(grant["material"]["secret"], read_only=True)
    repository, permissions = github.repository(
        repository_id=project.get("source_policy", {}).get("repository_id"),
        repository_url=project["repository_url"],
    )
    if permissions.get("push") is False:
        raise DeliveryError("Selected GitHub account cannot push to this repository.")
    return {
        "repository": repository,
        "read_verified": True,
        "write_verified": False,
        "reason": "Repository read verified; content/PR write permissions are proven only by actual delivery.",
    }
