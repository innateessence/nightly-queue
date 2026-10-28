import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    # Redirects must not receive the bearer token.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_api(base_url, token, path, data=None):
    url = f"{base_url.rstrip('/')}/api/v1/{path}"
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    request = Request(url, data=body, headers=headers)
    opener = build_opener(NoRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        message = http_error_message(exc)
        raise RuntimeError(f"API returned HTTP {exc.code}: {message}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else str(exc)
        raise RuntimeError(f"Could not reach nightly-queue: {reason}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("API returned an invalid JSON response.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("API returned an invalid JSON response.")
    return result


def http_error_message(error):
    try:
        details = json.load(error)
    except (ValueError, UnicodeDecodeError):
        return error.reason
    return json.dumps(details, ensure_ascii=False)


def positive_id(value):
    try:
        number = int(value)
        if not 0 < number < 2**63:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("Project ID must be a positive integer.")


def create_parser():
    description = "Add work to your nightly queue."
    parser = argparse.ArgumentParser(prog="nq", description=description)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("projects", help="List existing project IDs and repositories.")
    add = commands.add_parser("add", help="Queue work for an existing project.")
    add.add_argument("title")
    add.add_argument("--project", required=True, type=positive_id)
    add.add_argument("--instructions", default="")
    add.add_argument("--priority", choices=["low", "normal", "high"], default="normal")
    return parser


def validate_server_url(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError
    invalid_characters = any(char.isspace() or ord(char) < 32 for char in base_url)
    if invalid_characters:
        raise ValueError
    # Accessing port validates its range.
    parsed.port


def list_projects(base_url, token):
    response = request_api(base_url, token, "projects/")
    projects = response["projects"]
    if not projects:
        print("No projects. Create one in Django admin first.")
        return
    for project in projects:
        print(f"{project['id']}\t{project['name']}\t{project['repository_url']}")


def add_item(base_url, token, args):
    data = {
        "project_id": args.project,
        "title": args.title,
        "instructions": args.instructions,
        "priority": args.priority,
    }
    response = request_api(base_url, token, "items/", data)
    item = response["item"]
    project_id = item["project"]["id"]
    print(f"Queued item #{item['id']} for project #{project_id}: {item['title']}")


def main(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    base_url = os.environ.get("NQ_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("NQ_API_TOKEN", "")
    if not token:
        print("nq: set NQ_API_TOKEN to the server's API token.", file=sys.stderr)
        return 1
    try:
        validate_server_url(base_url)
    except ValueError:
        message = "NQ_URL must be an HTTP(S) server URL without credentials or query parameters."
        print(f"nq: {message}", file=sys.stderr)
        return 1
    try:
        if args.command == "projects":
            list_projects(base_url, token)
            return 0
        add_item(base_url, token, args)
        return 0
    except RuntimeError as exc:
        print(f"nq: {exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError):
        print("nq: API returned an unexpected response.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
