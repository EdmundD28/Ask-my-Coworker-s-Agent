import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from tests import _bootstrap
from tests.test_conversational_setup import configuration
from amca.agent_binding import AgentBinding
from amca.entry_hub import EntryHub, accept_file, load_session, parse_entry, render_entry, ENTRY_SCHEMA
from amca.hub_ui import HubApp, PAGE
from amca.setup_config import config_digest
from amca.setup_node import apply_config
from amca.onboarding import join_from_invite, _encode, INVITE_SCHEMA
from amca.transport import AgentIdentityReceipt
from amca import setup_cli


def call(app, payload=None, token="local-test", path="/api/hub/action", origin=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b""
    response = []
    env = {"REQUEST_METHOD": "POST" if payload else "GET", "PATH_INFO": path,
           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "HTTP_X_HUB_TOKEN": token,
           "HTTP_HOST": "127.0.0.1:9999"}
    if origin:
        env["HTTP_ORIGIN"] = origin
    content = b"".join(app(env, lambda status, headers: response.append((status, dict(headers)))))
    return response[0], content


class EntryHubTests(unittest.TestCase):
    def test_source_mode_finds_latest_local_build_instead_of_fixed_version(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            module = project / "src" / "amca" / "setup_cli.py"
            older = project / "dist" / "Ask My Coworker's Agent v4" / "Ask My Coworker's Agent.exe"
            latest = project / "dist" / "Ask My Coworker's Agent v27" / "Ask My Coworker's Agent.exe"
            for path in (module, older, latest):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            older.touch()
            time.sleep(0.01)
            latest.touch()
            with patch.object(setup_cli, "__file__", str(module)), patch.object(setup_cli.sys, "frozen", False, create=True):
                self.assertEqual(latest.parent.resolve(), setup_cli.package_directory())

    def test_packaged_program_uses_its_own_machine_location(self):
        local_executable = Path("C:/Users/tester/Downloads/Ask My Coworker's Agent/Ask My Coworker's Agent.exe")
        with patch.object(setup_cli.sys, "frozen", True, create=True), patch.object(setup_cli.sys, "executable", str(local_executable)):
            self.assertEqual(local_executable.resolve().parent, setup_cli.package_directory())

    def test_open_chat_requires_hub_authorization_and_keeps_token_out_of_response(self):
        import base64
        from amca.credentials import _protect
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, "local-test")
            entry = app.hub.create_code("测试")['entry_id']
            data = app.hub.data(entry)
            data.mkdir(parents=True)
            (data / 'session.dpapi').write_bytes(base64.b64encode(_protect(json.dumps({'port': 54321, 'token': 'private-test-token'}).encode())))
            with patch('amca.hub_ui.local_call', return_value={'connected': True}), patch('amca.hub_ui.webbrowser.open', return_value=True) as opened:
                self.assertEqual('403 Forbidden', call(app, {'action': 'open_chat', 'entry_id': entry}, token='wrong')[0][0])
                opened.assert_not_called()
                response, body = call(app, {'action': 'open_chat', 'entry_id': entry})
                self.assertEqual('200 OK', response[0])
                self.assertNotIn(b'private-test-token', body)
                opened.assert_called_once_with('http://127.0.0.1:54321/#token=private-test-token', new=2)

    def test_invitation_cannot_accept_a_different_remote_space(self):
        adapter, http = Mock(), Mock()
        http.request_json.return_value = {"subnet_slug": "wrong-space", "invitation_id": "invitation"}
        code = _encode({"schema": INVITE_SCHEMA, "join_url": "https://owner.example/onboarding/join", "token": "test"})
        with self.assertRaises(PermissionError):
            join_from_invite(code, adapter=adapter, registration=None, http=http,
                existing_identity=AgentIdentityReceipt("agent", "secret"), expected_subnet="expected-space")
        adapter.accept_invitation.assert_not_called()

    def test_ui_generates_unique_scoped_configuration_codes_in_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, "local-test")
            (code, headers), body = call(app, {"action": "create", "space_name": "中文协作空间"})
            self.assertEqual("200 OK", code)
            self.assertIn("charset=utf-8", headers["Content-Type"])
            first = json.loads(body)
            self.assertIn("中文协作空间", body.decode("utf-8"))
            packet = parse_entry(first["code"])
            self.assertEqual("create", packet["kind"])
            second = json.loads(call(app, {"action": "create", "space_name": "另一个空间"})[1])
            self.assertNotEqual(first["entry_id"], second["entry_id"])
            self.assertNotEqual(packet["ticket"], parse_entry(second["code"])["ticket"])
            self.assertNotIn(packet["ticket"], Path(root, "hub.sqlite3").read_bytes().decode("utf-8", errors="ignore"))
            self.assertIn('lang="zh-CN"', PAGE)
            self.assertNotIn("\ufffd", PAGE)

    def test_ui_denies_missing_token_and_cross_origin_mutations(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, "local-test")
            self.assertEqual("403 Forbidden", call(app, {"action": "create"}, token="")[0][0])
            self.assertEqual("403 Forbidden", call(app, {"action": "create"}, origin="https://unrelated.example")[0][0])
            self.assertEqual([], app.hub.overview()["spaces"])

    def test_code_claim_is_single_use_and_rebinding_revokes_old_session(self):
        with tempfile.TemporaryDirectory() as root:
            hub = EntryHub(root)
            issued = hub.create_code("测试")
            packet = parse_entry(issued["code"])
            claimed = hub.accept(packet, "我的第一段对话")
            session = load_session(claimed["session_file"])
            binding = AgentBinding(claimed["data_dir"])
            binding.authorized(session["secret"])
            self.assertEqual("already_claimed", hub.accept(packet, "不得抢占")["status"])
            packet["ticket"] = "wrong"
            with self.assertRaises(PermissionError):
                hub.accept(packet, "另一段对话")
            replacement = parse_entry(hub.create_code(existing_id=issued["entry_id"])["code"])
            hub.accept(replacement, "用户明确更换的对话")
            with self.assertRaises(PermissionError):
                binding.authorized(session["secret"])

    def test_new_space_reuses_persona_but_never_copies_disclosure_grants(self):
        with tempfile.TemporaryDirectory() as root:
            hub = EntryHub(root)
            hub.remember(configuration())
            packet = parse_entry(hub.create_code("新空间")["code"])
            claimed = hub.accept(packet, "新空间对话")
            self.assertTrue(claimed["profile_reused"])
            self.assertEqual(configuration()["card"], claimed["template"]["card"])
            self.assertEqual(configuration()["policy"]["answer_quota"], claimed["template"]["policy"]["answer_quota"])
            for key in ("allowed_asker_ids", "allowed_topics", "allowed_project_scopes"):
                self.assertEqual([], claimed["template"]["policy"][key])
            self.assertEqual({"allow": [], "deny": []}, claimed["template"]["policy"]["disclosure"])
            self.assertFalse(claimed["space_permissions_inherited"])

    def test_joining_same_space_twice_does_not_allocate_another_identity(self):
        with tempfile.TemporaryDirectory() as root:
            hub = EntryHub(root)
            packet = {"schema": ENTRY_SCHEMA, "kind": "join", "entry_id": "a" * 32, "registry_path": "", "ticket": "",
                      "space_id": "unique-space", "space_name": "已有空间", "invite_code": "test-code", "expires_at": time.time() + 600}
            first = hub.accept(packet, "本次对话")
            second = hub.accept(packet, "不同对话")
            self.assertEqual("already_known_space", second["status"])
            self.assertEqual(first["entry_id"], second["entry_id"])
            self.assertNotIn("session_file", second)
            self.assertEqual(1, len(hub.overview()["spaces"]))

    def test_expired_entry_is_rejected_and_unique_space_required_for_invite(self):
        with tempfile.TemporaryDirectory() as root:
            packet = parse_entry(EntryHub(root).create_code()["code"])
            packet["expires_at"] = time.time() - 1
            with self.assertRaises(PermissionError):
                parse_entry(render_entry(packet))
            packet.update(kind="join", expires_at=time.time() + 60, invite_code="test", space_id=None)
            with self.assertRaises(ValueError):
                parse_entry(render_entry(packet))

    def test_ui_invitation_embeds_target_and_no_owner_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, "local-test")
            issued = app.hub.create_code("目标空间")
            with patch("amca.entry_hub.status", return_value={"connected": True, "space_id": "space-unique", "public_endpoint": "https://owner.example/a2a"}), patch(
                "amca.pilot.create_one_time_invite", return_value={"invite_code": "invitation-only", "expires_at": "unused"}):
                (code, _), body = call(app, {"action": "invite", "entry_id": issued["entry_id"]})
            self.assertEqual("200 OK", code)
            packet = parse_entry(json.loads(body)["code"])
            self.assertEqual("space-unique", packet["space_id"])
            self.assertEqual("join", packet["kind"])
            self.assertEqual("", packet["registry_path"])
            self.assertNotIn("api_key", body.decode())
            invitation_text = json.loads(body)["code"]
            self.assertNotIn(str(setup_cli.package_directory()), invitation_text)
            self.assertIn("自己电脑上解压", invitation_text)


class BindingTests(unittest.TestCase):
    def test_ack_requires_receiving_the_right_challenge_for_this_session(self):
        with tempfile.TemporaryDirectory() as root:
            binding = AgentBinding(root)
            secret = binding.bind("当前对话")["secret"]
            binding.activate("agent-a", "space-a")
            with self.assertRaises(PermissionError):
                binding.acknowledge(secret, "guess", "guess")
            challenge = binding.poll(secret)
            self.assertEqual("space-a", challenge["space_id"])
            with self.assertRaises(PermissionError):
                binding.acknowledge(secret, challenge["challenge_id"], "wrong")
            proof = binding.acknowledge(secret, challenge["challenge_id"], challenge["nonce"])
            self.assertTrue(proof["channel_verified"])
            self.assertFalse(proof["can_wake_closed_conversation"])
            self.assertNotIn(secret, json.dumps(proof))
            with self.assertRaises(PermissionError):
                binding.activate("agent-a", "another-space")
            with self.assertRaises(PermissionError):
                binding.acknowledge(secret, challenge["challenge_id"], challenge["nonce"])


if __name__ == "__main__":
    unittest.main()
