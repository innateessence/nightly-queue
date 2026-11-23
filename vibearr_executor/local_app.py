"""Fixed candidate Django app recipe for worker-local browser phases."""

import ctypes
import errno
import os
from pathlib import Path
import secrets
import resource
import sys
from urllib.parse import urlsplit

APP_UID = 10003


def drop_identity():
    if os.getuid() != 0:
        raise RuntimeError("Candidate app initialization requires the trusted worker.")
    libc = ctypes.CDLL(None, use_errno=True)
    for capability in range(64):
        if libc.prctl(24, capability, 0, 0, 0) and ctypes.get_errno() != errno.EINVAL:
            raise OSError(ctypes.get_errno(), "Cannot remove service capability.")
    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError(
            ctypes.get_errno(), "Cannot prohibit service privilege escalation."
        )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**2, 4 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    os.setgroups([])
    os.setgid(APP_UID)
    os.setuid(APP_UID)
    os.umask(0o077)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "serve"}:
        raise RuntimeError("Choose a registered browser application action.")
    drop_identity()
    work = Path("/work")
    directory = Path(os.environ["VIBEARR_BROWSER_APP_ROOT"])
    database = urlsplit(os.environ["VIBEARR_DISPOSABLE_DATABASE_URL"])
    if (
        database.scheme != "postgresql"
        or database.hostname != "127.0.0.1"
        or database.username != "task"
    ):
        raise RuntimeError(
            "Browser application requires its local disposable task database."
        )
    os.chdir(work)
    sys.path.insert(0, str(work))
    os.environ["DJANGO_SETTINGS_MODULE"] = "vibearr_app.settings"
    from django.conf import settings

    settings.DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database.path[1:],
            "USER": "task",
            "PASSWORD": database.password,
            "HOST": "127.0.0.1",
            "PORT": str(database.port or 5432),
        }
    }
    settings.SECRET_KEY = secrets.token_hex(32)
    settings.DEBUG = False
    settings.ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    settings.STATIC_ROOT = directory / "static"
    settings.NQ_API_TOKEN = settings.VIBEARR_API_TOKEN = ""
    settings.LOGGING = {"version": 1, "disable_existing_loggers": True}
    import django

    django.setup()
    if sys.argv[1] == "prepare":
        from django.core.management import call_command

        call_command("migrate", interactive=False, verbosity=0)
        call_command("collectstatic", interactive=False, verbosity=0)
        return
    from django.core.wsgi import get_wsgi_application
    from gunicorn.app.base import BaseApplication

    class Application(BaseApplication):
        def load_config(self):
            for key, value in {
                "bind": "127.0.0.1:8765",
                "workers": 1,
                "threads": 2,
                "timeout": 30,
                "accesslog": None,
                "errorlog": "-",
            }.items():
                self.cfg.set(key, value)

        def load(self):
            return get_wsgi_application()

    Application().run()


if __name__ == "__main__":
    main()
