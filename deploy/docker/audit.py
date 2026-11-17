"""Audit operator-rendered Compose facts without exposing an engine API."""

import json
import sys


def audit(config):
    errors = []
    for name, service in config["services"].items():
        for field in ("privileged", "pid", "ipc", "network_mode", "devices"):
            value = service.get(field)
            if value is True or value == "host" or field == "devices" and value:
                errors.append(f"{name}: forbidden {field}")
        for mount in service.get("volumes", []):
            if mount["type"] not in {"volume", "tmpfs"}:
                errors.append(f"{name}: runtime mount is not a named volume or tmpfs")
            if "docker.sock" in str(mount):
                errors.append(f"{name}: engine socket mount")
        if any(value in {"SYS_ADMIN", "ALL"} for value in service.get("cap_add", [])):
            errors.append(f"{name}: forbidden capabilities")
        if service.get("read_only") is not True:
            errors.append(f"{name}: writable image root")
        for key in service.get("environment", {}):
            if key.startswith("DOCKER_"):
                errors.append(f"{name}: engine environment")
        if service.get("ports") and name != "vibearr":
            errors.append(f"{name}: private service publishes a host port")
    return errors


def main():
    errors = audit(json.load(sys.stdin))
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print(
        "Rendered Compose has no host binds, sockets, privileges, namespaces or private published ports."
    )


if __name__ == "__main__":
    main()
