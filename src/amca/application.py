from __future__ import annotations

from datetime import datetime

from .domain import (AnswerResult, AnswerStatus, AuditEvent, ConflictItem,
                     PublicationDecision, QuestionRequest, TaskSnapshot)


class CollaborationService:
    def __init__(self, *, evidence, runtime, audit, tasks, notifier, clock):
        self.evidence, self.runtime, self.audit, self.tasks, self.notifier, self.clock = evidence, runtime, audit, tasks, notifier, clock

    def respond(self, request: QuestionRequest) -> AnswerResult:
        now = self.clock()
        records = self.evidence.find(request.responder.agent_id, request.topic)
        if not records:
            result = AnswerResult(AnswerStatus.UNKNOWN, None, "No matching evidence is available.", now, asker=request.asker, responder=request.responder, request_id=request.request_id)
        else:
            permitted = [r for r in records if request.asker.agent_id in r.allowed_recipient_ids]
            if len(permitted) != len(records):
                self.notifier.notify(request.responder.owner_id, request.request_id, "Evidence cannot be disclosed to this recipient.")
                result = AnswerResult(AnswerStatus.REFUSE, None, "Cannot answer because relevant evidence is not disclosable to the asker.", now, asker=request.asker, responder=request.responder, request_id=request.request_id)
            elif len({r.content for r in permitted}) > 1:
                items = tuple(ConflictItem(r.evidence_id, r.content, r.source_label, r.version) for r in permitted)
                self.notifier.notify(request.responder.owner_id, request.request_id, "Relevant evidence conflicts and needs owner review.")
                result = AnswerResult(AnswerStatus.CONFLICT, None, "Conflicting evidence was found; the responder owner was notified.", now, tuple(f"{r.evidence_id}:{r.version}" for r in permitted), items, request.asker, request.responder, request.request_id)
            else:
                answer = self.runtime.answer(request, permitted)
                refs = tuple(f"{r.evidence_id}:{r.version}" for r in permitted)
                result = AnswerResult(AnswerStatus.ANSWER, answer, f"Answer supported by evidence {', '.join(r.evidence_id for r in permitted)}.", now, refs, asker=request.asker, responder=request.responder, request_id=request.request_id)
        self.audit.append(AuditEvent("answer", now, {"request_id": request.request_id, "task_id": request.task_id, "status": result.status.value, "evidence_refs": result.evidence_refs}))
        self.tasks.upsert(TaskSnapshot(request.task_id, (request.asker.agent_id, request.responder.agent_id), result.status.value, now))
        return result

    def shared_task_snapshot(self, task_id: str) -> dict:
        item = self.tasks.get(task_id)
        return {} if item is None else {"task_id": item.task_id, "participant_ids": item.participant_ids, "status": item.status, "updated_at": item.updated_at.isoformat()}

    def authorize_publication(self, *, result: AnswerResult, recipient_ids: frozenset[str], approving_owner_ids: frozenset[str]) -> PublicationDecision:
        original = {result.asker.agent_id} if result.asker else set()
        if recipient_ids.issubset(original): return PublicationDecision(True)
        required = frozenset({result.responder.owner_id} if result.responder else set())
        return PublicationDecision(required.issubset(approving_owner_ids), required)
