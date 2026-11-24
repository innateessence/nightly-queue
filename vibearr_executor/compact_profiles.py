"""Immutable recipes for the dedicated compact worker."""

from dataclasses import replace

from .integrated_profiles import profiles as installed_profiles


RUNTIME = "compact-worker-v1"
REQUIRED_BOUNDARY_CHECKS = {
    "container-ceilings",
    "task-uid-capabilities",
    "trusted-state-denial",
    "runner-control-denial",
    "direct-egress-denial",
    "descendant-termination",
    "cross-phase-reset",
    "pause-resume-deadline",
    "cancel-and-retry-reconciliation",
    "restart-reconciliation",
    "runner-worker-separation",
    "production-network-denial",
    "local-database-privilege",
    "local-database-reset",
    "pid-exhaustion-control-api",
    "bounded-logs",
    "authenticated-control",
}
ROLES = (
    "intake",
    "specification",
    "build",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
)


def source_access(profile_id):
    role = profile_id.removeprefix("factory-")
    if profile_id == "browser-ui" or role in ROLES and role != "build":
        return "read_only"
    return "read_write"


def profiles(image, requirements_digest):
    installed, validation = installed_profiles(image, requirements_digest)
    agent = next(profile for profile in installed if profile.phase == "agent")
    installed.extend(replace(agent, profile_id="factory-" + role) for role in ROLES)
    return installed, validation
