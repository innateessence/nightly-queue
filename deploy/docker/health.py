import json
import urllib.request
import time
from pathlib import Path


def main():
    heartbeat = float(Path("/tmp/vibearr-scheduler-heartbeat").read_text())
    if not 0 <= time.time() - heartbeat < 180:
        raise SystemExit(1)
    response = urllib.request.urlopen("http://127.0.0.1:8000/health/", timeout=3)
    if response.status != 200 or json.load(response) != {"status": "ok"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
