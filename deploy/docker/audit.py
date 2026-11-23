"""Audit operator-rendered Compose facts without exposing an engine API."""

import json
import re
import sys


SERVICES = {"app", "postgres", "runner", "worker"}
NETWORKS = {
    "app": {"database", "runner-api", "app-egress"},
    "postgres": {"database"},
    "runner": {"runner-api", "worker-control", "runner-egress"},
    "worker": {"worker-control"},
}
WORKER_LIMITS = {
    "memory_bytes": 4096 * 1024**2,
    "memory_swap_bytes": 4096 * 1024**2,
    "nano_cpus": 2_000_000_000,
    "pids": 128,
}
IMAGE_KEYS = {"runner": "VIBEARR_RUNNER_IMAGE_ID", "worker": "VIBEARR_WORKER_IMAGE_ID"}
APPLICATION_CREDENTIALS = {
    "DJANGO_SECRET_KEY",
    "VIBEARR_CREDENTIAL_KEY",
    "NQ_CREDENTIAL_KEY",
    "VIBEARR_POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD_FILE",
    "VIBEARR_API_TOKEN",
    "NQ_API_TOKEN",
}


def environment_errors(name, environment):
    errors = []
    keys = set(environment)
    if name in {"runner", "worker"} and keys & APPLICATION_CREDENTIALS:
        errors.append(f"{name}: application credentials are forbidden")
    if name == "worker" and keys & {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "VIBEARR_RUNNER_API_TOKEN",
        "VIBEARR_RUNNER_API_TOKEN_FILE",
    }:
        errors.append("worker: publication and controller credentials are forbidden")
    return errors


def isolation(name, networks, volumes, capabilities):
    errors = []
    if name in NETWORKS and set(networks) != NETWORKS[name]:
        errors.append(f"{name}: networks differ from the registered service boundary")
    if name == "worker":
        if set(volumes) != {"worker-control", "worker-secrets"}:
            errors.append(
                "worker: only worker control and dedicated channel secrets may be mounted"
            )
    if name == "runner":
        permitted = {"runner-data", "runner-secrets", "runner-logins", "worker-secrets"}
        if set(volumes) != permitted:
            errors.append(
                "runner: only private journal, login and channel volumes may be mounted"
            )
        if set(capabilities) & {"NET_ADMIN", "SETPCAP"}:
            errors.append("runner: task execution capabilities are forbidden")
    return errors


def audit(config):
    errors = []
    if set(config["services"]) != SERVICES:
        errors.append(
            "The default deployment must contain exactly app, postgres, runner and worker."
        )
    for network in ("database", "runner-api", "worker-control"):
        if config.get("networks", {}).get(network, {}).get("internal") is not True:
            errors.append(f"{network}: trusted service network must be internal")
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
        capabilities = {
            value.upper().removeprefix("CAP_") for value in service.get("cap_add", [])
        }
        if capabilities & {"SYS_ADMIN", "ALL"}:
            errors.append(f"{name}: forbidden capabilities")
        if service.get("read_only") is not True:
            errors.append(f"{name}: writable image root")
        for key in service.get("environment", {}):
            if key.startswith("DOCKER_"):
                errors.append(f"{name}: engine environment")
        errors.extend(environment_errors(name, service.get("environment", {})))
        if service.get("ports") and name != "app":
            errors.append(f"{name}: private service publishes a host port")
        volumes = {mount["source"] for mount in service.get("volumes", [])}
        errors.extend(
            isolation(name, service.get("networks", {}), volumes, capabilities)
        )
        if name in {"runner", "worker"}:
            for mount in service.get("volumes", []):
                if mount["source"] == "worker-secrets" and not mount.get("read_only"):
                    errors.append(f"{name}: worker channel secrets must be read-only")
            image = service.get("environment", {}).get(IMAGE_KEYS[name], "")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                errors.append(f"{name}: select the actual registered image ID")
            protocol = service.get("environment", {}).get("VIBEARR_RUNTIME_PROTOCOL")
            if protocol != "compact-worker-v1":
                errors.append(f"{name}: select the compact worker boundary")
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
        environment = dict(value.split("=", 1) for value in container["Config"]["Env"])
        errors.extend(environment_errors(service, environment))
        ports = host.get("PortBindings") or {}
        if ports and service != "app":
            errors.append(f"{name}: private runtime port published")
        limits = {
            "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"),
            "pids": host.get("PidsLimit"),
        }
        if service in IMAGE_KEYS:
            environment = dict(
                value.split("=", 1) for value in container["Config"]["Env"]
            )
            if environment.get(IMAGE_KEYS[service]) != container["Image"]:
                errors.append(
                    f"{name}: declared {service} image differs from its actual image"
                )
            if service == "worker" and limits != WORKER_LIMITS:
                errors.append(
                    f"{name}: actual worker ceilings differ from the registered slot"
                )
        labels = container["Config"]["Labels"]
        project = labels.get("com.docker.compose.project", "")
        if not project or service not in SERVICES:
            errors.append(f"{name}: selected Compose service identity is missing")
        else:
            prefix = project + "_"
            observed_networks = container.get("NetworkSettings", {}).get("Networks", {})
            networks = {value.removeprefix(prefix) for value in observed_networks}
            volumes = {
                value.get("Name", "").rsplit("_", 1)[-1]
                for value in container["Mounts"]
                if value["Type"] == "volume"
            }
            errors.extend(isolation(service, networks, volumes, added))
            for mount in container["Mounts"]:
                volume = mount.get("Name", "")
                logical = volume.rsplit("_", 1)[-1]
                if (
                    service in {"runner", "worker"}
                    and volume
                    and logical != "runner-data"
                    and volume != prefix + logical
                ):
                    errors.append(
                        f"{name}: private volume belongs to a different project"
                    )
                if logical == "worker-secrets" and mount["RW"]:
                    errors.append(f"{name}: worker channel secrets are writable")
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


def inspect_stack(containers):
    report = inspect_containers(containers)
    services = [item["service"] for item in report["containers"]]
    if len(services) != len(SERVICES) or set(services) != SERVICES:
        report["errors"].append(
            "Actual stack must contain exactly one app, postgres, runner and worker."
        )
    projects = {
        item["Config"]["Labels"].get("com.docker.compose.project")
        for item in containers
    }
    if len(projects) != 1:
        report["errors"].append("Actual containers must belong to one Compose project.")
    by_service = {
        item["Config"]["Labels"].get("com.docker.compose.service"): item
        for item in containers
    }
    if {"runner", "worker"} <= by_service.keys():
        runner = dict(
            value.split("=", 1) for value in by_service["runner"]["Config"]["Env"]
        )
        if runner.get("VIBEARR_WORKER_IMAGE_ID") != by_service["worker"]["Image"]:
            report["errors"].append(
                "Runner selected worker image differs from the actual worker."
            )
    return report


def main():
    if sys.argv[1:] == ["--inspect"]:
        report = inspect_stack(json.load(sys.stdin))
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
