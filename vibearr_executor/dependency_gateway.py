"""Route dependency operations to the current dedicated worker."""

from types import SimpleNamespace

from .server import Handler


class LocalClient:
    def __init__(self, runtime):
        self.runtime = runtime

    def capabilities(self):
        return self.runtime.capabilities()

    def request(self, path, data=None):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(runtime=self.runtime)
        handler.command = "POST"
        handler.path = path
        return handler.route(data or {})


class DependencyGateway:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory, self.artifacts = runtime.directory, runtime.artifacts
        self.local = LocalClient(runtime)
        self.clients = {phase: self.local for phase in ("agent", "setup", "validation")}

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def client(self, _operation_id):
        return self.local
