"""Trusted tool-free HTTP worker. Run with isolated Python, never in a checkout."""

import http.client
import json
import sys
from urllib.parse import quote

MAX_BYTES = 4 * 1024 * 1024


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
        connection = http.client.HTTPSConnection(
            "api.openai.com", timeout=data["timeout"]
        )
        headers = {
            "Authorization": "Bearer " + data["token"],
            "Content-Type": "application/json",
        }
        probe_model = data.get("probe_model")
        if probe_model is not None:
            if (
                not isinstance(probe_model, str)
                or not probe_model
                or len(probe_model) > 200
            ):
                raise ValueError
            connection.request(
                "GET", "/v1/models/" + quote(probe_model, safe=""), headers=headers
            )
        else:
            connection.request(
                "POST", "/v1/responses", json.dumps(data["request"]), headers
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
            print(json.dumps({"authenticated_model": value.get("id")}))
        else:
            print(json.dumps({"response": value}))
    except Exception:
        # Provider bodies and exception messages can contain credentials or prompt data.
        print(json.dumps({"error": "transport"}))


if __name__ == "__main__":
    main()
