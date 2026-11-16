"""Trusted tool-free HTTP worker. Run with isolated Python, never in a checkout."""

import http.client
import json
import sys

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
        print(json.dumps({"response": json.loads(raw)}))
    except Exception:
        # Provider bodies and exception messages can contain credentials or prompt data.
        print(json.dumps({"error": "transport"}))


if __name__ == "__main__":
    main()
