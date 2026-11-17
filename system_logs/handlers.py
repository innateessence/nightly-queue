import logging
import threading
import traceback
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

RETENTION_DAYS = 7
MAX_ENTRIES = 10000
_state = threading.local()


def retention_cutoff():
    return timezone.now() - timedelta(days=RETENTION_DAYS)


@sensitive_variables()
def safe_record(record):
    from .redaction import redact, secret_values

    try:
        source = record.name
        message = record.getMessage()
        if record.name.startswith(("django.request", "django.security")):
            request = getattr(record, "request", None)
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None) or "unmatched route"
            message = f"HTTP {getattr(record, 'status_code', 'error')} · {route}"
        details = ""
        if record.exc_info and record.exc_info[0]:
            frames = traceback.extract_tb(record.exc_info[2])
            details = record.exc_info[0].__name__ + "\n"
            details += "\n".join(
                f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames
            )
        if len(source) + len(message) + len(details) > 65536:
            raise ValueError("Oversized log record")
        secrets = secret_values()
        return (
            redact(source, secrets)[:120],
            redact(message, secrets)[:4000],
            redact(details, secrets)[:8000],
        )
    except Exception:
        return (
            "application",
            "Log message withheld because credential redaction is unavailable.",
            "",
        )


class SystemLogHandler(logging.Handler):
    @sensitive_variables()
    def emit(self, record):
        if record.levelno < logging.INFO or getattr(_state, "active", False):
            return
        _state.active = True
        try:
            from .models import SystemLog

            with transaction.atomic():
                source, message, details = safe_record(record)
                levels = (
                    (50, "critical"),
                    (40, "error"),
                    (30, "warning"),
                    (20, "info"),
                )
                level = next(
                    value for threshold, value in levels if record.levelno >= threshold
                )
                SystemLog.objects.create(
                    level=level, source=source, message=message, details=details
                )
                SystemLog.objects.filter(created_at__lt=retention_cutoff()).delete()
                oldest = SystemLog.objects.order_by("-pk").values_list("pk", flat=True)[
                    MAX_ENTRIES - 1 : MAX_ENTRIES
                ]
                boundary = next(iter(oldest), None)
                if boundary is not None:
                    SystemLog.objects.filter(pk__lt=boundary).delete()
        except Exception:
            # Logging must not fail requests or recurse when storage is unavailable.
            pass
        finally:
            _state.active = False
