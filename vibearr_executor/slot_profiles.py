"""Image-owned slot recipes; task manifests cannot choose commands."""

from .contracts import Limits, Profile, sha256
from .codex_task import ARGV, HOSTS, SECRET_ENV
from .validation_profile import ValidationProfile


LIMITS = Limits(cpu=2, memory_mb=4096, pids=128, workspace_mb=2048)


def profiles(phase, image, database_image, dependency_digest):
    sha256(image.removeprefix("sha256:"))
    if not image.startswith("sha256:"):
        raise ValueError("Operator must bind the actual immutable worker image ID.")
    validation = ValidationProfile(image, database_image, dependency_digest)
    if phase == "validation":
        return validation.executor_profiles()
    if phase == "setup":
        return [validation.setup_profile()]
    if phase != "agent":
        raise ValueError("Unsupported worker phase.")
    return [
        Profile(
            profile_id="native-default",
            version=1,
            image=image,
            phase=phase,
            argv=ARGV,
            limits=LIMITS,
            network_hosts=HOSTS["token"],
            backend_secret_env=SECRET_ENV,
            database_image=database_image,
        )
    ]
