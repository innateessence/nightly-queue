"""Trusted tool-free Claude HTTP worker; credentials arrive through private stdin."""

import http.client
import json
import sys

MAX_BYTES = 4 * 1024 * 1024
API_VERSION = "2026-10-21"


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
        connection = http.client.HTTPSConnection(
            "api.anthropic.com", timeout=data["timeout"]
        )
        headers = {
            "x-api-key": data["token"],
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }
        connection.request("POST", "/v1/messages", json.dumps(data["request"]), headers)
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
        # Never expose provider bodies or exception strings containing secrets or source data.
        print(json.dumps({"error": "transport"}))


if __name__ == "__main__":
    main()
