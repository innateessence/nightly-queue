from typing import Any, Callable

from .backends import Backend, BackendError, BackendRequest
from .codex_backend import CodexBackend
from .claude_backend import ClaudeBackend


class ScopedCredentials:
    def __init__(
        self,
        client: Any,
        *,
        run_id: str | None = None,
        claim_token: str | None = None,
        stage: str | None = None,
    ):
        self.client = client
        self.run_id = run_id
        self.claim_token = claim_token
        self.stage = stage
        if run_id is not None and (
            not claim_token or stage not in {"research", "implementation"}
        ):
            raise ValueError("Owned-run credentials require a stage and claim token.")

    def qualification(self, account):
        proof = self.client.configuration().get("release_verification", {})
        if proof.get("qualified") is not True:
            return None
        for entry in proof.get("accounts", []):
            if all(
                entry.get(key) == account.get(key)
                for key in ("backend", "credential_id", "model")
            ):
                return {**entry, "verification": proof}
        return None

    def __call__(self, account: dict[str, Any]) -> dict[str, Any]:
        if self.run_id is None:
            payload = {"scope": "preflight", **account}
            payload = {
                key: payload[key] for key in ("scope", "backend", "credential_id")
            }
        else:
            payload = {
                "scope": "run",
                "run_id": self.run_id,
                "claim_token": self.claim_token,
                "purpose": self.stage,
            }
        return self.client.request("POST", "/api/v1/runner/secrets/", payload)


class BackendRegistry:
    def __init__(
        self, factories: dict[str, Callable[[ScopedCredentials], Backend]] | None = None
    ):
        self.factories = (
            factories
            if factories is not None
            else {"codex": CodexBackend, "claude": ClaudeBackend}
        )

    def create(self, provider: str, credentials: ScopedCredentials) -> Backend:
        factory = self.factories.get(provider)
        if factory is None:
            raise BackendError("Selected backend has no verified adapter.")
        return factory(credentials)

    def stage(
        self, snapshot: dict[str, Any], stage: str, credentials: ScopedCredentials
    ) -> tuple[Backend, BackendRequest]:
        if stage not in {"research", "implementation"}:
            raise BackendError("Unknown backend stage.")
        effective = snapshot["effective"]
        provider = effective[f"{stage}_backend"]
        account = {
            "backend": provider,
            "credential_id": effective[f"{stage}_credential_id"],
            "model": effective[f"{stage}_model"],
        }
        request = BackendRequest(
            account, effective[f"{stage}_model"], effective[f"{stage}_instructions"], {}
        )
        return self.create(provider, credentials), request

    def usage_adapters(
        self, accounts: list[dict[str, Any]], credentials: ScopedCredentials
    ) -> dict[tuple[str, int | None], Backend]:
        adapters = {}
        for account in accounts:
            provider = account["backend"]
            if provider in self.factories:
                adapters[(provider, account["credential_id"])] = self.create(
                    provider, credentials
                )
        return adapters
