from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .transport import JsonHttpError, UrllibJsonHttpClient


CHAT_REPLY_CHANNEL = "agentplanet.chat"
DEFAULT_CHAT_API_BASE = "https://api.agentplanet.org"


@dataclass(frozen=True)
class InterfazeChatEnvelope:
    chat_id: str
    reply_path: str
    reply_to_id: str


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def is_allowed_reply_path(chat_id: str, reply_path: str) -> bool:
    if not chat_id or not reply_path:
        return False
    if any(item in reply_path for item in ("..", "?", "#", "//", "\\")):
        return False
    return reply_path == f"/api/chats/{chat_id}/agent-messages"


def extract_chat_envelope(payload: dict[str, Any]) -> InterfazeChatEnvelope | None:
    params = payload.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    metadata = message.get("metadata") if isinstance(message, dict) else None
    agentplanet = metadata.get("agentplanet") if isinstance(metadata, dict) else None
    if not isinstance(agentplanet, dict):
        return None

    chat_id = _nonempty(agentplanet.get("chat_id"))
    reply_path = _nonempty(agentplanet.get("reply_path"))
    reply_channel = _nonempty(agentplanet.get("reply_channel"))
    reply_to_id = _nonempty(agentplanet.get("message_id")) or _nonempty(
        agentplanet.get("messageId")
    )
    if (
        chat_id is None
        or reply_path is None
        or reply_to_id is None
        or reply_channel != CHAT_REPLY_CHANNEL
        or not is_allowed_reply_path(chat_id, reply_path)
    ):
        return None
    return InterfazeChatEnvelope(chat_id, reply_path, reply_to_id)


class InterfazeChatWriteback:
    """ACN-compatible Interfaze reply writer using short-lived agent JWTs."""

    def __init__(
        self,
        *,
        acn_base_url: str,
        chat_api_base: str,
        agent_id: str,
        api_key: str,
        http=None,
    ):
        self.acn_base_url = acn_base_url.rstrip("/")
        self.chat_api_base = chat_api_base.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.http = http or UrllibJsonHttpClient(timeout=15.0)

    def _mint_agent_jwt(self) -> str:
        acn_url = urlsplit(self.acn_base_url)
        if acn_url.scheme not in {"http", "https"} or not acn_url.netloc:
            raise ValueError("ACN base URL rejected")
        # ACN's agent APIs live under /api/v1, but OAuth is rooted at the
        # service origin. Keep this aligned with the ACN CLI contract.
        oauth_url = urlunsplit((acn_url.scheme, acn_url.netloc, "/oauth/token", "", ""))
        data = self.http.request_json(
            "POST",
            oauth_url,
            payload={
                "grant_type": "client_credentials",
                "client_id": self.agent_id,
                "client_secret": self.api_key,
                "audience": DEFAULT_CHAT_API_BASE,
            },
        )
        token = _nonempty(data.get("access_token"))
        if token is None:
            raise RuntimeError("ACN OAuth response did not contain an access token")
        return token

    def _reply_url(self, envelope: InterfazeChatEnvelope) -> str:
        if not is_allowed_reply_path(envelope.chat_id, envelope.reply_path):
            raise ValueError("Interfaze reply path rejected")
        url = urljoin(self.chat_api_base + "/", envelope.reply_path.lstrip("/"))
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("Interfaze reply URL scheme rejected")
        if urlsplit(url).netloc != urlsplit(self.chat_api_base).netloc:
            raise ValueError("Interfaze reply URL origin mismatch")
        return url

    def write(self, envelope: InterfazeChatEnvelope, content: str) -> None:
        url = self._reply_url(envelope)
        token = self._mint_agent_jwt()
        try:
            self.http.request_json(
                "POST",
                url,
                headers={"Authorization": "Bearer " + token},
                payload={"content": content, "reply_to_id": envelope.reply_to_id},
            )
        except JsonHttpError as exc:
            if exc.status != 401:
                raise
            # Match ACN CLI behavior: a rejected short-lived token is reminted once.
            token = self._mint_agent_jwt()
            self.http.request_json(
                "POST",
                url,
                headers={"Authorization": "Bearer " + token},
                payload={"content": content, "reply_to_id": envelope.reply_to_id},
            )
