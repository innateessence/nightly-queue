import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError


def normalize_repository_url(value):
    if not isinstance(value, str):
        raise ValidationError({"repository_url": "Enter a GitHub repository URL."})
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        message = "Enter a valid GitHub repository URL."
        raise ValidationError({"repository_url": message})

    message = "Use an HTTPS GitHub repository URL: https://github.com/owner/repo."
    error = {"repository_url": message}
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValidationError(error)
    if parsed.query or parsed.fragment:
        raise ValidationError(error)
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        raise ValidationError(error)
    valid_parts = all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    if not valid_parts:
        raise ValidationError(error)
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not repository or repository in {".", ".."} or owner in {".", ".."}:
        message = "Enter a GitHub owner and repository."
        raise ValidationError({"repository_url": message})
    return f"https://github.com/{owner}/{repository}.git"


def validate_branch(value):
    if not value:
        return
    message = "Enter a valid Git branch name."
    if value.startswith(("-", "/")) or value.endswith(("/", ".")) or value == "@":
        raise ValidationError(message)
    if any(sequence in value for sequence in ("..", "@{", "//")):
        raise ValidationError(message)
    if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value):
        raise ValidationError(message)
    for part in value.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            raise ValidationError(message)
