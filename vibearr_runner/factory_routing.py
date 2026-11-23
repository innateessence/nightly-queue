"""Frozen factory settings and trusted, additive reviewer routing."""

from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import PurePosixPath


AGENT_ROLES = (
    "intake",
    "specification",
    "build",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
)
REVIEWERS = tuple(role for role in AGENT_ROLES if role.startswith("reviewer-"))
SURFACE_REVIEWERS = {
    "security": ("reviewer-security",),
    "ui": ("reviewer-ux", "reviewer-ui"),
}
PROTECTED_PATHS = (
    ".github/workflows/**",
    "vibearr_executor/validation*",
    "vibearr_executor/*profiles.py",
    "deploy/docker/*Dockerfile*",
    "test.sh",
)
PATH_RULES = (
    {"pattern": "**/templates/**", "surfaces": ["ui"]},
    {"pattern": "templates/**", "surfaces": ["ui"]},
    {"pattern": "**/static/**", "surfaces": ["ui"]},
    {"pattern": "static/**", "surfaces": ["ui"]},
    {"pattern": "*.html", "surfaces": ["ui"]},
    {"pattern": "*.css", "surfaces": ["ui"]},
    {"pattern": "*.js", "surfaces": ["ui"]},
    {"pattern": "*.jsx", "surfaces": ["ui"]},
    {"pattern": "*.tsx", "surfaces": ["ui"]},
    {"pattern": "*.vue", "surfaces": ["ui"]},
    {"pattern": "credentials/**", "surfaces": ["security"]},
    {"pattern": "runner_control/**", "surfaces": ["security"]},
    {"pattern": "vibearr_runner/**", "surfaces": ["security"]},
    {"pattern": "vibearr_app/**", "surfaces": ["security"]},
    {"pattern": "vibearr_executor/**", "surfaces": ["security"]},
    {"pattern": "*auth*.py", "surfaces": ["security"]},
    {"pattern": "*credential*.py", "surfaces": ["security"]},
    {"pattern": "*security*.py", "surfaces": ["security"]},
    {"pattern": "*middleware*.py", "surfaces": ["security"]},
    {"pattern": "work_queue/services.py", "surfaces": ["security"]},
    {"pattern": "work_queue/api.py", "surfaces": ["security"]},
    {"pattern": "work_queue/github.py", "surfaces": ["security"]},
    {"pattern": "work_queue/import*.py", "surfaces": ["security"]},
    {"pattern": "requirements*.txt", "surfaces": ["security"]},
    {"pattern": "pyproject.toml", "surfaces": ["security"]},
    {"pattern": "uv.lock", "surfaces": ["security"]},
    {"pattern": "poetry.lock", "surfaces": ["security"]},
    {"pattern": "Pipfile*", "surfaces": ["security"]},
    {"pattern": "package.json", "surfaces": ["security"]},
    {"pattern": "**/package.json", "surfaces": ["security"]},
    {"pattern": "*package-lock.json", "surfaces": ["security"]},
    {"pattern": "*pnpm-lock.yaml", "surfaces": ["security"]},
    {"pattern": "*yarn.lock", "surfaces": ["security"]},
    {"pattern": "Dockerfile*", "surfaces": ["security"]},
    {"pattern": "docker-compose*", "surfaces": ["security"]},
    {"pattern": "deploy/**", "surfaces": ["security"]},
    {"pattern": ".github/**", "surfaces": ["security"]},
)
KNOWN_PATHS = (
    "*.py",
    "*.md",
    "*.txt",
    "*.toml",
    "*.ini",
    "*.html",
    "*.css",
    "*.js",
    "*.jsx",
    "*.tsx",
    "*.vue",
    "Dockerfile*",
    "docker-compose*",
    "deploy/**",
    ".github/**",
)


class FactoryRoutingError(ValueError):
    pass


