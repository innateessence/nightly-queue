from pathlib import Path

from .client import Client
from .integrated_profiles import RUNTIME


def main():
    token = Path("/run/vibearr/runner/control-token").read_text().strip()
    observed = Client("http://127.0.0.1:8791", token, timeout=3).capabilities()
    if observed.get("runtime") != RUNTIME:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
