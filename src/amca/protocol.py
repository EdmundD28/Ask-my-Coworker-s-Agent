from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class TriggerOrigin(str, Enum):
    OWNER_EXPLICIT = "owner_explicit"
    AGENT_AUTOMATIC = "agent_automatic"


class AdmissionStatus(str, Enum):
    ACCEPT = "accept"
    ASK_HUMAN = "ask_human"
    REJECT = "reject"


@dataclass(frozen=True)
class QuestionMessageV1:
    schema: str; request_id: str; task_id: str; conversation_id: str; chain_id: str
    parent_request_id: str | None; from_agent: str; target_agent: str; project_scope: str
    question: str; topic: str; trigger_origin: TriggerOrigin; created_at: datetime; nonce: str

    def to_payload(self) -> dict:
        return {"schema": self.schema, "request_id": self.request_id, "task_id": self.task_id,
                "conversation_id": self.conversation_id, "chain_id": self.chain_id,
                "parent_request_id": self.parent_request_id, "from_agent": self.from_agent,
                "target_agent": self.target_agent, "project_scope": self.project_scope,
                "question": self.question, "topic": self.topic,
                "trigger_origin": self.trigger_origin.value, "created_at": self.created_at.isoformat(),
                "nonce": self.nonce}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "QuestionMessageV1":
        if payload.get("schema") != "amca.question.v1":
            raise ValueError("unsupported AMCA question schema")
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return cls(
            schema="amca.question.v1",
            request_id=str(payload["request_id"]),
            task_id=str(payload["task_id"]),
            conversation_id=str(payload["conversation_id"]),
            chain_id=str(payload["chain_id"]),
            parent_request_id=(
                None
                if payload.get("parent_request_id") is None
                else str(payload["parent_request_id"])
            ),
            from_agent=str(payload["from_agent"]),
            target_agent=str(payload["target_agent"]),
            project_scope=str(payload["project_scope"]),
            question=str(payload["question"]),
            topic=str(payload["topic"]),
            trigger_origin=TriggerOrigin(str(payload["trigger_origin"])),
            created_at=created_at,
            nonce=str(payload["nonce"]),
        )


@dataclass(frozen=True)
class AdmissionDecision:
    status: AdmissionStatus
    reason_code: str
