"""Recover only the supervisor's registered, credential-free checkout."""

import json
import shutil

from .processes import ProcessTracker
from .recovery import ReconciliationRequired


class NativeClone:
    def __init__(self, host, manager, transport, item):
        self.host, self.transport = host, transport
        self.directory = manager.directory / host.run_id
        self.checkout = self.directory / "checkout"
        self.repository = item["repository_url"]
        self.arguments = [
            "clone",
            "--no-local",
            "--no-checkout",
            "--no-recurse-submodules",
            "--",
            self.repository,
            str(self.checkout),
        ]
        self.binding = {"repository_url": self.repository, "arguments": self.arguments}

    def record(self):
        record = self.host.native.get("clone")
        if record and record["binding"] != self.binding:
            raise ReconciliationRequired(
                "Native clone identity changed; retain the owned checkout."
            )
        return record

    def recover(self):
        record = self.record()
        if (self.directory / "workspace.json").exists():
            return False
        if not self.checkout.exists() and not self.checkout.is_symlink():
            return False
        if not record or self.checkout.is_symlink() or not self.checkout.is_dir():
            raise ReconciliationRequired(
                "An unregistered partial checkout requires operator reconciliation."
            )
        ProcessTracker(self.host.journal, self.host.state).recover()
        if record["state"] == "completed":
            return True
        shutil.rmtree(self.checkout)
        return False

    def command(self, arguments, cwd=None):
        if arguments[0] != "clone":
            return self.transport.command(
                arguments, self.repository, self.host.journal.directory, cwd
            )
        if arguments != self.arguments:
            raise ReconciliationRequired(
                "Native clone arguments differ from the owned intent."
            )
        record = self.record()
        if record is None:
            record = {"binding": self.binding, "state": "prepared"}
            self.host.native["clone"] = record
            self.host.save()

        def launch():
            record["state"] = "dispatched"
            self.host.save()
            result = self.transport._command(
                arguments, self.repository, self.host.journal.directory, cwd
            )
            record["state"] = "completed"
            self.host.save()
            return result

        key = "git:" + json.dumps([self.repository, arguments])
        return self.host.read(key, launch)
