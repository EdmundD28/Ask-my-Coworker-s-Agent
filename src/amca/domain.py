from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AnswerStatus(str, Enum):
    ANSWER = "answer"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    REFUSE = "refuse"


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    owner_id: str
    display_name: str
    owner_claim: str
    claim_verified: bool = False


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    owner_agent_id: str
    topic: str
    claim_key: str
    content: str
    source_label: str
    version: str
    updated_at: datetime
    allowed_recipient_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class QuestionRequest:
    request_id: str
    task_id: str
    conversation_id: str
    asker: AgentIdentity
    responder: AgentIdentity
    question: str
    topic: str
    created_at: datetime


@dataclass(frozen=True)
class ConflictItem:
    evidence_id: str
    content: str
    source_label: str
    version: str


@dataclass(frozen=True)
class AnswerResult:
    status: AnswerStatus
    answer: str | None
    reason: str
    generated_at: datetime
    evidence_refs: tuple[str, ...] = ()
    conflict_items: tuple[ConflictItem, ...] = ()
    asker: AgentIdentity | None = None
    responder: AgentIdentity | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class PublicationDecision:
    allowed: bool
    required_owner_ids: frozenset[str] = frozenset()


@dataclass
class TaskSnapshot:
    task_id: str
    participant_ids: tuple[str, ...]
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)
