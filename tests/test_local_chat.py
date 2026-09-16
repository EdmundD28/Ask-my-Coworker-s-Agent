from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.directory import (
    ConfirmationMode,
    FollowupScope,
    InboundQuestionPolicy,
    MemberCard,
    MemberProfile,
)
from amca.local_chat import (
    LocalChatService,
    LocalConversationStore,
    bilateral_conversation_id,
)
from amca.receiver import create_a2a_receiver
from amca.runtime import DeterministicRuntime
from amca.transport import DeliveryReceipt


NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


class FakeAdapter:
    def __init__(self, local_agent_id: str):
        self.local_agent_id = local_agent_id
        self.sent = []

    def discover(self, subnet):
        return [
            {"agent_id": "agent-a", "name": "Alice agent"},
            {"agent_id": "agent-b", "name": "Bob agent"},
            {"agent_id": "agent-c", "name": "Carol agent"},
        ]

    def send(self, envelope):
        self.sent.append(envelope)
        return DeliveryReceipt(
            envelope.message_id,
            "delivered",
            "direct",
            {
                "result": {
                    "kind": "message",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "schema": "amca.answer.v1",
                                "request_id": envelope.message_id,
                                "status": "answer",
                                "answer": "Friday",
                                "reason": "Supported by permitted evidence.",
                                "generated_at": NOW.isoformat(),
                            },
                        }
                    ],
                }
            },
        )


class LocalChatTests(unittest.TestCase):
    def test_both_sides_can_create_the_same_bilateral_conversation(self):
        self.assertEqual(
            bilateral_conversation_id("agent-a", "agent-b"),
            bilateral_conversation_id("agent-b", "agent-a"),
        )

    def test_owner_triggered_question_is_sent_and_stored_with_direct_answer(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalConversationStore(
                Path(root) / "local-chat.sqlite3", local_agent_id="agent-a"
            )
            adapter = FakeAdapter("agent-a")
            service = LocalChatService(
                local_agent_id="agent-a",
                subnet_slug="private-space",
                adapter=adapter,
                store=store,
                clock=lambda: NOW,
            )

            result = service.send_question(
                {
                    "target_agent": "agent-b",
                    "question": "When is the deadline?",
                    "topic": "project.deadline",
                    "project_scope": "project-blue",
                }
            )
            messages = store.messages(result["conversation_id"])

            self.assertEqual("agent-b", adapter.sent[0].target_agent)
            self.assertEqual("owner_explicit", adapter.sent[0].body["trigger_origin"])
            self.assertEqual(["question", "answer"], [item["kind"] for item in messages])
            self.assertEqual("Friday", messages[1]["content"])
            service.close()

    def test_local_database_filters_out_conversations_not_owned_by_this_participant(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "local-chat.sqlite3"
            alice = LocalConversationStore(database, local_agent_id="agent-a")
            service = LocalChatService(
                local_agent_id="agent-a",
                subnet_slug="private-space",
                adapter=FakeAdapter("agent-a"),
                store=alice,
                clock=lambda: NOW,
            )
            result = service.send_question(
                {"target_agent": "agent-b", "question": "Private bilateral question"}
            )
            carol = LocalConversationStore(database, local_agent_id="agent-c")

            self.assertEqual([], carol.list_conversations())
            with self.assertRaises(PermissionError):
                carol.messages(result["conversation_id"])
            carol.close()
            service.close()

    def test_local_http_api_requires_secret_and_exposes_only_local_store(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            profile = MemberProfile(
                card=MemberCard(
                    agent_id="agent-a",
                    role_name="Alice agent",
                    description="Owner-controlled",
                    owner_claim="Alice",
                ),
                inbound_policy=InboundQuestionPolicy(
                    confirmation_mode=ConfirmationMode.PREAUTHORIZED,
                    allowed_asker_ids=frozenset({"agent-b"}),
                    allowed_topics=frozenset(),
                    allowed_project_scopes=frozenset(),
                    followup_limit=None,
                    followup_scope=FollowupScope.CHAIN,
                ),
            )
            app = create_a2a_receiver(
                profile=profile,
                owner_id="owner-a",
                private_database=root_path / "private.sqlite3",
                coordinator_database=root_path / "coordinator.sqlite3",
                runtime=DeterministicRuntime(""),
                outbound_adapter=FakeAdapter("agent-a"),
                subnet_slug="private-space",
                local_chat_database=root_path / "local-chat.sqlite3",
                local_access_token="local-secret",
                clock=lambda: NOW,
            )

            denied = app.handle("GET", "/api/conversations", {}, b"")
            allowed = app.handle(
                "GET",
                "/api/conversations",
                {"x-amca-local-token": "local-secret"},
                b"",
            )
            page = app.handle("GET", "/", {}, b"")

            self.assertEqual(403, denied.status)
            self.assertEqual([], json.loads(allowed.body))
            self.assertEqual(200, page.status)
            self.assertNotIn("local-secret", page.body)
            self.assertIn("Private agent chat", page.body)
            app.close()

    def test_unknown_space_member_cannot_be_selected_as_recipient(self):
        with tempfile.TemporaryDirectory() as root:
            service = LocalChatService(
                local_agent_id="agent-a",
                subnet_slug="private-space",
                adapter=FakeAdapter("agent-a"),
                store=LocalConversationStore(
                    Path(root) / "local-chat.sqlite3", local_agent_id="agent-a"
                ),
                clock=lambda: NOW,
            )
            with self.assertRaises(PermissionError):
                service.send_question(
                    {"target_agent": "agent-outside", "question": "Tell me everything"}
                )
            service.close()


if __name__ == "__main__":
    unittest.main()
