from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.onboarding import OwnerOnboardingService, SqliteInviteStore, join_from_invite
from amca.transport import AgentIdentityReceipt, AgentRegistration


NOW = datetime(2026, 9, 16, tzinfo=UTC)


class FakeOwnerAdapter:
    def __init__(self):
        self.calls = []

    def invite(self, subnet, agent_id, *, note=None):
        self.calls.append((subnet, agent_id, note))
        return {"invitation_id": "inv-1"}


class FakeInviteeAdapter:
    def __init__(self):
        self.accepted = []

    def register_agent(self, registration):
        return AgentIdentityReceipt("member-1", "private-key")

    def accept_invitation(self, subnet, invitation_id):
        self.accepted.append((subnet, invitation_id))

    def discover(self, subnet):
        return [{"agent_id": "member-1"}]


class LoopbackHttp:
    def __init__(self, service):
        self.service = service

    def request_json(self, method, url, *, headers=None, payload=None):
        return self.service.admit(payload["token"], payload["agent_id"])


class OnboardingTests(unittest.TestCase):
    def test_one_action_join_registers_invites_accepts_and_verifies(self):
        with tempfile.TemporaryDirectory() as root:
            store = SqliteInviteStore(Path(root) / "coordinator.db")
            owner = FakeOwnerAdapter()
            service = OwnerOnboardingService(store, owner, clock=lambda: NOW)
            code = service.create_invite(
                join_url="https://owner.example/onboarding/join",
                subnet_slug="private-space",
            ).invite_code
            invitee = FakeInviteeAdapter()

            identity, receipt = join_from_invite(
                code,
                adapter=invitee,
                registration=AgentRegistration("B", "member", (), "https://b.example/a2a"),
                http=LoopbackHttp(service),
            )

            self.assertEqual("member-1", identity.agent_id)
            self.assertEqual("private-space", receipt["subnet_slug"])
            self.assertEqual([("private-space", "inv-1")], invitee.accepted)
            self.assertEqual("member-1", owner.calls[0][1])
            store.close()

    def test_invite_is_single_use(self):
        with tempfile.TemporaryDirectory() as root:
            store = SqliteInviteStore(Path(root) / "coordinator.db")
            service = OwnerOnboardingService(store, FakeOwnerAdapter(), clock=lambda: NOW)
            receipt = service.create_invite(
                join_url="https://owner.example/onboarding/join",
                subnet_slug="private-space",
            )
            # Decode only inside the test to exercise the public code through join_from_invite.
            invitee = FakeInviteeAdapter()
            join_from_invite(
                receipt.invite_code,
                adapter=invitee,
                registration=AgentRegistration("B", "member", (), "https://b.example/a2a"),
                http=LoopbackHttp(service),
            )
            with self.assertRaisesRegex(PermissionError, "already used"):
                join_from_invite(
                    receipt.invite_code,
                    adapter=invitee,
                    registration=AgentRegistration("C", "member", (), "https://c.example/a2a"),
                    http=LoopbackHttp(service),
                )
            store.close()

    def test_expired_invite_is_rejected_before_owner_invitation(self):
        with tempfile.TemporaryDirectory() as root:
            store = SqliteInviteStore(Path(root) / "coordinator.db")
            owner = FakeOwnerAdapter()
            service = OwnerOnboardingService(store, owner, clock=lambda: NOW)
            receipt = service.create_invite(
                join_url="https://owner.example/onboarding/join",
                subnet_slug="private-space",
                lifetime=timedelta(seconds=1),
            )
            service.clock = lambda: NOW + timedelta(seconds=2)
            with self.assertRaisesRegex(PermissionError, "expired"):
                join_from_invite(
                    receipt.invite_code,
                    adapter=FakeInviteeAdapter(),
                    registration=AgentRegistration("B", "member", (), "https://b.example/a2a"),
                    http=LoopbackHttp(service),
                )
            self.assertEqual([], owner.calls)
            store.close()


if __name__ == "__main__":
    unittest.main()
