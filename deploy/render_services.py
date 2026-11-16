"""Render user services for an explicit immutable release and private runtime."""

import argparse
import os
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18791)
    args = parser.parse_args()
    paths = [args.release.resolve(), args.runtime.resolve(), args.python.absolute()]
    if any(not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) for path in paths):
        parser.error(
            "Service paths must be absolute and contain no shell or unit escapes."
        )
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged executor port.")
    if not (paths[0] / "vibearr_runner").is_dir() or not paths[2].is_file():
        parser.error("Select an existing release checkout and Python executable.")
    runtime = paths[1]
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = runtime.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error("Runtime must be an owned private directory (0700).")
    output = runtime / "units"
    output.mkdir(mode=0o700, exist_ok=True)
    values = dict(
        RELEASE=str(paths[0]),
        RUNTIME=str(runtime),
        PYTHON=str(paths[2]),
        PORT=str(args.port),
    )
    for template in sorted(Path(__file__).parent.glob("*.service.in")):
        content = template.read_text()
        for name, value in values.items():
            content = content.replace(f"@{name}@", value)
        destination = output / template.name.removesuffix(".in")
        destination.write_text(content)
        destination.chmod(0o600)
        print(destination)


if __name__ == "__main__":
    main()
