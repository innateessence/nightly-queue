"""Observe the actual trusted controller process in a disposable Docker runner."""

import json
import os
from pathlib import Path


def main():
    if os.environ.get("VIBEARR_DISPOSABLE_ACCEPTANCE") != "1":
        raise RuntimeError(
            "Controller restart witnesses require a disposable acceptance project."
        )
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if b"vibearr_runner" not in arguments:
                continue
            status = dict(
                line.split(":", 1)
                for line in (process / "status").read_text().splitlines()
                if ":" in line
            )
            fields = (process / "stat").read_text().rsplit(") ", 1)[1].split()
        except FileNotFoundError:
            continue
        if int(status["Uid"].split()[0]) != 10001:
            raise RuntimeError(
                "Trusted controller is running with unexpected authority."
            )
        found.append(
            {"pid": int(process.name), "start_ticks": int(fields[19]), "uid": 10001}
        )
    if len(found) != 1:
        raise RuntimeError("Expected exactly one live trusted controller process.")
    state = json.loads(Path("/var/lib/vibearr/journal.json").read_bytes())
    if type(state.get("runner_id")) is not int or state.get("operation"):
        raise RuntimeError(
            "Controller restart witness requires an authenticated idle journal."
        )
    print(
        json.dumps(
            {
                "kind": "actual-controller-process",
                "runner_id": state["runner_id"],
                **found[0],
            }
        )
    )


if __name__ == "__main__":
    main()
