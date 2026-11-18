"""Authenticated private account control; credentials never enter argv or URLs."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .codex_account import private_bytes
from .contracts import canonical, identifier


class AccountUnavailable(ValueError):
    pass


class NoAccountRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, *_args, **_kwargs):
        response.close()
        raise AccountUnavailable("Account control redirects are forbidden.")


class AccountClient:
    def __init__(self, url, token="", *, timeout=90):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        fixed = parsed.hostname == "codex-account" and parsed.port == 8793
        if (
            parsed.scheme != "http"
            or not (local or fixed)
            or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
            or parsed.path not in {"", "/"}
        ):
            raise AccountUnavailable(
                "Select the private Docker account service origin."
            )
        if token and (not isinstance(token, str) or len(token) < 32):
            raise AccountUnavailable(
                "Private account control authentication is missing."
            )
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoAccountRedirect()
        )

    @classmethod
    def from_environment(cls):
        url = os.environ.get("VIBEARR_CODEX_ACCOUNT_URL", "")
        path = os.environ.get("VIBEARR_CODEX_ACCOUNT_TOKEN_FILE", "")
        if not url and not path:
            return None
        if not url or not path:
            raise AccountUnavailable(
                "Configure the protected Codex account service and its control token."
            )
        try:
            token = private_bytes(Path(path)).decode().strip()
        except (OSError, ValueError, UnicodeError):
            raise AccountUnavailable(
                "Initialize the private Docker account control token first."
            ) from None
        return cls(url, token)

    def request(self, path, data=None, *, token=None, timeout=None):
        selected = self.token if token is None else token
        request = urllib.request.Request(
            self.url + path,
            data=canonical(data) if data is not None else None,
            headers={
                "Authorization": "Bearer " + selected,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                content = response.read(65537)
                if len(content) > 65536:
                    raise AccountUnavailable(
                        "Account control response exceeded its bound."
                    )
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise AccountUnavailable("Account control response is invalid.")
                return value
        except (urllib.error.URLError, OSError, ValueError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise AccountUnavailable(
                "Codex account control is unavailable; check the protected service and account login."
            ) from None

    def healthy(self):
        value = self.request("/health")
        if (
            value.get("runtime") != "codex-account-v1"
            or type(value.get("protocol_version")) is not int
            or value["protocol_version"] != 1
        ):
            raise AccountUnavailable(
                "The protected account service protocol is unsupported."
            )
        return value

    def prepare(self, binding, secret):
        return self.request("/v1/prepare", {"binding": binding, "secret": secret})

    def register(self, credential, secret):
        return self.request(
            "/v1/accounts/register", {"credential": credential, "secret": secret}
        )

    def renew(self, operation, token, identity, request_id):
        identifier(operation)
        return self.request(
            f"/v1/lease/{operation}/renew",
            {"execution_identity": identity, "request_id": request_id},
            token=token,
            timeout=40,
        )

    def close(self, operation, token, identity):
        identifier(operation)
        return self.request(
            f"/v1/lease/{operation}/close",
            {"execution_identity": identity},
            token=token,
            timeout=10,
        )
