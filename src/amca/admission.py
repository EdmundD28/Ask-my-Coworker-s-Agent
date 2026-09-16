from __future__ import annotations

from datetime import datetime, timedelta

from .directory import ConfirmationMode
from .protocol import AdmissionDecision, AdmissionStatus, QuestionMessageV1, TriggerOrigin


class QuestionAdmissionService:
    def __init__(self, *, store, clock, max_age: timedelta):
        self.store, self.clock, self.max_age = store, clock, max_age

    def _reject(self, message, status, code):
        decision = AdmissionDecision(status, code)
        self.store.record_event(message, decision)
        return decision

    def admit(self, message: QuestionMessageV1, *, recipient, authenticated_sender_id: str):
        if message.trigger_origin is TriggerOrigin.AGENT_AUTOMATIC:
            return self._reject(message, AdmissionStatus.REJECT, "automatic_trigger_not_allowed_v1")
        if authenticated_sender_id != message.from_agent:
            return self._reject(message, AdmissionStatus.REJECT, "sender_mismatch")
        if recipient.card.agent_id != message.target_agent:
            return self._reject(message, AdmissionStatus.REJECT, "target_mismatch")
        if self.clock() - message.created_at > self.max_age:
            return self._reject(message, AdmissionStatus.REJECT, "stale_question")
        if self.store.has_request(message.request_id):
            return self._reject(message, AdmissionStatus.REJECT, "request_replay")
        if self.store.has_nonce(message.nonce):
            return self._reject(message, AdmissionStatus.REJECT, "nonce_replay")
        policy = recipient.inbound_policy
        if message.from_agent not in policy.allowed_asker_ids:
            return self._reject(message, AdmissionStatus.ASK_HUMAN, "outside_preauthorized_scope")
        if message.topic not in policy.allowed_topics or message.project_scope not in policy.allowed_project_scopes:
            return self._reject(message, AdmissionStatus.ASK_HUMAN, "outside_preauthorized_scope")
        if policy.confirmation_mode is ConfirmationMode.CONFIRM_EACH:
            return self._reject(message, AdmissionStatus.ASK_HUMAN, "confirmation_required")
        count = self.store.count(message, policy.followup_scope)
        if policy.followup_limit is not None and count >= policy.followup_limit:
            return self._reject(message, AdmissionStatus.REJECT, "followup_limit_reached")
        self.store.reserve(message)
        decision = AdmissionDecision(AdmissionStatus.ACCEPT, "accepted")
        self.store.record_event(message, decision)
        return decision
