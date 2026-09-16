from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from tests import _bootstrap  # noqa: F401

from amca.application import CollaborationService
from amca.domain import AgentIdentity, EvidenceRecord, QuestionRequest
from amca.infrastructure.memory import (
    MemoryAuditStore,
    MemoryEvidenceStore,
    MemoryNotifier,
    MemoryTaskStore,
)
from amca.monitor import create_monitor_app
from amca.runtime import DeterministicRuntime


NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


class PrivacyAndPublicationTests(unittest.TestCase):
    def setUp(self):
        self.asker = AgentIdentity("agent-a", "owner-a", "A", "Alice")
        self.responder = AgentIdentity("agent-b", "owner-b", "B", "Bob")
        self.evidence = MemoryEvidenceStore()
        self.evidence.add(
            EvidenceRecord(
                evidence_id="ev-private",
                owner_agent_id="agent-b",
                topic="project.deadline",
                claim_key="deadline",
                content="Private answer body: Friday",
                source_label="Bob private notes",
                version="2026-09-15",
                updated_at=NOW,
                allowed_recipient_ids=frozenset({"agent-a"}),
            )
        )
        self.audit = MemoryAuditStore()
        self.tasks = MemoryTaskStore()
        self.service = CollaborationService(
            evidence=self.evidence,
            runtime=DeterministicRuntime(answer="Friday"),
            audit=self.audit,
            tasks=self.tasks,
            notifier=MemoryNotifier(),
            clock=lambda: NOW,
        )
        self.request = QuestionRequest(
            request_id="req-private",
            task_id="task-private",
            conversation_id="conv-private",
            asker=self.asker,
            responder=self.responder,
            question="When is the private project deadline?",
            topic="project.deadline",
            created_at=NOW,
        )

    def test_shared_task_snapshot_contains_status_but_no_bilateral_body(self):
        result = self.service.respond(self.request)

        snapshot = self.service.shared_task_snapshot("task-private")
        serialized = json.dumps(snapshot, ensure_ascii=False)

        self.assertIn(result.status.value, serialized)
        self.assertIn("agent-a", serialized)
        self.assertIn("agent-b", serialized)
        for forbidden in (
            self.request.question,
            result.answer,
            result.reason,
            "Private answer body: Friday",
            "Bob private notes",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_audit_contains_failure_basis_but_not_untransmitted_source_text(self):
        self.service.respond(self.request)

        serialized = repr(self.audit.events)

        self.assertIn("req-private", serialized)
        self.assertIn("answer", serialized.lower())
        self.assertNotIn("Private answer body: Friday", serialized)

    def test_new_recipient_requires_responder_owner_approval(self):
        result = self.service.respond(self.request)

        blocked = self.service.authorize_publication(
            result=result,
            recipient_ids=frozenset({"agent-c"}),
            approving_owner_ids=frozenset(),
        )
        allowed = self.service.authorize_publication(
            result=result,
            recipient_ids=frozenset({"agent-c"}),
            approving_owner_ids=frozenset({"owner-b"}),
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(frozenset({"owner-b"}), blocked.required_owner_ids)
        self.assertTrue(allowed.allowed)

    def test_monitor_health_and_task_api_do_not_expose_body(self):
        self.service.respond(self.request)
        app = create_monitor_app(self.service)

        health = app.handle("GET", "/health")
        tasks = app.handle("GET", "/api/tasks")

        self.assertEqual(200, health.status)
        self.assertEqual("ok", json.loads(health.body)["status"])
        self.assertEqual(200, tasks.status)
        self.assertNotIn(self.request.question, tasks.body)
        self.assertNotIn("Friday", tasks.body)
        self.assertNotIn("Bob private notes", tasks.body)


if __name__ == "__main__":
    unittest.main()

