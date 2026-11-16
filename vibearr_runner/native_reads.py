"""Bound safe transport reads across supervisor restarts."""

import hashlib
import time

from .delivery import DeliveryError, DeliveryReadUnavailable
from .processes import current_read_attempt


class NativeReadWait(Exception):
    pass


class NativeReadExhausted(Exception):
    pass


class ReadAttempt:
    def __init__(self):
        self.was_started = False
        self.no_launch_confirmed = False

    def launched(self):
        self.was_started = True
        self.no_launch_confirmed = False

    def rejected(self):
        self.no_launch_confirmed = not self.was_started


def read(host, key, operation):
    identity = hashlib.sha256(key.encode()).hexdigest()
    records = host.native.setdefault("transport_reads", {})
    record = records.setdefault(identity, {"attempts": 0, "retry_at": 0})
    if record["attempts"] >= 3:
        raise NativeReadExhausted("Remote read failed after three bounded attempts.")
    if time.time() < record["retry_at"]:
        raise NativeReadWait("Waiting for remote read backoff or GitHub rate limit.")
    previous_attempts = record["attempts"]
    record["attempts"] += 1
    host.save()
    attempt = ReadAttempt()
    token = current_read_attempt.set(attempt)
    try:
        result = operation()
    except DeliveryReadUnavailable as exc:
        if attempt.no_launch_confirmed:
            raise DeliveryError(
                "Git read could not launch; inspect the runner transport."
            ) from None
        record["retry_at"] = max(
            time.time() + 2 ** (record["attempts"] - 1), exc.retry_at
        )
        host.save()
        if record["attempts"] >= 3:
            raise NativeReadExhausted(
                "Remote read failed after three bounded attempts."
            ) from None
        raise NativeReadWait(
            "Temporary remote read failure; waiting before retry."
        ) from None
    finally:
        current_read_attempt.reset(token)
        if attempt.no_launch_confirmed:
            record["attempts"] = previous_attempts
            host.save()
    del records[identity]
    host.save()
    return result
