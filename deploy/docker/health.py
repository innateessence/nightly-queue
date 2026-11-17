import json
import urllib.request


def main():
    response = urllib.request.urlopen("http://127.0.0.1:8000/health/", timeout=3)
    if response.status != 200 or json.load(response) != {"status": "ok"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
