from __future__ import annotations

import unittest

from tests import _bootstrap  # noqa: F401

from amca.transport import (
    AcnNetworkAdapter,
    AgentRegistration,
    MessageEnvelope,
    UrllibJsonHttpClient,
)


class FakeHttp:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[dict] = []

    def request_json(self, method, url, *, headers=None, payload=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "payload": payload or {},
            }
        )
        return self.responses.pop(0)


class AcnTransportContractTests(unittest.TestCase):
    def test_real_http_client_has_an_explicit_product_user_agent(self):
        client = UrllibJsonHttpClient()

        self.assertEqual("AMCA/0.1", client.user_agent)

    def test_registration_uses_current_acn_join_contract(self):
        http = FakeHttp()
        http.responses.append({"agent_id": "acn-b", "api_key": "acn-secret"})
        adapter = AcnNetworkAdapter("https://acn.example/api/v1", http=http)

        identity = adapter.register_agent(
            AgentRegistration(
                name="Responder B",
                description="AMCA responder owned by Bob",
                tags=("amca", "responder"),
                a2a_endpoint="https://bob.example/a2a",
            )
        )

        call = http.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual("https://acn.example/api/v1/agents/join", call["url"])
        self.assertEqual("https://bob.example/a2a", call["payload"]["a2a_endpoint"])
        self.assertEqual({"mode": "open"}, call["payload"]["communication_policy"])
        self.assertEqual("direct", call["payload"]["delivery"])
        self.assertTrue(call["payload"]["self_hosted"])
        self.assertNotIn("subnet_ids", call["payload"])
        self.assertEqual("acn-b", identity.agent_id)

    def test_send_uses_agent_key_and_does_not_confuse_delivery_with_disclosure(self):
        http = FakeHttp()
        http.responses.append({"message_id": "msg-1", "status": "accepted"})
        adapter = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-a",
            api_key="acn-key-a",
        )
        envelope = MessageEnvelope(
            message_id="question-1",
            from_agent="acn-a",
            target_agent="acn-b",
            message_type="amca.question.v1",
            body={"request_id": "req-1", "question": "What is the limit?"},
        )

        receipt = adapter.send(envelope)

        call = http.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual("https://acn.example/api/v1/communication/send", call["url"])
        self.assertEqual("Bearer acn-key-a", call["headers"]["Authorization"])
        self.assertEqual("acn-a", call["payload"]["from_agent"])
        self.assertEqual("acn-b", call["payload"]["target_agent"])
        self.assertNotIn("allowed_recipient_ids", call["payload"])
        self.assertEqual("accepted", receipt.status)

    def test_send_preserves_direct_a2a_response_for_the_local_chat(self):
        http = FakeHttp()
        direct_response = {
            "result": {
                "kind": "message",
                "parts": [{"kind": "data", "data": {"schema": "amca.answer.v1"}}],
            }
        }
        http.responses.append(direct_response)
        adapter = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-a",
            api_key="acn-key-a",
        )

        receipt = adapter.send(
            MessageEnvelope("question-1", "acn-a", "acn-b", "amca.question.v1", {})
        )

        self.assertEqual("delivered", receipt.status)
        self.assertEqual(direct_response, receipt.response)

    def test_endpoint_refresh_uses_owner_key_and_existing_agent_identity(self):
        http = FakeHttp()
        http.responses.append(
            {
                "agent_id": "acn-owner",
                "endpoint": "https://owner.example/a2a",
                "endpoint_reachable": True,
                "a2a_handshake_ok": True,
            }
        )
        adapter = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-owner",
            api_key="owner-key",
        )

        result = adapter.update_endpoint("https://owner.example/a2a")

        call = http.calls[0]
        self.assertEqual("PATCH", call["method"])
        self.assertEqual(
            "https://acn.example/api/v1/agents/acn-owner/endpoint", call["url"]
        )
        self.assertEqual("Bearer owner-key", call["headers"]["Authorization"])
        self.assertTrue(result["a2a_handshake_ok"])

    def test_policy_switch_and_public_profile_use_current_acn_paths(self):
        http = FakeHttp()
        http.responses.extend(
            [
                {"communication_policy": {"mode": "open"}},
                {"agent_id": "acn-owner", "mode": "open", "unread_manifest_count": 0},
            ]
        )
        adapter = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-owner",
            api_key="owner-key",
        )

        adapter.update_communication_policy("open")
        profile = adapter.communication_profile()

        self.assertEqual(
            "https://acn.example/api/v1/agents/acn-owner/policy",
            http.calls[0]["url"],
        )
        self.assertEqual(
            {"communication_policy": {"mode": "open"}},
            http.calls[0]["payload"],
        )
        self.assertEqual("Bearer owner-key", http.calls[0]["headers"]["Authorization"])
        self.assertEqual("GET", http.calls[1]["method"])
        self.assertEqual("open", profile["mode"])


if __name__ == "__main__":
    unittest.main()
