import io
import json
import re
import zipfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from .client import NoRedirects

LIMIT = 4 * 1024 * 1024


def valid_response_id(value):
    if not isinstance(value, str) or not 0 < len(value) <= 200 or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def verify_boundary_workflow(boundary, token):
    match = re.fullmatch(
        r"https://github.com/([^/]+/[^/]+)/actions/runs/([1-9][0-9]*)",
        boundary["workflow_run_url"],
    )
    if not match:
        raise ValueError("Invalid deployment workflow URL.")
    repository, run_id = match.groups()
    opener = build_opener(ProxyHandler({}), NoRedirects())
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def read(path):
        request = Request("https://api.github.com" + path, headers=headers)
        with opener.open(request, timeout=15) as response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError("Workflow evidence is too large.")
            return json.loads(raw)

    run = read(f"/repos/{repository}/actions/runs/{run_id}")
    environment = boundary["environment"]
    expected = {
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "path": ".github/workflows/runner-deployment-boundary.yml",
        "head_sha": environment["source_revision"],
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "Deployment workflow is incomplete, failed, or has a different source revision."
        )
    artifacts = read(
        f"/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
    )
    if artifacts.get("total_count", 101) >= 100:
        raise ValueError("Deployment artifact lookup is incomplete.")
    matching = [
        row
        for row in artifacts.get("artifacts", [])
        if row.get("name") == "runner-deployment-boundary"
        and row.get("expired") is False
    ]
    if len(matching) != 1:
        raise ValueError(
            "Exactly one unexpired deployment evidence artifact is required."
        )
    artifact_id = matching[0]["id"]
    if type(artifact_id) is not int or artifact_id < 1:
        raise ValueError("Invalid artifact identity.")
    request = Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        headers=headers,
    )
    try:
        opener.open(request, timeout=15).close()
    except HTTPError as error:
        if error.code != 302:
            raise
        location = error.headers["Location"]
        error.close()
    else:
        raise ValueError("Expected a signed artifact download redirect.")
    parsed = urlsplit(location)
    hostname = parsed.hostname or ""
    trusted = hostname.endswith(
        (
            ".blob.core.windows.net",
            ".actions.githubusercontent.com",
            ".githubusercontent.com",
        )
    )
    if parsed.scheme != "https" or not trusted or parsed.username or parsed.password:
        raise ValueError("Invalid signed artifact download host.")
    # The signed storage request never receives the GitHub credential.
    with opener.open(Request(location), timeout=15) as response:
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Deployment artifact exceeds the download limit.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist() != ["runner-boundary.json"]:
            raise ValueError("Deployment artifact has unexpected contents.")
        info = archive.getinfo("runner-boundary.json")
        if info.file_size > 65536:
            raise ValueError("Deployment evidence exceeds its limit.")
        document = json.loads(archive.read(info))
    for key in (
        "environment",
        "code_revision",
        "environment_digest",
        "command",
        "exit_code",
        "output_digest",
    ):
        if document.get(key) != boundary.get(key):
            raise ValueError(
                "Downloaded deployment evidence does not match qualification."
            )
    if type(document.get("tests_run")) is not int or document["tests_run"] < 1:
        raise ValueError("Deployment evidence did not execute boundary tests.")
    return {
        "run_id": int(run_id),
        "head_sha": run["head_sha"],
        "artifact_id": artifact_id,
        "conclusion": run["conclusion"],
    }
