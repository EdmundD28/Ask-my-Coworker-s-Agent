from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .domain import EvidenceRecord, QuestionRequest


@dataclass(frozen=True)
class RuntimeCall:
    request_id: str
    question: str
    evidence_ids: tuple[str, ...]


class AnswerRuntime(Protocol):
    def answer(self, request: QuestionRequest, evidence: Sequence[EvidenceRecord]) -> str: ...


class DeterministicRuntime:
    def __init__(self, answer: str):
        self.answer_text = answer
        self.calls: list[RuntimeCall] = []

    def answer(self, request: QuestionRequest, evidence: Sequence[EvidenceRecord]) -> str:
        self.calls.append(RuntimeCall(request.request_id, request.question, tuple(x.evidence_id for x in evidence)))
        return self.answer_text
