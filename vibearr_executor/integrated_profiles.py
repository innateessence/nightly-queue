"""Immutable CLI and validation recipes installed in the dedicated worker."""

from .contracts import Limits, Profile
from .validation_profile import ValidationProfile


VERSIONS = {"codex": "0.169.0", "claude": "2.1.118", "opencode": "1.19.5"}
LIMITS = Limits(2, 4096, 128, 2048)
MODEL_HOSTS = (
    "chatgpt.com",
    "auth.openai.com",
    "api.anthropic.com",
    "claude.ai",
    "platform.claude.com",
)
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.native_task")


def profiles(image, requirements_digest):
    validation = ValidationProfile(image, image, requirements_digest)
    agent = Profile(
        "native-default",
        1,
        image,
        "agent",
        ARGV,
        LIMITS,
        MODEL_HOSTS,
        "VIBEARR_NATIVE_CONNECTION",
        image,
    )
    return [
        agent,
        validation.setup_profile(),
        *validation.executor_profiles(),
    ], validation
