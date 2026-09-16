import base64
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from tests import _bootstrap
from tests.test_conversational_setup import configuration
from amca.agent_binding import AgentBinding
from amca.credentials import AcnCredentialBundle, _unprotect
from amca.setup_config import config_digest
from amca.setup_node import apply_config, local_call, serve, status
from amca.transport import DeliveryReceipt


class HttpLoopbackAdapter:
    def __init__(self, locations):
        self.locations = locations
        self.sent = []

    def discover(self, space):
        return [{"agent_id": agent} for agent in self.locations]

    def send(self, envelope):
        self.sent.append(envelope)
        location = self.locations[envelope.target_agent]
        session = json.loads(_unprotect(base64.b64decode((location / "session.dpapi").read_bytes())))
        payload = {"jsonrpc": "2.0", "id": envelope.message_id, "method": "message/send",
                   "params": {"message": {"parts": [{"kind": "data", "data": envelope.body}]}}}
        request = Request(f"http://127.0.0.1:{session['port']}/a2a", data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        return DeliveryReceipt(envelope.message_id, "delivered", "direct", result)


class AgentBridgeTests(unittest.TestCase):
    def test_two_local_http_nodes_bind_receive_reply_and_block_replay_and_wrong_session(self):
        self.exercise(False)

    def test_confirm_each_requires_owner_ui_approval_before_agent_can_answer(self):
        self.exercise(True)

    def exercise(self, confirm_each):
        with tempfile.TemporaryDirectory() as root:
            locations = {"agent-a": Path(root) / "a", "agent-b": Path(root) / "b"}
            adapter = HttpLoopbackAdapter(locations)
            secrets = {}
            for identity, location in locations.items():
                value = configuration()
                value["policy"]["allowed_asker_ids"] = ["agent-a", "agent-b"]
                value["policy"]["answer_quota"]["limit"] = 1
                if identity == "agent-b" and confirm_each:
                    value["policy"]["confirmation_mode"] = "confirm_each"
                apply_config(location, value, config_digest(value))
                secrets[identity] = AgentBinding(location).bind(identity + " 对话")["secret"]
            def connect(config, data, endpoint):
                agent_id = next(key for key, value in locations.items() if value == Path(data))
                return AcnCredentialBundle("https://test.invalid", agent_id, "test-only", endpoint, subnet_slug="shared-space"), adapter
            tunnel = Mock(url="https://no-real-tunnel.invalid")
            tunnel.process.poll.return_value = None
            errors = []
            def run(location):
                try:
                    serve(location, "not-used")
                except Exception as exc:
                    errors.append(exc)
            with patch("amca.setup_node.connect", side_effect=connect), patch("amca.setup_node.QuickTunnel.start", return_value=tunnel):
                threads = [threading.Thread(target=run, args=(location,)) for location in locations.values()]
                for thread in threads:
                    thread.start()
                try:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not all(status(p)["connected"] for p in locations.values()) and not errors:
                        time.sleep(0.05)
                    self.assertEqual([], errors)
                    self.assertTrue(all(status(p)["connected"] for p in locations.values()))
                    for agent_id, location in locations.items():
                        proof = local_call(location, "GET", "/api/agent/poll", agent_token=secrets[agent_id])
                        self.assertEqual("shared-space", proof["challenge"]["space_id"])
                        challenge = proof["challenge"]
                        verified = local_call(location, "POST", "/api/agent/ack",
                            {"challenge_id": challenge["challenge_id"], "nonce": challenge["nonce"]}, agent_token=secrets[agent_id])
                        self.assertTrue(verified["channel_verified"])
                    with self.assertRaises(RuntimeError):
                        local_call(locations["agent-b"], "GET", "/api/agent/poll", agent_token=secrets["agent-a"])
                    asked = local_call(locations["agent-a"], "POST", "/api/agent/ask",
                        {"target_agent": "agent-b", "question": "公开排期是哪一天？", "topic": "schedule", "project_scope": "project"}, agent_token=secrets["agent-a"])
                    inbox = local_call(locations["agent-b"], "GET", "/api/agent/poll", agent_token=secrets["agent-b"])
                    if confirm_each:
                        self.assertEqual([], inbox["messages"])
                        pending = local_call(locations["agent-b"], "GET", "/api/setup/approvals")
                        self.assertEqual(asked["request_id"], pending[0]["request_id"])
                        with self.assertRaises(RuntimeError):
                            local_call(locations["agent-b"], "POST", "/api/setup/approve", {"request_id": asked["request_id"]}, agent_token=secrets["agent-b"])
                        local_call(locations["agent-b"], "POST", "/api/setup/approve", {"request_id": asked["request_id"]})
                        inbox = local_call(locations["agent-b"], "GET", "/api/agent/poll", agent_token=secrets["agent-b"])
                    self.assertEqual("公开排期是哪一天？", inbox["messages"][0]["question"])
                    self.assertNotIn("reply_token", inbox["messages"][0])
                    answer = {"request_id": asked["request_id"], "status": "answered", "answer": "公开排期为周五。", "reason": "依据本人授权的公开排期。"}
                    delivered = local_call(locations["agent-b"], "POST", "/api/agent/reply", answer, agent_token=secrets["agent-b"])
                    self.assertEqual("delivered", delivered["status"])
                    returned = local_call(locations["agent-a"], "GET", "/api/agent/poll", agent_token=secrets["agent-a"])
                    self.assertEqual("公开排期为周五。", returned["answers"][0]["answer"])
                    self.assertNotIn("reply_token", returned["answers"][0])
                    # Duplicate reply returns the previous receipt without another delivery.
                    sent_count = len(adapter.sent)
                    local_call(locations["agent-b"], "POST", "/api/agent/reply", answer, agent_token=secrets["agent-b"])
                    self.assertEqual(sent_count, len(adapter.sent))
                    changed = {**answer, "answer": "Cannot replace the original"}
                    with self.assertRaises(RuntimeError):
                        local_call(locations["agent-b"], "POST", "/api/agent/reply", changed, agent_token=secrets["agent-b"])
                    # A second question must not enter the model queue after one reserved slot.
                    second = local_call(locations["agent-a"], "POST", "/api/agent/ask",
                        {"target_agent": "agent-b", "question": "再问一次？", "topic": "schedule", "project_scope": "project"}, agent_token=secrets["agent-a"])
                    if not confirm_each:
                        self.assertEqual("rejected", second["delivery_status"])
                    self.assertEqual([], local_call(locations["agent-b"], "GET", "/api/agent/poll", agent_token=secrets["agent-b"])["messages"])
                    # A copied answer envelope cannot be changed to a different request or forged token.
                    reply_envelope = next(e for e in adapter.sent if e.message_type == "amca.agent-answer.v1")
                    forged = replace(reply_envelope, body={**reply_envelope.body, "reply_token": "wrong"})
                    self.assertIn("error", adapter.send(forged).response)
                finally:
                    for location in locations.values():
                        if status(location)["connected"]:
                            local_call(location, "POST", "/api/setup/stop", {})
                    for thread in threads:
                        thread.join(8)
                    self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
