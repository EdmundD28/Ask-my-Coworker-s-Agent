from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401

from amca.credentials import AcnCredentialBundle
from amca.pilot import connect_owner, enable_direct_chat, invite_agent, join_one_time_invite
from amca.transport import AgentIdentityReceipt, JsonHttpError


class MemoryStore:
    saved: list[AcnCredentialBundle] = []
    loaded: AcnCredentialBundle | None = None

    def __init__(self, path):
        self.path = Path(path)

    def save(self, bundle):
        self.saved.append(bundle)

    def load(self):
        if self.loaded is None:
            raise AssertionError("test credential bundle not configured")
        return self.loaded


class FakeAdapter:
    registration = None
    invitations = []
    policy_updates = []

    def __init__(self, base_url, **kwargs):
        self.base_url = base_url

    def register_agent(self, registration):
        self.registration = registration
        return AgentIdentityReceipt(
            "acn-owner",
            "secret",
            status="active",
            claim_status="unclaimed",
            claim_url="https://claim.example/acn-owner",
            endpoint_reachable=True,
            a2a_handshake_ok=True,
        )

    def create_network(self, *, name, description, is_private):
        return {
            "slug": "amca-private",
            "is_public": False,
            "join_policy": "approval",
        }

    def invite(self, slug, agent_id, *, note=None):
        self.invitations.append((slug, agent_id, note))
        return {"invitation_id": "invite-1", "status": "pending"}

    def update_endpoint(self, endpoint):
        return {
            "endpoint": endpoint,
            "endpoint_reachable": True,
            "a2a_handshake_ok": True,
        }

    def update_communication_policy(self, mode):
        self.policy_updates.append(mode)
        return {"communication_policy": {"mode": mode}}

    def communication_profile(self):
        return {"mode": "open", "unread_manifest_count": 0}


class DnsDelayedAdapter(FakeAdapter):
    attempts = 0

    def register_agent(self, registration):
        type(self).attempts += 1
        if type(self).attempts < 3:
            raise JsonHttpError(
                400,
                '{"detail":"Endpoint host cannot be resolved or is not in an allowed address range."}',
            )
        return AgentIdentityReceipt("member-1", "member-secret")


class PilotTests(unittest.TestCase):
    def test_member_registration_retries_only_delayed_endpoint_dns(self):
        MemoryStore.saved = []
        DnsDelayedAdapter.attempts = 0
        delays = []
        notices = []
        identity = AgentIdentityReceipt("member-1", "member-secret")
        with patch("amca.pilot.WindowsCredentialStore", MemoryStore), patch(
            "amca.pilot.AcnNetworkAdapter", DnsDelayedAdapter
        ), patch(
            "amca.pilot.join_from_invite",
            return_value=(identity, {"subnet_slug": "private-space"}),
        ):
            result = join_one_time_invite(
                "invite-code",
                endpoint="https://new.trycloudflare.com/a2a",
                credential_path="unused-member-credentials.json",
                sleeper=delays.append,
                on_registration_retry=lambda attempt, total: notices.append(
                    (attempt, total)
                ),
            )
        self.assertEqual(3, DnsDelayedAdapter.attempts)
        self.assertEqual([3.0, 3.0], delays)
        self.assertEqual([(1, 10), (2, 10)], notices)
        self.assertEqual("private-space", result["subnet_slug"])
    def test_connect_owner_registers_real_endpoint_then_private_space(self):
        MemoryStore.saved = []
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "credentials.json"
            with patch("amca.pilot.WindowsCredentialStore", MemoryStore), patch(
                "amca.pilot.AcnNetworkAdapter", FakeAdapter
            ):
                result = connect_owner(
                    endpoint="https://owner.example/a2a",
                    credential_path=path,
                )

        self.assertTrue(result["endpoint_reachable"])
        self.assertTrue(result["a2a_handshake_ok"])
        self.assertTrue(result["subnet_private"])
        self.assertTrue(result["claim_required"])
        self.assertNotIn("claim_url", result)
        self.assertEqual("approval", result["join_policy"])
        self.assertEqual("amca-private", MemoryStore.saved[-1].subnet_slug)

    def test_invite_uses_owner_credentials_without_exposing_the_api_key(self):
        MemoryStore.loaded = AcnCredentialBundle(
            base_url="https://acn.example/api/v1",
            agent_id="acn-owner",
            api_key="secret",
            a2a_endpoint="https://owner.example/a2a",
            subnet_slug="amca-private",
        )
        FakeAdapter.invitations = []
        with patch("amca.pilot.WindowsCredentialStore", MemoryStore), patch(
            "amca.pilot.AcnNetworkAdapter", FakeAdapter
        ):
            result = invite_agent(
                "credentials.json",
                invitee_agent_id="acn-invitee",
                note="owner-approved pilot",
            )

        self.assertEqual(
            [("amca-private", "acn-invitee", "owner-approved pilot")],
            FakeAdapter.invitations,
        )
        self.assertNotIn("api_key", result)
        self.assertEqual("pending", result["status"])

    def test_inviting_the_owner_again_is_rejected_before_network_use(self):
        MemoryStore.loaded = AcnCredentialBundle(
            base_url="https://acn.example/api/v1",
            agent_id="acn-owner",
            api_key="secret",
            a2a_endpoint="https://owner.example/a2a",
            subnet_slug="amca-private",
        )
        with patch("amca.pilot.WindowsCredentialStore", MemoryStore):
            with self.assertRaisesRegex(ValueError, "already a member"):
                invite_agent(
                    "credentials.json",
                    invitee_agent_id="acn-owner",
                )

    def test_enable_direct_chat_verifies_endpoint_before_opening_transport(self):
        MemoryStore.loaded = AcnCredentialBundle(
            base_url="https://acn.example/api/v1",
            agent_id="acn-owner",
            api_key="secret",
            a2a_endpoint="https://owner.example/a2a",
            subnet_slug="amca-private",
        )
        FakeAdapter.policy_updates = []
        with patch("amca.pilot.WindowsCredentialStore", MemoryStore), patch(
            "amca.pilot.AcnNetworkAdapter", FakeAdapter
        ):
            result = enable_direct_chat("credentials.json")

        self.assertEqual(["open"], FakeAdapter.policy_updates)
        self.assertEqual("direct", result["delivery"])
        self.assertEqual("open", result["communication_mode"])
