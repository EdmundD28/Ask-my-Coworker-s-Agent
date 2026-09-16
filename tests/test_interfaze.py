from __future__ import annotations

import unittest

from tests import _bootstrap  # noqa: F401

from amca.interfaze import (
    InterfazeChatEnvelope,
    InterfazeChatWriteback,
    extract_chat_envelope,
    is_allowed_reply_path,
)
from amca.transport import JsonHttpError


class FakeHttp:
    def __init__(self, *, reject_first_write=False):
        self.calls = []
        self.reject_first_write = reject_first_write

    def request_json(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, headers, payload))
        if url.endswith("/oauth/token"):
            return {"access_token": f"jwt-{len(self.calls)}", "expires_in": 300}
        if self.reject_first_write:
            self.reject_first_write = False
            raise JsonHttpError(401, "rejected")
        return {"status": "ok"}


def payload(**changes):
    agentplanet = {
        "chat_id": "chat-1",
        "message_id": "message-1",
        "reply_channel": "agentplanet.chat",
        "reply_path": "/api/chats/chat-1/agent-messages",
    }
    agentplanet.update(changes)
    return {
        "params": {"message": {"metadata": {"agentplanet": agentplanet}}}
    }


class InterfazeWritebackTests(unittest.TestCase):
    def test_extracts_only_allowlisted_chat_envelope(self):
        envelope = extract_chat_envelope(payload())
        self.assertEqual(
            InterfazeChatEnvelope("chat-1", "/api/chats/chat-1/agent-messages", "message-1"),
            envelope,
        )
        self.assertIsNone(extract_chat_envelope(payload(reply_path="/api/internal/admin")))
        self.assertIsNone(extract_chat_envelope(payload(reply_channel="forged")))

    def test_reply_path_rejects_cross_chat_and_url_tricks(self):
        self.assertTrue(is_allowed_reply_path("chat-1", "/api/chats/chat-1/agent-messages"))
        self.assertFalse(is_allowed_reply_path("chat-1", "/api/chats/chat-2/agent-messages"))
        self.assertFalse(is_allowed_reply_path("chat-1", "/api/chats/chat-1/../admin"))
        self.assertFalse(is_allowed_reply_path("chat-1", "//evil.example/agent-messages"))

    def test_mints_short_lived_jwt_and_posts_visible_reply(self):
        http = FakeHttp()
        writer = InterfazeChatWriteback(
            acn_base_url="https://api.acnlabs.dev/api/v1",
            chat_api_base="https://api.agentplanet.org",
            agent_id="agent-1",
            api_key="acn-secret",
            http=http,
        )
        writer.write(
            InterfazeChatEnvelope("chat-1", "/api/chats/chat-1/agent-messages", "message-1"),
            "AMCA online",
        )
        self.assertEqual("https://api.acnlabs.dev/oauth/token", http.calls[0][1])
        self.assertEqual("https://api.agentplanet.org/api/chats/chat-1/agent-messages", http.calls[1][1])
        self.assertEqual({"content": "AMCA online", "reply_to_id": "message-1"}, http.calls[1][3])
        self.assertEqual("Bearer jwt-1", http.calls[1][2]["Authorization"])

    def test_remints_once_when_gateway_rejects_jwt(self):
        http = FakeHttp(reject_first_write=True)
        writer = InterfazeChatWriteback(
            acn_base_url="https://api.acnlabs.dev/api/v1",
            chat_api_base="https://api.agentplanet.org",
            agent_id="agent-1",
            api_key="acn-secret",
            http=http,
        )
        writer.write(
            InterfazeChatEnvelope("chat-1", "/api/chats/chat-1/agent-messages", "message-1"),
            "AMCA online",
        )
        self.assertEqual(4, len(http.calls))
        self.assertEqual("Bearer jwt-3", http.calls[-1][2]["Authorization"])


if __name__ == "__main__":
    unittest.main()
