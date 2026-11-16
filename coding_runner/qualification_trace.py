import re

from .backend_transports import TRANSPORTS
from .backends import BackendError, BackendOutputError
from .proposal_backend import ProcessTransport, require_secret_free, validate_model_data
from .release_evidence import valid_response_id


class ModelTraceTransport(ProcessTransport):
    def __init__(self, journal, state, account, credential_version, stage="research"):
        super().__init__()
        self.journal, self.state = journal, state
        self.account, self.credential_version = account, credential_version
        self.stage = stage

    def command(self):
        if self.account["backend"] == "claude":
            from .claude_backend import ClaudeProcessTransport

            return ClaudeProcessTransport().command()
        if self.account["backend"] == "opencode":
            from .opencode_backend import OpenCodeProcessTransport

            return OpenCodeProcessTransport().command()
        if self.account["backend"] != "codex":
            raise BackendError("Controlled transport has no verified worker.")
        return super().command()

    def exchange(self, payload, **kwargs):
        response = super().exchange(payload, **kwargs)
        if self.account["backend"] == "opencode":
            from .opencode_backend import (
                parse_response as parse_opencode_response,
                validate_response_model,
            )

            provider = payload["connection"]["provider"]
            event = parse_opencode_response(response, self.stage, provider)
            validate_response_model(response, payload["request"]["model"], provider)
        else:
            if self.account["backend"] == "claude":
                from .claude_backend import parse_response
            elif self.account["backend"] == "codex":
                from .codex_backend import parse_response
            else:
                raise BackendError("Controlled transport has no verified parser.")
            event = parse_response(response, self.stage)
        value = response.get("response", {})
        receipt = {
            "receipt_kind": "provider_response_id",
            "response_id": value.get("id"),
        }
        token = payload.get("token")
        if self.account["backend"] == "opencode":
            selected = payload["connection"]
            token = selected["api_key"]
            receipt.update(
                inference_provider=selected["provider"], endpoint=selected["base_url"]
            )
            if selected["provider"] == "ollama":
                observed = response.get("receipt", {})
                response_digest = observed.get("response_digest")
                if (
                    not isinstance(response_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", response_digest) is None
                ):
                    raise BackendError(
                        "Controlled Ollama transport lacks a raw HTTP response receipt."
                    )
                receipt = {
                    **receipt,
                    "receipt_kind": "local_response_digest",
                    "response_digest": response_digest,
                }
                receipt.pop("response_id")
                if "ollama_version" in observed:
                    receipt["ollama_version"] = observed["ollama_version"]
        validate_model_data(receipt)
        if token:
            require_secret_free([event.data, receipt], token)
        if receipt["receipt_kind"] != "local_response_digest" and not valid_response_id(
            receipt.get("response_id")
        ):
            raise BackendOutputError("Model response identity is invalid.")
        evidence = self.state.setdefault("release_evidence", {})
        traces = evidence.setdefault("model_responses", [])
        traces.append(
            {
                "backend": self.account["backend"],
                "credential_id": self.account["credential_id"],
                "model": payload["request"]["model"],
                "secret_version": self.credential_version,
                "transport": TRANSPORTS[self.account["backend"]],
                **receipt,
            }
        )
        self.journal.save(self.state)
        return response
