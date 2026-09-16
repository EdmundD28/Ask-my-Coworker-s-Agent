from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.directory import (
    ConfirmationMode,
    FollowupScope,
    InboundQuestionPolicy,
    MemberCard,
    MemberProfile,
)
from amca.infrastructure.sqlite import SqliteMemberDirectory


class MemberDirectoryTests(unittest.TestCase):
    def test_member_card_and_inbound_policy_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            first = SqliteMemberDirectory(database)
            first.register(
                MemberProfile(
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
                        followup_limit=3,
                        followup_scope=FollowupScope.CHAIN,
                    ),
                )
            )
            first.close()

            second = SqliteMemberDirectory(database)
            restored = second.get("agent-b")

            self.assertEqual("Project scheduler", restored.card.role_name)
            self.assertEqual("Bob", restored.card.owner_claim)
            self.assertFalse(restored.card.claim_verified)
            self.assertEqual(3, restored.inbound_policy.followup_limit)
            second.close()

    def test_public_cards_expose_role_claim_but_not_private_policy_details(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_store = SqliteMemberDirectory(Path(directory) / "coordinator.sqlite3")
            directory_store.register(
                MemberProfile(
                    card=MemberCard(
                        agent_id="agent-b",
                        role_name="Project scheduler",
                        description="Answers scheduling questions",
                        owner_claim="Bob",
                    ),
                    inbound_policy=InboundQuestionPolicy(
                        confirmation_mode=ConfirmationMode.PREAUTHORIZED,
                        allowed_asker_ids=frozenset({"agent-a"}),
                        allowed_topics=frozenset({"private.topic"}),
                        allowed_project_scopes=frozenset({"secret-project"}),
                        followup_limit=3,
                        followup_scope=FollowupScope.CHAIN,
                    ),
                )
            )

            public = directory_store.public_cards()
            serialized = json.dumps(public, ensure_ascii=False)

            self.assertIn("Project scheduler", serialized)
            self.assertIn("Bob", serialized)
            self.assertIn('"claim_verified": false', serialized)
            self.assertNotIn("private.topic", serialized)
            self.assertNotIn("secret-project", serialized)
            self.assertNotIn("allowed_asker_ids", serialized)
            directory_store.close()


if __name__ == "__main__":
    unittest.main()

