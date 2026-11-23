"""Version compact writes while preserving recorded-operation recovery routes."""

PREFIX = "/v4/compact-worker-v1"


def recovery_request(path):
    parts = path.split("/")
    if len(parts) != 5:
        return False
    group = parts[:3]
    action = parts[4]
    if group == ["", "v1", "operations"]:
        return action in {"status", "stop", "logs", "result", "termination_receipt"}
    if group == ["", "v1", "dependency-builds"]:
        return action in {"status", "stop"}
    return group == ["", "v2", "operations"] and action == "dependencies"
