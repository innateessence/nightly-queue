"""Audit operator-rendered Compose facts without exposing an engine API."""

import json
import sys


def audit(config):
    errors = []
    if set(config["services"]) != {"app", "postgres", "runner"}:
        errors.append(
            "The default deployment must contain exactly app, postgres and runner."
        )
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
        if service.get("ports") and name != "app":
            errors.append(f"{name}: private service publishes a host port")
    return errors


def inspect_containers(containers):
    errors, facts = [], []
    for container in containers:
        name = container["Name"].lstrip("/")
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        host = container["HostConfig"]
        if host.get("Privileged") or host.get("Devices") or host.get("DeviceRequests"):
            errors.append(f"{name}: runtime privileges or devices")
        if host.get("PidMode") or host.get("IpcMode") not in {"", "private", None}:
            errors.append(f"{name}: shared runtime namespace")
        if host.get("NetworkMode") == "host" or host.get("NetworkMode", "").startswith(
            "container:"
        ):
            errors.append(f"{name}: shared network namespace")
        dropped = {
            value.upper().removeprefix("CAP_") for value in host.get("CapDrop") or []
        }
        added = {
            value.upper().removeprefix("CAP_") for value in host.get("CapAdd") or []
        }
        if not host.get("ReadonlyRootfs") or "ALL" not in dropped:
            errors.append(f"{name}: runtime image or capability boundary")
        if "no-new-privileges:true" not in host.get("SecurityOpt", []):
            errors.append(f"{name}: privilege escalation is enabled")
        if added & {"ALL", "SYS_ADMIN"}:
            errors.append(f"{name}: forbidden runtime capabilities")
        mounts = [
            {
                "type": value["Type"],
                "target": value["Destination"],
                "writable": value["RW"],
            }
            for value in container["Mounts"]
        ]
        if any(value["type"] not in {"volume", "tmpfs"} for value in mounts):
            errors.append(f"{name}: runtime host bind")
        if any("docker.sock" in value["target"] for value in mounts):
            errors.append(f"{name}: runtime engine socket")
        if any(value.startswith("DOCKER_") for value in container["Config"]["Env"]):
            errors.append(f"{name}: runtime engine environment")
        ports = host.get("PortBindings") or {}
        if ports and service != "app":
            errors.append(f"{name}: private runtime port published")
        limits = {
            "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"),
            "pids": host.get("PidsLimit"),
        }
        if service == "runner":
            environment = dict(
                value.split("=", 1) for value in container["Config"]["Env"]
            )
            if environment.get("VIBEARR_RUNNER_IMAGE_ID") != container["Image"]:
                errors.append(
                    f"{name}: declared worker image differs from its actual image"
                )
            expected = {
                "memory_bytes": 4096 * 1024**2,
                "memory_swap_bytes": 4096 * 1024**2,
                "nano_cpus": 2_000_000_000,
                "pids": 128,
            }
            if limits != expected:
                errors.append(
                    f"{name}: actual worker ceilings differ from the registered slot"
                )
        facts.append(
            {
                "name": name,
                "service": service,
                "container_id": container["Id"],
                "image": container["Image"],
                "read_only": host.get("ReadonlyRootfs"),
                "mounts": mounts,
                "limits": limits,
            }
        )
    return {"schema_version": 1, "errors": errors, "containers": facts}


def main():
    if sys.argv[1:] == ["--inspect"]:
        report = inspect_containers(json.load(sys.stdin))
        print(json.dumps(report, indent=2))
        raise SystemExit(bool(report["errors"]))
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
