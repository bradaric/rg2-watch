"""Thin client for the shared local OpenCode service's session API."""
import base64
import json
import logging
import subprocess
import uuid
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class OpenCodeError(Exception):
    pass


class OpenCodeBadRequest(OpenCodeError):
    """The service rejected one form/request."""


class OpenCodeGlobalError(OpenCodeError):
    """Authentication, access, or model configuration affects every form."""


class OpenCodeUnavailable(OpenCodeError):
    """The service/provider is temporarily unavailable or rate limited."""


class OpenCodeClient:
    def __init__(self, http: httpx.Client, directory: str | Path):
        self.http = http
        self.directory = str(Path(directory).resolve())

    @classmethod
    def from_service(cls, directory: str | Path = ".") -> "OpenCodeClient":
        """Discover the running shared service and its local Basic-auth password."""
        try:
            url = subprocess.check_output(
                ["opencode", "service", "status"], text=True, timeout=10).strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"unexpected service address: {url!r}")
            service = json.loads(
                (Path.home() / ".config/opencode/service.json").read_text(encoding="utf-8"))
            password = service["password"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise OpenCodeUnavailable(f"cannot connect to OpenCode service: {exc}") from exc
        return cls(httpx.Client(base_url=url, auth=("opencode", password)), directory)

    def _request(self, method: str, path: str, *, json_body=None, params=None,
                 timeout: float = 1200.0):
        try:
            response = self.http.request(method, path, json=json_body, params=params, timeout=timeout)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            raise OpenCodeUnavailable(f"OpenCode service: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (401, 403, 404):
            raise OpenCodeGlobalError(
                f"OpenCode HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code == 429 or response.status_code >= 500:
            raise OpenCodeUnavailable(
                f"OpenCode HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise OpenCodeBadRequest(
                f"OpenCode HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json() if response.content else None
        except ValueError as exc:
            raise OpenCodeUnavailable(f"invalid response from OpenCode service: {exc}") from exc

    def analyze(self, tiles, prompt: str, schema: dict, provider: str, model: str,
                variant: str) -> dict:
        """Run one tool-free temporary agent session and return its assistant message."""
        session_id = None
        try:
            created = self._request("POST", "/api/session", json_body={
                "title": f"rg2-watch-{uuid.uuid4().hex[:12]}",
                "agent": "rg2-analyzer",
                "model": {"providerID": provider, "id": model, "variant": variant},
                "location": {"directory": self.directory},
                "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            })
            session_id = created["data"]["id"]
            files = [{
                "uri": "data:image/jpeg;base64," + base64.standard_b64encode(tile.jpeg).decode("ascii"),
                "name": f"{tile.name}.jpg",
                "description": f"Image {i + 1} of {len(tiles)}, in document order",
            } for i, tile in enumerate(tiles)]
            names = ", ".join(tile.name for tile in tiles)
            text = (f"{prompt}\n\nReturn JSON matching this exact JSON Schema:\n"
                    f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    f"Images in order: {names}.")
            self._request("POST", f"/api/session/{session_id}/prompt",
                          json_body={"text": text, "files": files})
            self._request("POST", f"/api/experimental/session/{session_id}/wait")
            response = self._request("GET", f"/api/session/{session_id}/message",
                                     params={"type": "assistant", "order": "desc", "limit": 5})
            messages = [m for m in response.get("data", []) if m.get("type") == "assistant"]
            if not messages:
                raise OpenCodeUnavailable("OpenCode returned no assistant response")
            message = messages[0]
            if message.get("error"):
                raise OpenCodeGlobalError(f"OpenCode assistant error: {message['error']}")
            return message
        except (KeyError, TypeError, AttributeError) as exc:
            raise OpenCodeUnavailable(f"unexpected OpenCode response: {exc!r}") from exc
        finally:
            if session_id is not None:
                try:
                    self._request("DELETE", f"/api/session/{session_id}", timeout=60)
                except OpenCodeError as exc:
                    log.warning("could not delete temporary OpenCode session %s: %s", session_id, exc)
