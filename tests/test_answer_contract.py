from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tests import _bootstrap  # noqa: F401

from amca.application import CollaborationService
from amca.domain import (
    AgentIdentity,
    AnswerStatus,
    EvidenceRecord,
    QuestionRequest,
)
from amca.infrastructure.memory import (
    MemoryAuditStore,
    MemoryEvidenceStore,
    MemoryNotifier,
    MemoryTaskStore,
)
from amca.runtime import DeterministicRuntime


NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
ASKER = AgentIdentity(
    agent_id="agent-a",
    owner_id="owner-a",
    display_name="A",
    owner_claim="Alice",
)
RESPONDER = AgentIdentity(
    agent_id="agent-b",
    owner_id="owner-b",
    display_name="B",
    owner_claim="Bob",
)


def question(topic: str = "motor.limit") -> QuestionRequest:
    return QuestionRequest(
        request_id="req-1",
        task_id="task-1",
        conversation_id="conv-1",
        asker=ASKER,
        responder=RESPONDER,
        question="What is the supported limit?",
        topic=topic,
        created_at=NOW,
    )


def record(
    *,
    evidence_id: str = "ev-1",
    content: str = "12 A continuous",
    allowed: frozenset[str] = frozenset({"agent-a"}),
    version: str = "datasheet-v2",
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        owner_agent_id="agent-b",
        topic="motor.limit",
        claim_key="continuous-current",
        content=content,
        source_label="private supplier sheet",
        version=version,
        updated_at=NOW,
        allowed_recipient_ids=allowed,
    )


class AnswerContractTests(unittest.TestCase):
    def make_service(self, records: list[EvidenceRecord]):
        evidence = MemoryEvidenceStore()
        for item in records:
            evidence.add(item)
        audit = MemoryAuditStore()
        tasks = MemoryTaskStore()
        notifier = MemoryNotifier()
        runtime = DeterministicRuntime(answer="The supported limit is 12 A continuous.")
        service = CollaborationService(
            evidence=evidence,
            runtime=runtime,
            audit=audit,
            tasks=tasks,
            notifier=notifier,
            clock=lambda: NOW,
        )
        return service, evidence, audit, tasks, notifier, runtime

    def test_supported_question_returns_answer_reason_time_and_version(self):
        service, _, _, _, _, runtime = self.make_service([record()])

        result = service.respond(question())

        self.assertEqual(AnswerStatus.ANSWER, result.status)
        self.assertEqual("The supported limit is 12 A continuous.", result.answer)
        self.assertIn("ev-1", result.reason)
        self.assertEqual(NOW, result.generated_at)
        self.assertEqual(("ev-1:datasheet-v2",), result.evidence_refs)
        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(("ev-1",), runtime.calls[0].evidence_ids)

    def test_missing_material_returns_unknown_without_calling_model(self):
        service, _, _, _, _, runtime = self.make_service([])

        result = service.respond(question(topic="unknown.topic"))

        self.assertEqual(AnswerStatus.UNKNOWN, result.status)
        self.assertIsNone(result.answer)
        self.assertIn("no matching evidence", result.reason.lower())
        self.assertEqual([], runtime.calls)

    def test_disclosable_conflict_returns_both_items_and_notifies_owner(self):
        service, _, _, _, notifier, runtime = self.make_service(
            [
                record(evidence_id="ev-old", content="10 A continuous", version="v1"),
                record(evidence_id="ev-new", content="12 A continuous", version="v2"),
            ]
        )

        result = service.respond(question())

        self.assertEqual(AnswerStatus.CONFLICT, result.status)
        self.assertEqual(2, len(result.conflict_items))
        self.assertEqual({"10 A continuous", "12 A continuous"}, {x.content for x in result.conflict_items})
        self.assertEqual(1, len(notifier.notifications))
        self.assertEqual("owner-b", notifier.notifications[0].owner_id)
        self.assertEqual([], runtime.calls)

    def test_restricted_conflict_refuses_without_leaking_content(self):
        secret = "SECRET-24-A"
        service, _, audit, _, notifier, runtime = self.make_service(
            [
                record(evidence_id="ev-public", content="12 A continuous"),
                record(
                    evidence_id="ev-secret",
                    content=secret,
                    allowed=frozenset({"agent-c"}),
                ),
            ]
        )

        result = service.respond(question())

        self.assertEqual(AnswerStatus.REFUSE, result.status)
        self.assertIsNone(result.answer)
        self.assertEqual((), result.conflict_items)
        self.assertNotIn(secret, result.reason)
        self.assertNotIn(secret, repr(audit.events))
        self.assertEqual(1, len(notifier.notifications))
        self.assertEqual([], runtime.calls)

    def test_single_restricted_record_refuses_before_model_call(self):
        service, _, _, _, _, runtime = self.make_service(
            [record(allowed=frozenset({"agent-c"}))]
        )

        result = service.respond(question())

        self.assertEqual(AnswerStatus.REFUSE, result.status)
        self.assertIsNone(result.answer)
        self.assertEqual([], runtime.calls)

    def test_identity_claim_is_explicitly_unverified(self):
        service, *_ = self.make_service([record()])

        result = service.respond(question())

        self.assertFalse(result.asker.claim_verified)
        self.assertFalse(result.responder.claim_verified)


if __name__ == "__main__":
    unittest.main()

