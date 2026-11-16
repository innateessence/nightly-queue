"""Bound safe transport reads across supervisor restarts."""

import hashlib
import time

from .delivery import DeliveryReadUnavailable


class NativeReadWait(Exception):
    pass


class NativeReadExhausted(Exception):
    pass


def read(host, key, operation):
    identity = hashlib.sha256(key.encode()).hexdigest()
    records = host.native.setdefault("transport_reads", {})
    record = records.setdefault(identity, {"attempts": 0, "retry_at": 0})
    if record["attempts"] >= 3:
        raise NativeReadExhausted("Remote read failed after three bounded attempts.")
    if time.time() < record["retry_at"]:
        raise NativeReadWait("Waiting for remote read backoff or GitHub rate limit.")
    record["attempts"] += 1
    host.save()
    try:
        result = operation()
    except DeliveryReadUnavailable as exc:
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
    del records[identity]
    host.save()
    return result
