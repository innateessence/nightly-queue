"""Only for exporting an existing SQLite database."""

from .environment import getenv
from pathlib import Path

from .settings import *  # noqa: F403
from .settings import BASE_DIR

database_path = Path(getenv("NQ_DB_PATH", str(BASE_DIR / "db.sqlite3")))

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": f"{database_path.resolve().as_uri()}?mode=ro",
    }
}
