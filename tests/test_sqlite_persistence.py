from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.application import CollaborationService
from amca.domain import (
    AgentIdentity,
    AnswerStatus,
    EvidenceRecord,
    QuestionRequest,
    TaskSnapshot,
)
from amca.governance import CollaborationSpace, Role
from amca.infrastructure.memory import MemoryNotifier
from amca.infrastructure.sqlite import (
    SqliteAuditStore,
    SqliteEvidenceStore,
    SqliteGovernanceStore,
    SqliteTaskStore,
)
from amca.runtime import DeterministicRuntime


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class SqlitePersistenceTests(unittest.TestCase):
    def test_task_state_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            first = SqliteTaskStore(database)
            first.upsert(TaskSnapshot("task-1", ("agent-a", "agent-b"), "answer", NOW))
            first.close()

            second = SqliteTaskStore(database)
            restored = second.get("task-1")

            self.assertIsNotNone(restored)
            self.assertEqual(("agent-a", "agent-b"), restored.participant_ids)
            self.assertEqual("answer", restored.status)
            self.assertEqual(NOW, restored.updated_at)
            second.close()

    def test_private_evidence_store_is_bound_to_one_owner_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bob-private.sqlite3"
            store = SqliteEvidenceStore(database, owner_agent_id="agent-b")
            store.add(
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

            self.assertEqual(1, len(store.find("agent-b", "project.deadline")))
            with self.assertRaises(PermissionError):
                store.find("agent-a", "project.deadline")
            store.close()

    def test_service_audit_survives_restart_without_private_source_text(self):
        secret = "PRIVATE-SOURCE-TEXT"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = SqliteEvidenceStore(root / "bob.sqlite3", owner_agent_id="agent-b")
            audit = SqliteAuditStore(root / "coordinator.sqlite3")
            tasks = SqliteTaskStore(root / "coordinator.sqlite3")
            evidence.add(
                EvidenceRecord(
                    evidence_id="ev-secret",
                    owner_agent_id="agent-b",
                    topic="secret.topic",
                    claim_key="secret",
                    content=secret,
                    source_label="private file",
                    version="v1",
                    updated_at=NOW,
                    allowed_recipient_ids=frozenset({"agent-c"}),
                )
            )
            service = CollaborationService(
                evidence=evidence,
                runtime=DeterministicRuntime(answer="must not run"),
                audit=audit,
                tasks=tasks,
                notifier=MemoryNotifier(),
                clock=lambda: NOW,
            )
            result = service.respond(
                QuestionRequest(
                    request_id="req-secret",
                    task_id="task-secret",
                    conversation_id="conv-secret",
                    asker=AgentIdentity("agent-a", "owner-a", "A", "Alice"),
                    responder=AgentIdentity("agent-b", "owner-b", "B", "Bob"),
                    question="Tell me the secret",
                    topic="secret.topic",
                    created_at=NOW,
                )
            )
            self.assertEqual(AnswerStatus.REFUSE, result.status)
            evidence.close()
            audit.close()
            tasks.close()

            reopened = SqliteAuditStore(root / "coordinator.sqlite3")
            serialized = repr(reopened.events)

            self.assertIn("req-secret", serialized)
            self.assertNotIn(secret, serialized)
            reopened.close()

    def test_governance_state_and_pending_transfer_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            audit = SqliteAuditStore(database)
            governance = SqliteGovernanceStore(database)
            space = CollaborationSpace.create(
                space_id="space-1",
                creator_user_id="user-a",
                audit=audit,
                clock=lambda: NOW,
            )
            space.add_member(actor_user_id="user-a", member_user_id="user-b")
            space.propose_owner_transfer(actor_user_id="user-a", target_user_id="user-b")
            governance.save(space)
            governance.close()
            audit.close()

            audit2 = SqliteAuditStore(database)
            governance2 = SqliteGovernanceStore(database)
            restored = governance2.load("space-1", audit=audit2, clock=lambda: NOW)

            self.assertEqual(Role.OWNER, restored.role_of("user-a"))
            self.assertEqual(Role.MEMBER, restored.role_of("user-b"))
            restored.accept_owner_transfer(actor_user_id="user-b")
            governance2.save(restored)
            self.assertEqual(Role.OWNER, restored.role_of("user-b"))
            self.assertEqual("owner_transferred", audit2.events[-1].event_type)
            governance2.close()
            audit2.close()


if __name__ == "__main__":
    unittest.main()

