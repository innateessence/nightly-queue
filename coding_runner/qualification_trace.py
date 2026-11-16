from .backend_transports import TRANSPORTS
from .backends import BackendError, BackendOutputError
from .proposal_backend import ProcessTransport, require_secret_free
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
        if self.account["backend"] != "codex":
            raise BackendError("Controlled transport has no verified worker.")
        return super().command()

    def exchange(self, payload, **kwargs):
        response = super().exchange(payload, **kwargs)
        if self.account["backend"] == "claude":
            from .claude_backend import parse_response
        elif self.account["backend"] == "codex":
            from .codex_backend import parse_response
        else:
            raise BackendError("Controlled transport has no verified parser.")
        event = parse_response(response, self.stage)
        value = response.get("response", {})
        response_id = value.get("id")
        token = payload.get("token")
        if token:
            require_secret_free([event.data, response_id], token)
        if not valid_response_id(response_id):
            raise BackendOutputError("Model response identity is invalid.")
        evidence = self.state.setdefault("release_evidence", {})
        traces = evidence.setdefault("model_responses", [])
        traces.append(
            {
                "backend": self.account["backend"],
                "credential_id": self.account["credential_id"],
                "model": payload["request"]["model"],
                "secret_version": self.credential_version,
                "response_id": response_id,
                "transport": TRANSPORTS[self.account["backend"]],
            }
        )
        self.journal.save(self.state)
        return response
