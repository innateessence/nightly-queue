"""Authenticated PID 1 for a preprovisioned Compose slot."""

import hashlib
from contextlib import ExitStack
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
from .account_bridge import AccountBridge, BridgeHandler, BridgeServer, BRIDGE_PORT
from .account_client import AccountClient


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
    with ExitStack() as stack:
        server = stack.enter_context(SlotServer(("0.0.0.0", 8791), Handler))
        server.runtime, server.token = runtime, token
        bridge_server = None
        bridge_thread = None
        if phase == "agent":
            client = AccountClient(
                os.environ.get("VIBEARR_CODEX_ACCOUNT_URL", "http://codex-account:8793")
            )
            runtime.account_bridge = AccountBridge(runtime, client)
            runtime.account_bridge.reconcile()
            bridge_server = stack.enter_context(
                BridgeServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
            )
            bridge_server.bridge = runtime.account_bridge
            bridge_thread = threading.Thread(
                target=bridge_server.serve_forever, daemon=True
            )
            bridge_thread.start()

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
            if bridge_server is not None:
                bridge_server.shutdown()
            if bridge_thread is not None:
                bridge_thread.join(timeout=5)


if __name__ == "__main__":
    main()
