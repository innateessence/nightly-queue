"""Trusted inference worker, without tools, redirects, plugins or repository access."""

import hashlib
import http.client
import json
import runpy
import sys
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 4 * 1024 * 1024


def main() -> None:
    connection = None
    try:
        data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
        validators = runpy.run_path(
            str(Path(__file__).with_name("opencode_connection.py"))
        )
        selected = validators["validate_connection"](data["connection"])
        parsed = urlsplit(selected["base_url"])
        factory = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = factory(parsed.hostname, parsed.port, timeout=data["timeout"])
        headers = {"Content-Type": "application/json"}
        key = selected["api_key"]
        provider = selected["provider"]
        if provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2026-10-21"})
        elif key:
            headers["Authorization"] = "Bearer " + key
        suffix = "/chat" if provider == "ollama" else "/chat/completions"
        if provider == "anthropic":
            suffix = "/messages"
        probing = data.get("operation") == "probe"
        path = parsed.path.rstrip("/") + suffix
        if probing:
            path = parsed.path.rstrip("/") + (
                "/tags" if provider == "ollama" else "/models"
            )
            if provider == "opencode":
                path = "/inference/v1/models"
        connection.request(
            "GET" if probing else "POST",
            path,
            None if probing else json.dumps(data["request"]),
            headers,
        )
        response = connection.getresponse()
        if response.status in {401, 403}:
            print(json.dumps({"error": "authentication"}))
            return
        if response.status != 200:
            print(json.dumps({"error": "model"}))
            return
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        value = json.loads(raw)
        if probing:
            rows = value.get("models" if provider == "ollama" else "data", [])
            names = {
                (
                    row.get("name", row.get("model"))
                    if provider == "ollama"
                    else row.get("id")
                )
                for row in rows
                if isinstance(row, dict)
            }
            model = data.get("model")
            available = model in names or (
                provider == "ollama"
                and isinstance(model, str)
                and model + ":latest" in names
            )
            print(json.dumps({"authenticated_model": model if available else None}))
            return
        result = {"response": value}
        if provider == "ollama":
            receipt = {"response_digest": hashlib.sha256(raw).hexdigest()}
            reported_version = value.get("ollama_version")
            if isinstance(reported_version, str) and len(reported_version) <= 100:
                receipt["ollama_version"] = reported_version
            result["receipt"] = receipt
        print(json.dumps(result))
    except Exception:
        print(json.dumps({"error": "transport"}))
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
