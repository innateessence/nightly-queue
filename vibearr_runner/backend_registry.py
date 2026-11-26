"""Subscription usage is read through the trusted runner's private mediation API."""

from pathlib import Path

from .environment import getenv

from .gates import UsageReading


class UnsupportedUsage:
    def read_usage(self, account, now):
        return UsageReading(supported=False)


class BackendRegistry:
    def __init__(self, factories=None):
        self.native_usage = factories is None
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
        token_file = getenv("VIBEARR_MEDIATION_CONTROLLER_TOKEN_FILE", "")
        if token_file and self.native_usage:
            from .compact_connections import MediationClient
            from .subscription_usage import SubscriptionUsage

            mediation = MediationClient(
                getenv("VIBEARR_MEDIATION_URL", "http://127.0.0.1:8792"),
                Path(token_file).read_text().strip(),
            )
            return {
                (account["backend"], account["credential_id"]): (
                    SubscriptionUsage(mediation)
                    if account["backend"] in {"codex", "claude"}
                    else UnsupportedUsage()
                )
                for account in accounts
            }
        return {
            (account["backend"], account["credential_id"]): self.factories[
                account["backend"]
            ]()
            for account in accounts
            if account["backend"] in self.factories
        }
