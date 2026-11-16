"""Release proof binds each selected provider to its actual model transport."""

from typing import Any

PROOF_PROTOCOL = "structured-proposals-v1"
TRANSPORTS = {
    "codex": "codex-responses-structured-proposals-v1",
    "claude": "claude-messages-structured-proposals-v1",
    "opencode": "opencode-provider-structured-proposals-v1",
}


def qualification_matches(
    credentials: Any, account: dict[str, Any], version: Any
) -> bool:
    from .release import matches_deployment

    try:
        qualification = getattr(credentials, "qualification", lambda account: None)(
            account
        )
        if not isinstance(qualification, dict):
            return False
        expected = {
            key: account.get(key) for key in ("backend", "credential_id", "model")
        }
        expected.update(
            secret_version=version, transport=TRANSPORTS.get(account.get("backend", ""))
        )
        if (
            not expected["model"]
            or not expected["transport"]
            or type(version) is not int
        ):
            return False
        if any(qualification.get(key) != value for key, value in expected.items()):
            return False
        proof = qualification.get("verification", {})
        return (
            isinstance(proof, dict)
            and proof.get("transport") == PROOF_PROTOCOL
            and proof.get("qualified") is True
            and any(
                isinstance(row, dict)
                and all(row.get(key) == value for key, value in expected.items())
                for row in proof.get("accounts", [])
            )
            and matches_deployment(proof)
        )
    except (KeyError, TypeError, ValueError):
        return False
