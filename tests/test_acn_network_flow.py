from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tests import _bootstrap  # noqa: F401

from amca.protocol import QuestionMessageV1, TriggerOrigin
from amca.transport import AcnNetworkAdapter, MessageEnvelope


NOW = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request_json(self, method, url, *, headers=None, payload=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "payload": payload,
            }
        )
        return self.responses.pop(0)


class AcnNetworkFlowTests(unittest.TestCase):
    def test_private_network_invitation_discovery_and_leave_match_current_acn_paths(self):
        http = FakeHttp(
            [
                {"slug": "amca-space"},
                {"invitation_id": "invite-1", "status": "pending"},
                {"status": "approved"},
                {"agents": [{"agent_id": "acn-a"}, {"agent_id": "acn-b"}]},
                {"status": "left"},
            ]
        )
        owner = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-a",
            api_key="key-a",
        )
        invitee = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-b",
            api_key="key-b",
        )

        owner.create_network(name="AMCA Space", description="Private AMCA collaboration")
        owner.invite("amca-space", "acn-b", note="owner-triggered pilot")
        invitee.accept_invitation("amca-space", "invite-1")
        agents = invitee.discover("amca-space")
        invitee.leave("amca-space")

        calls = http.calls
        self.assertEqual(("POST", "https://acn.example/api/v1/subnets"), (calls[0]["method"], calls[0]["url"]))
        self.assertEqual("approval", calls[0]["payload"]["join_policy"])
        self.assertTrue(calls[0]["payload"]["is_private"])
        self.assertEqual("Bearer key-a", calls[0]["headers"]["Authorization"])
        self.assertEqual("https://acn.example/api/v1/subnets/amca-space/invitations", calls[1]["url"])
        self.assertEqual({"agent_id": "acn-b", "note": "owner-triggered pilot"}, calls[1]["payload"])
        self.assertEqual("https://acn.example/api/v1/subnets/amca-space/invitations/invite-1/accept", calls[2]["url"])
        self.assertEqual("Bearer key-b", calls[2]["headers"]["Authorization"])
        self.assertEqual("GET", calls[3]["method"])
        self.assertEqual("https://acn.example/api/v1/subnets/amca-space/agents", calls[3]["url"])
        self.assertEqual("DELETE", calls[4]["method"])
        self.assertEqual("https://acn.example/api/v1/agents/acn-b/subnets/amca-space", calls[4]["url"])
        self.assertEqual(["acn-a", "acn-b"], [item["agent_id"] for item in agents])

    def test_question_uses_proper_a2a_data_envelope_and_inquiry_category(self):
        http = FakeHttp([{"message_id": "acn-msg-1", "status": "accepted"}])
        adapter = AcnNetworkAdapter(
            "https://acn.example/api/v1",
            http=http,
            agent_id="acn-a",
            api_key="key-a",
        )
        question = QuestionMessageV1(
            schema="amca.question.v1",
            request_id="req-1",
            task_id="task-1",
            conversation_id="conv-1",
            chain_id="chain-1",
            parent_request_id=None,
            from_agent="acn-a",
            target_agent="acn-b",
            project_scope="project-blue",
            question="When is the deadline?",
            topic="project.deadline",
            trigger_origin=TriggerOrigin.OWNER_EXPLICIT,
            created_at=NOW,
            nonce="nonce-1",
        )
        envelope = MessageEnvelope(
            message_id="req-1",
            from_agent="acn-a",
            target_agent="acn-b",
            message_type="amca.question.v1",
            body=question.to_payload(),
        )

        adapter.send(envelope)

        payload = http.calls[0]["payload"]
        self.assertEqual("inquiry", payload["message_type"])
        self.assertNotIn("body", payload)
        self.assertNotIn("message_id", payload)
        a2a = payload["message"]
        self.assertEqual("user", a2a["role"])
        self.assertEqual("data", a2a["parts"][0]["kind"])
        self.assertEqual("amca.question.v1", a2a["parts"][0]["data"]["schema"])
        self.assertEqual("owner_explicit", a2a["parts"][0]["data"]["trigger_origin"])


if __name__ == "__main__":
    unittest.main()
