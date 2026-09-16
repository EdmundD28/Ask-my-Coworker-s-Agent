from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.admission import QuestionAdmissionService
from amca.directory import (
    ConfirmationMode,
    FollowupScope,
    InboundQuestionPolicy,
    MemberCard,
    MemberProfile,
)
from amca.infrastructure.sqlite import SqliteAdmissionStore
from amca.protocol import AdmissionStatus, QuestionMessageV1, TriggerOrigin


NOW = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)


def message(**changes) -> QuestionMessageV1:
    base = QuestionMessageV1(
        schema="amca.question.v1",
        request_id="req-1",
        task_id="task-1",
        conversation_id="conv-1",
        chain_id="chain-1",
        parent_request_id=None,
        from_agent="agent-a",
        target_agent="agent-b",
        project_scope="project-blue",
        question="When is the deadline?",
        topic="project.deadline",
        trigger_origin=TriggerOrigin.OWNER_EXPLICIT,
        created_at=NOW,
        nonce="nonce-1",
    )
    return replace(base, **changes)


def profile(**policy_changes) -> MemberProfile:
    policy = InboundQuestionPolicy(
        confirmation_mode=ConfirmationMode.PREAUTHORIZED,
        allowed_asker_ids=frozenset({"agent-a"}),
        allowed_topics=frozenset({"project.deadline"}),
        allowed_project_scopes=frozenset({"project-blue"}),
        followup_limit=None,
        followup_scope=FollowupScope.CHAIN,
    )
    policy = replace(policy, **policy_changes)
    return MemberProfile(
        card=MemberCard(
            agent_id="agent-b",
            role_name="Project scheduler",
            description="Answers scheduling questions for Project Blue",
            owner_claim="Bob",
        ),
        inbound_policy=policy,
    )


class QuestionAdmissionTests(unittest.TestCase):
    def make_service(self, database: Path):
        store = SqliteAdmissionStore(database)
        service = QuestionAdmissionService(
            store=store,
            clock=lambda: NOW,
            max_age=timedelta(minutes=5),
        )
        return service, store

    def test_owner_triggered_preauthorized_question_is_accepted_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            service, store = self.make_service(Path(directory) / "coordinator.sqlite3")

            decision = service.admit(
                message(),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )

            self.assertEqual(AdmissionStatus.ACCEPT, decision.status)
            event = store.events[-1]
            self.assertEqual("req-1", event["request_id"])
            self.assertEqual("owner_explicit", event["trigger_origin"])
            self.assertNotIn("When is the deadline?", repr(event))
            store.close()

    def test_automatic_trigger_is_rejected_in_v1_before_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, store = self.make_service(Path(directory) / "coordinator.sqlite3")

            rejected = service.admit(
                message(trigger_origin=TriggerOrigin.AGENT_AUTOMATIC),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )
            accepted = service.admit(
                message(),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )

            self.assertEqual(AdmissionStatus.REJECT, rejected.status)
            self.assertEqual("automatic_trigger_not_allowed_v1", rejected.reason_code)
            self.assertEqual(AdmissionStatus.ACCEPT, accepted.status)
            store.close()

    def test_transport_sender_must_match_message_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            service, store = self.make_service(Path(directory) / "coordinator.sqlite3")

            decision = service.admit(
                message(),
                recipient=profile(),
                authenticated_sender_id="agent-c",
            )

            self.assertEqual(AdmissionStatus.REJECT, decision.status)
            self.assertEqual("sender_mismatch", decision.reason_code)
            store.close()

    def test_stale_question_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service, store = self.make_service(Path(directory) / "coordinator.sqlite3")

            decision = service.admit(
                message(created_at=NOW - timedelta(minutes=6)),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )

            self.assertEqual(AdmissionStatus.REJECT, decision.status)
            self.assertEqual("stale_question", decision.reason_code)
            store.close()

    def test_nonce_and_request_replay_remain_blocked_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            service, store = self.make_service(database)
            first = service.admit(
                message(),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )
            store.close()

            service2, store2 = self.make_service(database)
            nonce_replay = service2.admit(
                message(request_id="req-2"),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )
            request_replay = service2.admit(
                message(nonce="nonce-2"),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )

            self.assertEqual(AdmissionStatus.ACCEPT, first.status)
            self.assertEqual("nonce_replay", nonce_replay.reason_code)
            self.assertEqual("request_replay", request_replay.reason_code)
            store2.close()

    def test_confirm_each_and_out_of_scope_both_request_owner_review(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            service, store = self.make_service(database)
            confirm_each = service.admit(
                message(),
                recipient=profile(confirmation_mode=ConfirmationMode.CONFIRM_EACH),
                authenticated_sender_id="agent-a",
            )
            out_of_scope = service.admit(
                message(request_id="req-2", nonce="nonce-2", topic="finance.private"),
                recipient=profile(),
                authenticated_sender_id="agent-a",
            )

            self.assertEqual(AdmissionStatus.ASK_HUMAN, confirm_each.status)
            self.assertEqual("confirmation_required", confirm_each.reason_code)
            self.assertEqual(AdmissionStatus.ASK_HUMAN, out_of_scope.status)
            self.assertEqual("outside_preauthorized_scope", out_of_scope.reason_code)
            store.close()

    def test_followup_counting_scope_is_configuration_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain_service, chain_store = self.make_service(root / "chain.sqlite3")
            sender_service, sender_store = self.make_service(root / "sender.sqlite3")
            chain_policy = profile(followup_limit=1, followup_scope=FollowupScope.CHAIN)
            sender_policy = profile(followup_limit=1, followup_scope=FollowupScope.SENDER)
            second_chain = message(
                request_id="req-2",
                nonce="nonce-2",
                chain_id="chain-2",
                conversation_id="conv-2",
            )

            chain_first = chain_service.admit(message(), recipient=chain_policy, authenticated_sender_id="agent-a")
            chain_second = chain_service.admit(second_chain, recipient=chain_policy, authenticated_sender_id="agent-a")
            sender_first = sender_service.admit(message(), recipient=sender_policy, authenticated_sender_id="agent-a")
            sender_second = sender_service.admit(second_chain, recipient=sender_policy, authenticated_sender_id="agent-a")

            self.assertEqual(AdmissionStatus.ACCEPT, chain_first.status)
            self.assertEqual(AdmissionStatus.ACCEPT, chain_second.status)
            self.assertEqual(AdmissionStatus.ACCEPT, sender_first.status)
            self.assertEqual("followup_limit_reached", sender_second.reason_code)
            chain_store.close()
            sender_store.close()


if __name__ == "__main__":
    unittest.main()

