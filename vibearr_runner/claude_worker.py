"""Trusted tool-free Claude HTTP worker; credentials arrive through private stdin."""

import http.client
import json
import sys
import re
from urllib.parse import quote

MAX_BYTES = 4 * 1024 * 1024
API_VERSION = "2026-10-21"


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
        connection = http.client.HTTPSConnection(
            "api.anthropic.com", timeout=data["timeout"]
        )
        headers = {
            "Authorization": "Bearer " + data["token"],
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }
        probe_model = data.get("probe_model")
        if probe_model is not None:
            if (
                not isinstance(probe_model, str)
                or len(probe_model) > 200
                or re.fullmatch(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*", probe_model) is None
            ):
                raise ValueError
            connection.request(
                "GET", "/v1/models/" + quote(probe_model, safe=""), headers=headers
            )
        else:
            connection.request(
                "POST", "/v1/messages", json.dumps(data["request"]), headers
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
        if probe_model is not None:
            model_id = value.get("id")
            valid = (
                value.get("type") == "model"
                and isinstance(model_id, str)
                and model_id.startswith("claude-")
            )
            print(json.dumps({"authenticated_model": probe_model if valid else None}))
        else:
            print(json.dumps({"response": value}))
    except Exception:
        # Never expose provider bodies or exception strings containing secrets or source data.
        print(json.dumps({"error": "transport"}))


if __name__ == "__main__":
    main()
