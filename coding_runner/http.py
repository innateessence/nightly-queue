import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(RuntimeError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, token, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "nightly-queue-coding-runner"
    request = Request(url, data=body, headers=headers)
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        raise APIError(f"HTTP request failed with status {exc.code}.") from None
    except (URLError, OSError, ValueError):
        raise APIError("HTTP request failed or returned invalid JSON.") from None


class QueueClient:
    def __init__(self, url, token):
        self.url = url.rstrip("/") + "/api/v1/"
        self.token = token

    def check(self):
        data = request_json(self.url + "projects/", self.token)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise APIError("Queue returned an invalid projects response.")

    def item_request(self, path, data=None):
        result = request_json(self.url + path, self.token, data)
        if not isinstance(result, dict) or "item" not in result:
            raise APIError("Queue returned an invalid item response.")
        if result["item"] is not None and not isinstance(result["item"], dict):
            raise APIError("Queue returned an invalid item response.")
        return result["item"]

    def claim(self, run_id):
        return self.item_request("items/claim/", {"execution_ref": f"runner:{run_id}"})

    def lookup(self, run_id):
        return self.item_request(f"runner/runs/{run_id}/")

    def finish(self, item, outcome):
        data = {"claim_token": item["claim_token"], **outcome}
        result = self.item_request(f"items/{item['id']}/finish/", data)
        if (
            not result
            or result.get("id") != item["id"]
            or result.get("status") != outcome["status"]
        ):
            raise APIError("Queue returned an unexpected completion result.")
        return result

    def log(self, item, run_id, sequence, text):
        data = {
            "run_id": run_id,
            "claim_token": item["claim_token"],
            "sequence": sequence,
            "text": text,
        }
        url = self.url + f"items/{item['id']}/logs/"
        result = request_json(url, self.token, data, timeout=3)
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise APIError("Queue did not accept the log upload.")


class GitHubClient:
    def __init__(self, token):
        self.token = token

    def check(self):
        data = request_json("https://api.github.com/user", self.token)
        if not isinstance(data, dict) or not data.get("login"):
            raise APIError("GitHub authentication failed.")

    def pull_request(self, repository, branch, base, title, body):
        url = f"https://api.github.com/repos/{repository}/pulls"
        owner = repository.split("/")[0]
        query = urlencode(
            {"state": "all", "head": f"{owner}:{branch}", "per_page": 100}
        )
        pulls = request_json(f"{url}?{query}", self.token)
        if not isinstance(pulls, list):
            raise APIError("GitHub returned an invalid pull request list.")
        matching = [pull for pull in pulls if pull.get("head", {}).get("ref") == branch]
        if matching:
            pull = matching[0]
            if pull.get("state") != "open" or pull.get("base", {}).get("ref") != base:
                raise APIError(
                    "The task branch already has a closed PR or a different PR base."
                )
            return pull["html_url"]
        data = {"head": branch, "base": base, "title": title[:200], "body": body}
        pull = request_json(url, self.token, data)
        return pull["html_url"]
