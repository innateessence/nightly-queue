"""Version compact writes while preserving recorded-operation recovery routes."""

import ipaddress

from .contracts import Rejected

PREFIX = "/v4/compact-worker-v1"
QUALIFICATION_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def qualification_targets(value):
    if type(value) is not dict or set(value) != {"app", "postgres", "runner"}:
        raise Rejected(
            "Supply exactly the inspected app, postgres and runner IPv4 targets."
        )
    for address in value.values():
        if not isinstance(address, str) or len(address) > 15:
            raise Rejected(
                "Qualification targets must be canonical private IPv4 addresses."
            )
        try:
            parsed = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            raise Rejected(
                "Qualification targets must be canonical private IPv4 addresses."
            ) from None
        if not any(parsed in network for network in QUALIFICATION_NETWORKS):
            raise Rejected(
                "Qualification targets must be actual private service addresses."
            )
    if len(set(value.values())) != 3:
        raise Rejected("Qualification targets must identify distinct services.")
    return dict(value)


def recovery_request(path):
    parts = path.split("/")
    if len(parts) != 5:
        return False
    group = parts[:3]
    action = parts[4]
    if group == ["", "v1", "operations"]:
        return action in {"status", "stop", "logs", "result", "termination_receipt"}
    if group == ["", "v1", "dependency-builds"]:
        return action == "stop"
    return group == ["", "v2", "operations"] and action == "dependencies"