def digest(value):
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError, UnicodeEncodeError):
        raise FactoryRoutingError(
            "Factory settings must be finite Unicode JSON."
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def default_configuration():
    return {
        "schema_version": 1,
        "enabled": False,
        "roles": {},
        "max_repairs": 2,
        "protected_paths": list(PROTECTED_PATHS),
        "path_rules": deepcopy(list(PATH_RULES)),
        "known_paths": list(KNOWN_PATHS),
        "unknown_path_reviewers": list(REVIEWERS),
    }


def checked_text(value, name, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or "\x00" in value
    ):
        raise FactoryRoutingError(f"Invalid factory {name}.")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise FactoryRoutingError(f"Invalid factory {name} Unicode.") from None
    return value


def checked_pattern(value):
    value = checked_text(value, "path pattern", 500)
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise FactoryRoutingError(
            "Factory path patterns must be relative repository paths."
        )
    return value


def text_list(value, name, allowed=None, maximum=100):
    if not isinstance(value, list) or len(value) > maximum:
        raise FactoryRoutingError(f"Invalid factory {name} list.")
    result = []
    for entry in value:
        entry = checked_text(entry, name, 500)
        if allowed is not None and entry not in allowed:
            raise FactoryRoutingError(f"Unsupported factory {name}: {entry}.")
        if entry not in result:
            result.append(entry)
    return result


def normalize_configuration(value):
    defaults = default_configuration()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise FactoryRoutingError("Enter supported factory configuration fields.")
    result = {**defaults, **deepcopy(value)}
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise FactoryRoutingError("Unsupported factory configuration version.")
    if type(result["enabled"]) is not bool:
        raise FactoryRoutingError("Factory enabled must be a boolean.")
    if type(result["max_repairs"]) is not int or not 0 <= result["max_repairs"] <= 10:
        raise FactoryRoutingError("Factory repairs must be between zero and ten.")
    roles = result["roles"]
    if not isinstance(roles, dict) or set(roles) - set(AGENT_ROLES):
        raise FactoryRoutingError("Configure supported factory agent roles.")
    for role, settings in roles.items():
        if not isinstance(settings, dict) or set(settings) - {"model", "instructions"}:
            raise FactoryRoutingError("Roles may only override model and instructions.")
        checked_text(settings.get("model", ""), "model", 200, empty=True)
        checked_text(
            settings.get("instructions", ""), "instructions", 20000, empty=True
        )
    protected = text_list(result["protected_paths"], "protected path")
    result["protected_paths"] = sorted(
        set(PROTECTED_PATHS) | set(map(checked_pattern, protected))
    )
    if len(result["protected_paths"]) > 100:
        raise FactoryRoutingError(
            "Configure at most 100 protected paths including required paths."
        )
    known = text_list(result["known_paths"], "known path")
    result["known_paths"] = sorted(set(map(checked_pattern, known)))
    unknown = text_list(result["unknown_path_reviewers"], "reviewer", REVIEWERS)
    result["unknown_path_reviewers"] = sorted(set(unknown) | set(REVIEWERS))
    rules = result["path_rules"]
    if not isinstance(rules, list) or len(rules) > 100:
        raise FactoryRoutingError("Configure at most 100 factory path rules.")
    normalized = {}
    for rule in [*PATH_RULES, *rules]:
        if not isinstance(rule, dict) or set(rule) != {"pattern", "surfaces"}:
            raise FactoryRoutingError("Factory rules need pattern and surfaces.")
        pattern = checked_pattern(rule["pattern"])
        surfaces = text_list(rule["surfaces"], "surface", SURFACE_REVIEWERS)
        if not surfaces:
            raise FactoryRoutingError("Factory path rules need at least one surface.")
        normalized.setdefault(pattern, set()).update(surfaces)
    result["path_rules"] = [
        {"pattern": pattern, "surfaces": sorted(surfaces)}
        for pattern, surfaces in sorted(normalized.items())
    ]
    if len(result["path_rules"]) > 100:
        raise FactoryRoutingError(
            "Configure at most 100 factory path rules including required routes."
        )
    digest(result)
    return result


def changed_paths(paths):
    if not isinstance(paths, list) or not paths or len(paths) > 10000:
        raise FactoryRoutingError("Record a bounded nonempty changed-path inventory.")
    result = set()
    for entry in paths:
        values = [entry]
        if isinstance(entry, dict):
            if set(entry) - {"path", "old_path", "status"} or not entry.get("path"):
                raise FactoryRoutingError("Invalid changed-path entry.")
            values = [entry["path"]]
            if entry.get("old_path"):
                values.append(entry["old_path"])
        for value in values:
            value = checked_text(value, "changed path", 4096)
            parts = value.split("/")
            if (
                PurePosixPath(value).is_absolute()
                or "\\" in value
                or any(part in ("", ".", "..") for part in parts)
            ):
                raise FactoryRoutingError(
                    "Changed paths must be canonical relative paths."
                )
            result.add(value)
    return sorted(result)


def route(configuration, intake, paths, *, previous=None):
    settings = normalize_configuration(configuration)
    paths = changed_paths(paths)
    if not isinstance(intake, dict):
        raise FactoryRoutingError("Record the accepted intake assessment.")
    surfaces = set(text_list(intake.get("surfaces", []), "intake surface", maximum=16))
    risk = intake.get("risk", "high")
    if risk not in ("low", "medium", "high"):
        risk = "high"
    unknown = bool(surfaces - set(SURFACE_REVIEWERS))
    reviewers = {"reviewer-code"}
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("settings_digest") != digest(
            settings
        ):
            raise FactoryRoutingError(
                "Previous routing belongs to different factory settings."
            )
        reviewers.update(
            text_list(previous.get("required_reviewers"), "reviewer", REVIEWERS)
        )
        surfaces.update(
            text_list(previous.get("surfaces"), "surface", SURFACE_REVIEWERS)
        )
        if previous.get("risk") == "high":
            risk = "high"
        elif previous.get("risk") == "medium" and risk == "low":
            risk = "medium"
    protected = []
    for path in paths:
        matched = False
        for rule in settings["path_rules"]:
            if fnmatchcase(path, rule["pattern"]):
                surfaces.update(rule["surfaces"])
                matched = True
        if not matched and not any(
            fnmatchcase(path, pattern) for pattern in settings["known_paths"]
        ):
            unknown = True
        if any(fnmatchcase(path, pattern) for pattern in settings["protected_paths"]):
            protected.append(path)
            surfaces.add("security")
            risk = "high"
    if unknown:
        reviewers.update(settings["unknown_path_reviewers"])
        surfaces.update(SURFACE_REVIEWERS)
        risk = "high"
    for surface in surfaces & set(SURFACE_REVIEWERS):
        reviewers.update(SURFACE_REVIEWERS[surface])
    surfaces.intersection_update(SURFACE_REVIEWERS)
    return {
        "risk": risk,
        "surfaces": sorted(surfaces),
        "required_reviewers": sorted(reviewers),
        "rule_digest": digest(
            {
                key: settings[key]
                for key in (
                    "path_rules",
                    "known_paths",
                    "protected_paths",
                    "unknown_path_reviewers",
                )
            }
        ),
        "paths_digest": digest(paths),
        "settings_digest": digest(settings),
        "protected_paths": protected,
    }


def capability_checker():
    from .native_sessions import NativeSessionError

    try:
        from .native_conformance import require_factory_capabilities as require_roles
    except ImportError:
        raise NativeSessionError(
            "Install qualified factory role capabilities before enabling factory execution."
        ) from None
    return require_roles


def require_factory_capabilities(configuration, capabilities):
    if not configuration or not configuration.get("enabled"):
        return {}
    roles = ("intake", "specification", "build", "reviewer-code", "decision")
    return capability_checker()(capabilities, roles)


def require_route_capabilities(routing, capabilities):
    reviewers = text_list(routing.get("required_reviewers"), "reviewer", REVIEWERS)
    if "reviewer-code" not in reviewers:
        raise FactoryRoutingError("Code review is always required.")
    browser = any(role in reviewers for role in ("reviewer-ux", "reviewer-ui"))
    return capability_checker()(capabilities, reviewers, browser=browser)
