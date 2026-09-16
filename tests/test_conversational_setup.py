from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from tests import _bootstrap  # noqa: F401
from amca.credentials import WindowsCredentialStore
from amca.setup_cli import main, start
from amca.setup_config import SCHEMA, config_digest, contract, validate_config
from amca.setup_handbook import write_handbook
from amca.setup_node import apply_config, connect, local_call, node_lock, serve, status
from amca.onboarding import INVITE_SCHEMA, _encode
from amca.transport import AgentIdentityReceipt, JsonHttpError


def configuration():
    return {"schema": SCHEMA, "mode": "create", "space_name": "测试空间",
            "invite_code": "", "card": {"role_name": "排期助手", "description": "回答排期问题", "owner_claim": "测试用户"},
            "policy": {"confirmation_mode": "preauthorized", "allowed_asker_ids": ["peer"],
                       "allowed_topics": ["schedule"], "allowed_project_scopes": ["project"],
                       "answer_quota": {"limit": 3, "scope": "sender", "window_seconds": 86400},
                       "disclosure": {"allow": ["已公开排期"], "deny": ["私人联系方式"]}}}


class SetupContractTests(unittest.TestCase):
    def test_user_choices_survive_apply_and_readback_without_connecting(self):
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter") as adapter:
            value = configuration()
            receipt = apply_config(root, value, config_digest(value))
            observed = status(root)
            self.assertEqual(value, observed["configuration"])
            self.assertEqual(receipt["config_digest"], observed["config_digest"])
            self.assertFalse(observed["connected"])
            self.assertFalse(observed["automatic_answers_enabled"])
            self.assertFalse((Path(root) / "credentials.json").exists())
            adapter.assert_not_called()

    def test_changed_configuration_cannot_use_previous_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            value = configuration()
            digest = config_digest(value)
            value["policy"]["answer_quota"]["limit"] = None
            with self.assertRaisesRegex(ValueError, "摘要"):
                apply_config(root, value, digest)
            self.assertFalse((Path(root) / "setup.json").exists())

    def test_rejects_missing_fields_unknown_permissions_boolean_limits_and_placeholders(self):
        cases = []
        value = configuration(); del value["policy"]["disclosure"]; cases.append(value)
        value = configuration(); value["policy"]["admin_override"] = True; cases.append(value)
        value = configuration(); value["policy"]["answer_quota"]["limit"] = True; cases.append(value)
        value = configuration(); value["policy"]["answer_quota"]["window_seconds"] = -1; cases.append(value)
        value = configuration(); value["policy"]["allowed_asker_ids"] = ["*"]; cases.append(value)
        cases.append(contract()["template"])
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(value)

    def test_policy_cannot_be_overwritten_while_node_holds_lock(self):
        with tempfile.TemporaryDirectory() as root:
            value = configuration()
            apply_config(root, value, config_digest(value))
            with node_lock(root), self.assertRaises(RuntimeError):
                apply_config(root, value, config_digest(value))
            self.assertEqual(value, status(root)["configuration"])

    def test_cli_returns_nonzero_for_invalid_json_and_does_not_create_session(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "invalid.json"
            path.write_text("not json", encoding="utf-8")
            with patch("sys.stdout", new=io.StringIO()) as output:
                result = main(["--data-dir", root, "validate", "--file", str(path)])
            self.assertEqual(1, result)
            self.assertEqual("error", json.loads(output.getvalue())["status"])
            self.assertFalse((Path(root) / "setup.json").exists())

    def test_start_is_hidden_and_does_not_claim_immediate_connection(self):
        with tempfile.TemporaryDirectory() as root:
            value = configuration(); apply_config(root, value, config_digest(value))
            with patch("amca.setup_cli.subprocess.Popen", return_value=Mock(pid=123)) as launch:
                receipt = start(root)
            self.assertFalse(receipt["connected"])
            self.assertEqual("starting", receipt["status"])
            self.assertEqual(subprocess.CREATE_NO_WINDOW, launch.call_args.kwargs["creationflags"])
            self.assertEqual("1", launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"])
            self.assertIn("_serve", launch.call_args.args[0])

    def test_handbook_is_self_contained_and_uses_full_product_name(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_handbook(Path(root) / "setup.md", package_directory=Path(root) / "owner's package")
            content = path.read_text(encoding="utf-8")
            for command in ("describe", "validate --file", "apply --file", " start", " status", " stop"):
                self.assertIn(command, content)
            self.assertIn("owner''s package", content)
            self.assertIn("必须询问当前用户", content)
            self.assertNotIn("AMCA", content)
            self.assertIn("三项保留空数组", content)
            from amca.agent_guide import GUIDE
            self.assertTrue(content.endswith(GUIDE))


class SetupConnectionTests(unittest.TestCase):
    def adapter(self):
        adapter = Mock()
        adapter.register_agent.return_value = AgentIdentityReceipt("setup-agent", "not-a-real-secret")
        adapter.create_network.return_value = {"slug": "setup-space"}
        adapter.update_endpoint.return_value = {"endpoint_reachable": True, "a2a_handshake_ok": True}
        adapter.discover.return_value = [{"agent_id": "setup-agent"}]
        return adapter

    def test_create_and_restart_reuse_credentials_and_network(self):
        adapter = self.adapter()
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter):
            first, _ = connect(configuration(), root, "https://first.example/a2a")
            second, _ = connect(configuration(), root, "https://second.example/a2a")
            self.assertEqual(first.agent_id, second.agent_id)
            self.assertEqual("https://second.example/a2a", WindowsCredentialStore(Path(root) / "credentials.json").load().a2a_endpoint)
            self.assertNotIn("not-a-real-secret", (Path(root) / "credentials.json").read_text())
            adapter.register_agent.assert_called_once()
            adapter.create_network.assert_called_once()

    def test_invitee_registers_accepts_and_verifies_without_creating_a_space(self):
        adapter = self.adapter()
        http = Mock()
        http.request_json.return_value = {"subnet_slug": "invited-space", "invitation_id": "invitation"}
        value = configuration()
        value.update(mode="join", space_name="", invite_code=_encode({"schema": INVITE_SCHEMA,
            "join_url": "https://owner.example/onboarding/join", "token": "test-one-time-token"}))
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter), patch("amca.pilot.AcnNetworkAdapter", return_value=adapter), patch("amca.onboarding.UrllibJsonHttpClient", return_value=http):
            apply_config(root, value, config_digest(value))
            identity, _ = connect(value, root, "https://member.example/a2a")
            self.assertEqual("invited-space", identity.subnet_slug)
            adapter.accept_invitation.assert_called_once_with("invited-space", "invitation")
            adapter.create_network.assert_not_called()
            self.assertNotIn("test-one-time-token", json.dumps(status(root)))
            self.assertNotIn("not-a-real-secret", json.dumps(status(root)))

    def test_registered_identity_cannot_be_changed_by_overwriting_configuration(self):
        adapter = self.adapter()
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter):
            value = configuration(); apply_config(root, value, config_digest(value))
            connect(value, root, "https://first.example/a2a")
            value["card"]["owner_claim"] = "another owner"
            with self.assertRaisesRegex(ValueError, "已登记身份"):
                apply_config(root, value, config_digest(value))

    def test_uncertain_network_creation_is_not_repeated(self):
        adapter = self.adapter()
        adapter.create_network.side_effect = JsonHttpError(None, "secret remote body")
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter):
            with self.assertRaises(JsonHttpError):
                connect(configuration(), root, "https://first.example/a2a")
            with self.assertRaisesRegex(RuntimeError, "结果不确定"):
                connect(configuration(), root, "https://first.example/a2a")
            adapter.create_network.assert_called_once()
            self.assertTrue((Path(root) / "credentials.json").exists())

    def test_failed_membership_verification_never_returns_success(self):
        adapter = self.adapter(); adapter.discover.return_value = []
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter):
            with self.assertRaisesRegex(RuntimeError, "成员身份"):
                connect(configuration(), root, "https://first.example/a2a")

    def test_real_local_server_requires_token_reports_actual_mode_and_stops(self):
        adapter = self.adapter()
        tunnel = Mock(url="https://isolated.example")
        tunnel.process.poll.return_value = None
        errors = []
        with tempfile.TemporaryDirectory() as root, patch("amca.setup_node.AcnNetworkAdapter", return_value=adapter), patch("amca.setup_node.QuickTunnel.start", return_value=tunnel):
            value = configuration(); apply_config(root, value, config_digest(value))
            def run():
                try:
                    serve(root, "unused")
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not status(root)["connected"] and not errors:
                    time.sleep(0.05)
                current = status(root)
                self.assertTrue(current["connected"], errors)
                self.assertEqual("confirm_each", current["effective_inbound_mode"])
                self.assertEqual("preauthorized", current["configuration"]["policy"]["confirmation_mode"])
                self.assertFalse(current["automatic_answers_enabled"])
                self.assertEqual("setup-agent", local_call(root, "GET", "/api/me")["agent_id"])
                # Read the encrypted test session only to exercise a real unauthenticated HTTP request.
                import base64
                from amca.credentials import _unprotect
                session = json.loads(_unprotect(base64.b64decode((Path(root) / "session.dpapi").read_bytes())))
                with self.assertRaises(HTTPError) as denied:
                    urlopen(f"http://127.0.0.1:{session['port']}/api/setup/status", timeout=2)
                self.assertEqual(403, denied.exception.code)
                with self.assertRaises(RuntimeError):
                    local_call(root, "POST", "/api/setup/config", value)
                self.assertNotIn(session["token"], json.dumps(current))
            finally:
                if status(root)["connected"]:
                    local_call(root, "POST", "/api/setup/stop", {})
                thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertFalse(status(root)["connected"])
            self.assertFalse((Path(root) / "session.dpapi").exists())
            tunnel.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
