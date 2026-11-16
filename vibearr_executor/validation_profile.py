"""Image-owned validation commands; candidate files never select coverage."""

from dataclasses import dataclass
from pathlib import Path

from .contracts import Limits, Profile, Rejected, canonical, digest

PROFILE_ID = "vibearr-python312-v1"
CHECK_IDS = (
    "black",
    "ruff",
    "mypy",
    "migrations",
    "system",
    "django",
    "cli",
    "runner",
    "git-acceptance",
)
TASK_ARGV = (
    "/usr/local/bin/python3.12",
    "-I",
    "-m",
    "vibearr_executor.validation_task",
)
BUILD_ARGV = (
    "/usr/local/bin/python3.12",
    "-I",
    "-m",
    "vibearr_executor.dependency_task",
)
PACKAGE_HOSTS = ("pypi.org", "files.pythonhosted.org")
VALIDATION_LIMITS = Limits(cpu=2, memory_mb=4096, pids=128, workspace_mb=2048)


def recipe_digest() -> str:
    root = Path(__file__).parent
    files = (
        "validation_profile.py",
        "validation_task.py",
        "validation_app.py",
        "validation_black.toml",
        "validation_ruff.toml",
        "validation_mypy.ini",
    )
    return digest(
        b"".join(name.encode() + b"\0" + (root / name).read_bytes() for name in files)
    )


@dataclass(frozen=True)
class ValidationProfile:
    image: str
    database_image: str
    dependency_digest: str
    profile_id: str = PROFILE_ID
    version: int = 1

    def __post_init__(self):
        from .contracts import sha256

        sha256(self.dependency_digest)
        if (
            self.profile_id != PROFILE_ID
            or type(self.version) is not int
            or self.version != 1
        ):
            raise Rejected("Unsupported trusted validation profile.")
        self.executor_profiles()

    def data(self) -> dict:
        return {
            "schema_version": 1,
            "profile_id": self.profile_id,
            "version": self.version,
            "recipe_digest": recipe_digest(),
            "image": self.image,
            "database_image": self.database_image,
            "dependency_digest": self.dependency_digest,
            "required_checks": list(CHECK_IDS),
            "executor_profiles": [
                profile.data() for profile in self.executor_profiles()
            ],
        }

    @property
    def digest(self) -> str:
        return digest(canonical(self.data()))

    @classmethod
    def parse(cls, data: dict) -> "ValidationProfile":
        if not isinstance(data, dict):
            raise Rejected("Supply the complete trusted validation profile.")
        try:
            profile = cls(
                image=data["image"],
                database_image=data["database_image"],
                dependency_digest=data["dependency_digest"],
                profile_id=data["profile_id"],
                version=data["version"],
            )
        except (KeyError, TypeError):
            raise Rejected("Supply the complete trusted validation profile.") from None
        if canonical(data) != canonical(profile.data()):
            raise Rejected(
                "Validation profile differs from the installed trusted recipe."
            )
        return profile

    def executor_profiles(self) -> list[Profile]:
        profiles = []
        for check_id in CHECK_IDS:
            database = (
                self.database_image
                if check_id in {"migrations", "system", "django", "mypy"}
                else ""
            )
            profiles.append(
                Profile.parse(
                    {
                        "profile_id": f"{self.profile_id}.{self.image[7:23]}.{check_id}",
                        "version": self.version,
                        "image": self.image,
                        "phase": "validation",
                        "argv": [*TASK_ARGV, check_id],
                        "limits": VALIDATION_LIMITS.__dict__,
                        "network_hosts": [],
                        "backend_secret_env": "",
                        "database_image": database,
                    }
                )
            )
        return profiles

    def setup_profile(self) -> Profile:
        return Profile.parse(
            {
                "profile_id": f"{self.profile_id}.dependencies",
                "version": self.version,
                "image": self.image,
                "phase": "setup",
                "argv": list(BUILD_ARGV),
                "limits": VALIDATION_LIMITS.__dict__,
                "network_hosts": list(PACKAGE_HOSTS),
                "backend_secret_env": "",
                "database_image": "",
            }
        )


def validate_dependency_provenance(
    value: dict,
    profile: ValidationProfile,
    *,
    parent: ValidationProfile | None = None,
    run_id: str | None = None,
) -> dict:
    from .contracts import identifier, sha256
    from .qualification import REQUIRED_CHECKS

    expected = {
        "schema_version",
        "build_id",
        "run_id",
        "source_digest",
        "requirements_digest",
        "dependency_digest",
        "parent_image",
        "parent_profile_digest",
        "parent_runtime_binding",
        "setup_operation_id",
        "setup_log_digest",
        "derived_image",
        "qualification_binding_digest",
        "qualification_report_digest",
        "boundary_checks",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise Rejected("Supply complete protected dependency-build provenance.")
    for key in ("build_id", "run_id", "setup_operation_id"):
        identifier(value[key])
    for key in (
        "source_digest",
        "requirements_digest",
        "dependency_digest",
        "parent_profile_digest",
        "parent_runtime_binding",
        "setup_log_digest",
        "qualification_binding_digest",
        "qualification_report_digest",
    ):
        sha256(value[key])
    for key in ("parent_image", "derived_image"):
        if not isinstance(value[key], str) or not value[key].startswith("sha256:"):
            raise Rejected("Dependency provenance requires immutable image identities.")
        sha256(value[key][7:])
    if (
        value["derived_image"] != profile.image
        or value["requirements_digest"] != profile.dependency_digest
        or value["setup_operation_id"] != value["build_id"]
    ):
        raise Rejected(
            "Dependency provenance differs from the selected validation image."
        )
    if not isinstance(value["boundary_checks"], list) or sorted(
        value["boundary_checks"]
    ) != sorted(REQUIRED_CHECKS):
        raise Rejected("Dependency image lacks complete boundary qualification.")
    if parent is not None and (
        value["parent_image"] != parent.image
        or value["parent_profile_digest"] != parent.digest
    ):
        raise Rejected(
            "Dependency image parent differs from the configured trusted image."
        )
    if run_id is not None and value["run_id"] != run_id:
        raise Rejected("Dependency images are scoped to their original run.")
    return value


def main():
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Write a private immutable validation profile from reviewed image IDs."
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = ValidationProfile(
        args.image, args.database_image, digest(args.requirements.read_bytes())
    )
    descriptor = os.open(
        args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(profile.data()) + b"\n")


if __name__ == "__main__":
    main()
