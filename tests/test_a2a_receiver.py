from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from tests import _bootstrap  # noqa: F401

from amca.directory import (
    ConfirmationMode,
    FollowupScope,
    InboundQuestionPolicy,
    MemberCard,
    MemberProfile,
)
from amca.domain import EvidenceRecord
from amca.infrastructure.sqlite import SqliteEvidenceStore
from amca.receiver import build_receiver_server, create_a2a_receiver
from amca.runtime import DeterministicRuntime


NOW = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def profile() -> MemberProfile:
    return MemberProfile(
        card=MemberCard(
            agent_id="agent-b",
            role_name="Project scheduler",
            description="Answers scheduling questions",
            owner_claim="Bob",
        ),
        inbound_policy=InboundQuestionPolicy(
            confirmation_mode=ConfirmationMode.PREAUTHORIZED,
            allowed_asker_ids=frozenset({"agent-a"}),
            allowed_topics=frozenset({"project.deadline"}),
            allowed_project_scopes=frozenset({"project-blue"}),
            followup_limit=None,
            followup_scope=FollowupScope.CHAIN,
        ),
    )


def question_payload(**changes) -> dict:
    data = {
        "schema": "amca.question.v1",
        "request_id": "req-1",
        "task_id": "task-1",
        "conversation_id": "conv-1",
        "chain_id": "chain-1",
        "parent_request_id": None,
        "from_agent": "agent-a",
        "target_agent": "agent-b",
        "project_scope": "project-blue",
        "question": "When is the deadline?",
        "topic": "project.deadline",
        "trigger_origin": "owner_explicit",
        "created_at": NOW.isoformat(),
        "nonce": "nonce-1",
    }
    data.update(changes)
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": "transport-msg-1",
                "role": "user",
                "parts": [{"kind": "data", "data": data}],
            }
        },
    }


def interfaze_payload(*, reply_channel="agentplanet.chat") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-interfaze-1",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": "msg-interfaze-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "ok"}],
                "metadata": {
                    "agentplanet": {
                        "chat_id": "chat-1",
                        "message_id": "user-msg-1",
                        "reply_channel": reply_channel,
                        "reply_path": "/api/chats/chat-1/agent-messages",
                    }
                },
            }
        },
    }


