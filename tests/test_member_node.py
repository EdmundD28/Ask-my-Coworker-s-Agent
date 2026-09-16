from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import _bootstrap  # noqa: F401

from amca.member_node import BootstrapA2AApp, SwitchableApp, main, wait_for_public_endpoint


def call(app, method, path, body=b""):
    status = []
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    output = b"".join(app(environ, lambda value, headers: status.append(value)))
    return status[0], json.loads(output)


class MemberNodeTests(unittest.TestCase):
    def test_public_endpoint_waits_for_dns_propagation(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        with patch(
            "amca.member_node.urlopen",
            side_effect=[OSError("dns pending"), response],
        ) as request:
            wait_for_public_endpoint(
                "https://new.trycloudflare.com/health",
                timeout=5,
                sleeper=lambda _: None,
            )
        self.assertEqual(2, request.call_count)

    def test_bootstrap_endpoint_is_reachable_before_identity_exists(self):
        app = BootstrapA2AApp()
        status, health = call(app, "GET", "/health")
        self.assertEqual("200 OK", status)
        self.assertEqual("bootstrapping", health["status"])
        status, response = call(app, "POST", "/a2a", b"{}")
        self.assertEqual("200 OK", status)
        self.assertEqual(-32601, response["error"]["code"])

    def test_switchable_app_replaces_bootstrap_without_changing_port(self):
        app = SwitchableApp(BootstrapA2AApp())
        app.replace(lambda environ, start: (start("200 OK", []), [b'{"status":"online"}'])[1])
        _, response = call(app, "GET", "/health")
        self.assertEqual("online", response["status"])

    def test_successful_join_opens_the_invitee_local_chat_page(self):
        class Process:
            def wait(self):
                raise KeyboardInterrupt

        tunnel = Mock(process=Process())
        server = Mock()
        receiver = Mock()
        state = {
            "agent_id": "agent-b",
            "local_chat_url": "http://127.0.0.1:8123/#token=secret",
            "tunnel": tunnel,
            "server": server,
            "receiver": receiver,
        }
        with tempfile.TemporaryDirectory() as root:
            invitation = Path(root) / "invite.json"
            invitation.write_text(
                json.dumps({"invite_code": "one-time-code"}), encoding="utf-8"
            )
            with patch("amca.member_node.run_member", return_value=state), patch(
                "amca.member_node.webbrowser.open"
            ) as opened:
                main(["--invite-file", str(invitation), "--data-dir", root])

        opened.assert_called_once_with(state["local_chat_url"], new=2)
        server.shutdown.assert_called_once()
        receiver.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
