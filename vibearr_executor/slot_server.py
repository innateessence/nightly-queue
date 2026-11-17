"""Authenticated PID 1 for a preprovisioned Compose slot."""

import hashlib
import os
from pathlib import Path
import signal
import threading

from .server import Handler
from .slot_http import SlotServer
from .slot_database import Database
from .slot_network import address, configure
from .slot_profiles import profiles
from .slots import SlotRuntime


def main():
    os.umask(0o077)
    phase = os.environ["VIBEARR_SLOT_PHASE"]
    token = Path("/run/vibearr/control-token").read_text().strip()
    if len(token) < 32:
        raise RuntimeError("Worker control token is missing.")
    database = None
    database_host = os.environ.get("VIBEARR_TEST_DATABASE", "")
    if database_host:
        password = Path("/run/database/password").read_text().strip()
        database = Database(database_host, password)
    image = os.environ["VIBEARR_SLOT_IMAGE_ID"]
    database_image = os.environ.get("VIBEARR_TEST_DATABASE_IMAGE_ID", "")
    requirements = Path("/opt/validation/project-requirements.txt").read_bytes()
    selected = profiles(
        phase, image, database_image, hashlib.sha256(requirements).hexdigest()
    )

    def network(profile):
        destinations = []
        proxy = []
        if profile.network_hosts:
            host = "model-proxy" if phase == "agent" else "package-proxy"
            proxy = [(address(host), 3128)]
            destinations.extend(proxy)
        if profile.database_image and database:
            destinations.append((database.host, 5432))
        configure(destinations)
        return proxy

    configure([])
    runtime = SlotRuntime(
        Path("/control"), selected, configure_network=network, database=database
    )
    runtime.control_token = token
    if database:
        database.reset("")
    runtime.capabilities()
    stopping = threading.Event()
    with SlotServer(("0.0.0.0", 8791), Handler) as server:
        server.runtime, server.token = runtime, token

        def stop(_signal, _frame):
            runtime.cancel.set()
            stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
