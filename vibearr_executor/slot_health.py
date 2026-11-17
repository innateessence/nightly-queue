from pathlib import Path
import os

from .client import Client


def main():
    client = Client(
        "http://127.0.0.1:8791",
        Path(
            os.environ.get("VIBEARR_EXECUTOR_TOKEN_FILE", "/run/vibearr/control-token")
        )
        .read_text()
        .strip(),
    )
    observed = client.capabilities()
    if observed.get("runtime") != "compose-slot-v2":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
