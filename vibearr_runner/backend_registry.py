"""Provider account usage remains unsupported until an actual quota source exists."""

from .gates import UsageReading


class UnsupportedUsage:
    def read_usage(self, account, now):
        return UsageReading(supported=False)


class BackendRegistry:
    def __init__(self, factories=None):
        self.factories = (
            factories
            if factories is not None
            else {
                "codex": UnsupportedUsage,
                "claude": UnsupportedUsage,
                "opencode": UnsupportedUsage,
            }
        )

    def usage_adapters(self, accounts):
        return {
            (account["backend"], account["credential_id"]): self.factories[
                account["backend"]
            ]()
            for account in accounts
            if account["backend"] in self.factories
        }
