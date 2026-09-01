import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def add(x, y):
    return x + y


@shared_task
def nightly_greeting():
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    logger.info("nightly_greeting fired at %s", stamp)
    return {"greeting": "good night", "at": stamp}
