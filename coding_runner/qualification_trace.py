import json

from .backends import BackendOutputError
from .codex_backend import ProcessTransport, parse_response
from .release import TRANSPORT


class ModelTraceTransport(ProcessTransport):
    def __init__(self, journal, state, account, credential_version, stage="research"):
        super().__init__()
        self.journal, self.state = journal, state
        self.account, self.credential_version = account, credential_version
        self.stage = stage

    def exchange(self, payload, **kwargs):
        response = super().exchange(payload, **kwargs)
        event = parse_response(response, self.stage)
        value = response.get("response", {})
        response_id = value.get("id")
        token = payload.get("token")
        if token and json.dumps(token)[1:-1] in json.dumps([event.data, response_id]):
            raise BackendOutputError("Model evidence contains credential material.")
        if (
            value.get("status") == "completed"
            and isinstance(response_id, str)
            and response_id
        ):
            evidence = self.state.setdefault("release_evidence", {})
            traces = evidence.setdefault("model_responses", [])
            traces.append(
                {
                    "backend": self.account["backend"],
                    "credential_id": self.account["credential_id"],
                    "model": payload["request"]["model"],
                    "secret_version": self.credential_version,
                    "response_id": response_id,
                    "transport": TRANSPORT,
                }
            )
            self.journal.save(self.state)
        return response