class A2AReceiverTests(unittest.TestCase):
    def make_app(self, root: Path):
        evidence = SqliteEvidenceStore(root / "bob-private.sqlite3", owner_agent_id="agent-b")
        evidence.add(
            EvidenceRecord(
                evidence_id="ev-b",
                owner_agent_id="agent-b",
                topic="project.deadline",
                claim_key="deadline",
                content="Friday",
                source_label="Bob private notes",
                version="v1",
                updated_at=NOW,
                allowed_recipient_ids=frozenset({"agent-a"}),
            )
        )
        evidence.close()
        return create_a2a_receiver(
            profile=profile(),
            owner_id="owner-b",
            private_database=root / "bob-private.sqlite3",
            coordinator_database=root / "coordinator.sqlite3",
            runtime=DeterministicRuntime("Friday"),
            clock=lambda: NOW,
        )

    def test_interfaze_text_gets_safe_visible_receipt_without_private_answer(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(Path(root))
            response = app.handle(
                "POST",
                "/a2a",
                {"content-type": "application/json"},
                json.dumps(interfaze_payload()).encode("utf-8"),
            )
            app.close()

        payload = json.loads(response.body)
        self.assertEqual(200, response.status)
        self.assertEqual("text", payload["result"]["parts"][0]["kind"])
        self.assertIn("接收端在线", payload["result"]["parts"][0]["text"])
        self.assertIn("不会读取私人资料或调用模型", payload["result"]["parts"][0]["text"])

    def test_interfaze_text_is_written_back_when_writer_is_configured(self):
        class Writer:
            def __init__(self):
                self.calls = []

            def write(self, envelope, content):
                self.calls.append((envelope, content))

        with tempfile.TemporaryDirectory() as root:
            writer = Writer()
            app = self.make_app(Path(root))
            app.chat_writeback = writer
            response = app.handle(
                "POST",
                "/a2a",
                {"content-type": "application/json"},
                json.dumps(interfaze_payload()).encode("utf-8"),
            )
            app.close()

        self.assertEqual(200, response.status)
        self.assertEqual(1, len(writer.calls))
        self.assertEqual("chat-1", writer.calls[0][0].chat_id)
        self.assertIn("接收端在线", writer.calls[0][1])

    def test_forged_non_interfaze_text_does_not_become_an_amca_question(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app(Path(root))
            response = app.handle(
                "POST",
                "/a2a",
                {"content-type": "application/json"},
                json.dumps(interfaze_payload(reply_channel="other.channel")).encode("utf-8"),
            )
            app.close()

        payload = json.loads(response.body)
        self.assertEqual(-32602, payload["error"]["code"])

    def test_valid_jsonrpc_question_runs_admission_and_returns_a2a_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(Path(directory))

            response = app.handle(
                "POST",
                "/a2a",
                {"content-type": "application/json"},
                json.dumps(question_payload()).encode("utf-8"),
            )
            payload = json.loads(response.body)
            answer = payload["result"]["parts"][0]["data"]

            self.assertEqual(200, response.status)
            self.assertEqual("rpc-1", payload["id"])
            self.assertEqual("message", payload["result"]["kind"])
            self.assertEqual("agent", payload["result"]["role"])
            self.assertEqual("amca.answer.v1", answer["schema"])
            self.assertEqual("answer", answer["status"])
            self.assertEqual("Friday", answer["answer"])
            self.assertEqual("unverified_self_claim", answer["transport_identity"])
            app.close()

    def test_replayed_jsonrpc_question_is_rejected_without_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(Path(directory))
            raw = json.dumps(question_payload()).encode("utf-8")

            first = app.handle("POST", "/a2a", {}, raw)
            replay = app.handle("POST", "/a2a", {}, raw)
            replay_data = json.loads(replay.body)["result"]["parts"][0]["data"]

            self.assertEqual(200, first.status)
            self.assertEqual("reject", replay_data["status"])
            self.assertEqual("request_replay", replay_data["reason_code"])
            self.assertNotIn("Friday", replay.body)
            app.close()

    def test_automatic_trigger_and_wrong_method_do_not_reach_private_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(Path(directory))
            automatic = question_payload(trigger_origin="agent_automatic")
            wrong_method = question_payload()
            wrong_method["method"] = "tasks/get"

            denied = app.handle("POST", "/a2a", {}, json.dumps(automatic).encode())
            method_error = app.handle("POST", "/a2a", {}, json.dumps(wrong_method).encode())

            denied_data = json.loads(denied.body)["result"]["parts"][0]["data"]
            self.assertEqual("reject", denied_data["status"])
            self.assertEqual("automatic_trigger_not_allowed_v1", denied_data["reason_code"])
            self.assertNotIn("Friday", denied.body)
            self.assertEqual(-32601, json.loads(method_error.body)["error"]["code"])
            app.close()

    def test_health_and_size_limit_are_enforced_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(Path(directory))

            health = app.handle("GET", "/health", {}, b"")
            oversized = app.handle("POST", "/a2a", {}, b"x" * (64 * 1024 + 1))

            health_data = json.loads(health.body)
            self.assertEqual("ok", health_data["status"])
            self.assertEqual("agent-b", health_data["agent_id"])
            self.assertEqual(413, oversized.status)
            app.close()

    def test_real_local_port_accepts_jsonrpc_post(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(Path(directory))
            server = build_receiver_server(app, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = Request(
                    base + "/a2a",
                    data=json.dumps(question_payload()).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                app.close()

            self.assertEqual("amca.answer.v1", payload["result"]["parts"][0]["data"]["schema"])


if __name__ == "__main__":
    unittest.main()
