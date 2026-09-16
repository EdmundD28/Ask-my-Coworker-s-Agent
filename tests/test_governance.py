from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tests import _bootstrap  # noqa: F401

from amca.governance import CollaborationSpace, GovernanceError, Role
from amca.infrastructure.memory import MemoryAuditStore


NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.audit = MemoryAuditStore()
        self.space = CollaborationSpace.create(
            space_id="space-1",
            creator_user_id="user-a",
            audit=self.audit,
            clock=lambda: NOW,
        )
        self.space.add_member(actor_user_id="user-a", member_user_id="user-b")

    def test_creator_is_owner_and_transfer_waits_for_acceptance(self):
        self.assertEqual(Role.OWNER, self.space.role_of("user-a"))

        self.space.propose_owner_transfer(actor_user_id="user-a", target_user_id="user-b")

        self.assertEqual(Role.OWNER, self.space.role_of("user-a"))
        self.assertEqual(Role.MEMBER, self.space.role_of("user-b"))

    def test_acceptance_transfers_owner_and_demotes_previous_owner(self):
        self.space.propose_owner_transfer(actor_user_id="user-a", target_user_id="user-b")

        self.space.accept_owner_transfer(actor_user_id="user-b")

        self.assertEqual(Role.MEMBER, self.space.role_of("user-a"))
        self.assertEqual(Role.OWNER, self.space.role_of("user-b"))
        event = self.audit.events[-1]
        self.assertEqual("owner_transferred", event.event_type)
        self.assertEqual("user-a", event.details["previous_owner"])
        self.assertEqual("user-b", event.details["new_owner"])

    def test_transfer_target_must_already_be_a_member(self):
        with self.assertRaises(GovernanceError):
            self.space.propose_owner_transfer(
                actor_user_id="user-a",
                target_user_id="user-c",
            )


if __name__ == "__main__":
    unittest.main()

