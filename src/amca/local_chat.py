from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .protocol import QuestionMessageV1, TriggerOrigin
from .transport import AcnNetworkAdapter, MessageEnvelope


MAX_QUESTION_CHARS = 10_000


def bilateral_conversation_id(first_agent_id: str, second_agent_id: str) -> str:
    participants = sorted((first_agent_id, second_agent_id))
    digest = hashlib.sha256(
        f"amca.bilateral.v1|{participants[0]}|{participants[1]}".encode("utf-8")
    ).hexdigest()[:24]
    return f"bilateral-{digest}"


@dataclass(frozen=True)
class LocalMessage:
    message_id: str
    conversation_id: str
    request_id: str
    sender_agent_id: str
    recipient_agent_id: str
    kind: str
    content: str | None
    status: str
    reason: str | None
    created_at: str


class LocalConversationStore:
    """A participant-scoped message store that never acts as shared coordination state."""

    def __init__(self, database: str | Path, *, local_agent_id: str):
        self.local_agent_id = local_agent_id
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS local_conversations (
                    local_agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    participant_ids TEXT NOT NULL,
                    counterpart_agent_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (local_agent_id, conversation_id)
                )"""
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS local_messages (
                    local_agent_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    sender_agent_id TEXT NOT NULL,
                    recipient_agent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (local_agent_id, message_id)
                )"""
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _participants(self, peer_agent_id: str) -> tuple[str, str]:
        if not peer_agent_id or peer_agent_id == self.local_agent_id:
            raise ValueError("a bilateral conversation requires another participant")
        return tuple(sorted((self.local_agent_id, peer_agent_id)))

    def _upsert_conversation(
        self, conversation_id: str, peer_agent_id: str, occurred_at: str
    ) -> None:
        participants = self._participants(peer_agent_id)
        expected = bilateral_conversation_id(*participants)
        if conversation_id != expected:
            raise PermissionError("conversation id does not match its participants")
        self.connection.execute(
            """INSERT INTO local_conversations
               (local_agent_id, conversation_id, participant_ids,
                counterpart_agent_id, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(local_agent_id, conversation_id) DO UPDATE SET
                   updated_at=excluded.updated_at""",
            (
                self.local_agent_id,
                conversation_id,
                json.dumps(participants, separators=(",", ":")),
                peer_agent_id,
                occurred_at,
            ),
        )

    def record_question(
        self,
        message: QuestionMessageV1,
        *,
        status: str,
    ) -> None:
        if self.local_agent_id not in {message.from_agent, message.target_agent}:
            raise PermissionError("local agent is not a participant in this question")
        peer = (
            message.target_agent
            if message.from_agent == self.local_agent_id
            else message.from_agent
        )
        with self._lock:
            self._upsert_conversation(message.conversation_id, peer, message.created_at.isoformat())
            self.connection.execute(
                """INSERT OR REPLACE INTO local_messages VALUES
                   (?, ?, ?, ?, ?, ?, 'question', ?, ?, NULL, ?)""",
                (
                    self.local_agent_id,
                    message.request_id,
                    message.conversation_id,
                    message.request_id,
                    message.from_agent,
                    message.target_agent,
                    message.question,
                    status,
                    message.created_at.isoformat(),
                ),
            )
            self.connection.commit()

    def update_question_status(self, request_id: str, status: str, reason: str | None = None) -> None:
        with self._lock:
            self.connection.execute(
                """UPDATE local_messages SET status=?, reason=?
                   WHERE local_agent_id=? AND message_id=? AND kind='question'""",
                (status, reason, self.local_agent_id, request_id),
            )
            self.connection.commit()

    def record_answer(
        self,
        *,
        conversation_id: str,
        request_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        status: str,
        content: str | None,
        reason: str | None,
        created_at: str,
    ) -> None:
        if self.local_agent_id not in {sender_agent_id, recipient_agent_id}:
            raise PermissionError("local agent is not a participant in this answer")
        peer = (
            recipient_agent_id
            if sender_agent_id == self.local_agent_id
            else sender_agent_id
        )
        with self._lock:
            self._upsert_conversation(conversation_id, peer, created_at)
            self.connection.execute(
                """INSERT OR REPLACE INTO local_messages VALUES
                   (?, ?, ?, ?, ?, ?, 'answer', ?, ?, ?, ?)""",
                (
                    self.local_agent_id,
                    f"answer:{request_id}",
                    conversation_id,
                    request_id,
                    sender_agent_id,
                    recipient_agent_id,
                    content,
                    status,
                    reason,
                    created_at,
                ),
            )
            self.connection.commit()

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT conversation_id, participant_ids, counterpart_agent_id, updated_at
                   FROM local_conversations WHERE local_agent_id=?
                   ORDER BY updated_at DESC""",
                (self.local_agent_id,),
            ).fetchall()
        return [
            {
                "conversation_id": row["conversation_id"],
                "participant_ids": json.loads(row["participant_ids"]),
                "counterpart_agent_id": row["counterpart_agent_id"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            visible = self.connection.execute(
                """SELECT 1 FROM local_conversations
                   WHERE local_agent_id=? AND conversation_id=?""",
                (self.local_agent_id, conversation_id),
            ).fetchone()
            if visible is None:
                raise PermissionError("conversation is not visible to this participant")
            rows = self.connection.execute(
                """SELECT message_id, conversation_id, request_id, sender_agent_id,
                          recipient_agent_id, kind, content, status, reason, created_at
                   FROM local_messages
                   WHERE local_agent_id=? AND conversation_id=?
                   ORDER BY created_at,
                            CASE kind WHEN 'question' THEN 0 ELSE 1 END,
                            message_id""",
                (self.local_agent_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]


def _answer_data(response: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    candidate: Any = response
    if isinstance(candidate.get("response"), dict):
        candidate = candidate["response"]
    if isinstance(candidate.get("result"), dict):
        candidate = candidate["result"]
    if isinstance(candidate, dict) and isinstance(candidate.get("root"), dict):
        candidate = candidate["root"]
    if not isinstance(candidate, dict):
        return None
    parts = candidate.get("parts")
    if not isinstance(parts, list):
        message = candidate.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data":
            data = part.get("data")
            if isinstance(data, dict) and data.get("schema") in {
                "amca.answer.v1",
                "amca.admission.v1",
            }:
                return data
    return None


class LocalChatService:
    def __init__(
        self,
        *,
        local_agent_id: str,
        subnet_slug: str,
        adapter: AcnNetworkAdapter,
        store: LocalConversationStore,
        clock=lambda: datetime.now(UTC),
    ):
        self.local_agent_id = local_agent_id
        self.subnet_slug = subnet_slug
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.prepare_question = None

    def close(self) -> None:
        self.store.close()

    def members(self) -> list[dict[str, str]]:
        members = self.adapter.discover(self.subnet_slug)
        result = []
        for member in members:
            if not isinstance(member, dict):
                continue
            agent_id = member.get("agent_id")
            if not isinstance(agent_id, str) or agent_id == self.local_agent_id:
                continue
            result.append(
                {
                    "agent_id": agent_id,
                    "display_name": str(member.get("name") or member.get("display_name") or agent_id),
                    "description": str(member.get("description") or ""),
                }
            )
        return sorted(result, key=lambda item: (item["display_name"], item["agent_id"]))

    def send_question(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("target_agent")
        question_text = payload.get("question")
        if not isinstance(target, str) or not target:
            raise ValueError("target_agent is required")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError("question is required")
        question_text = question_text.strip()
        if len(question_text) > MAX_QUESTION_CHARS:
            raise ValueError("question is too long")
        allowed_targets = {member["agent_id"] for member in self.members()}
        if target not in allowed_targets:
            raise PermissionError("target is not another member of this private space")

        now = self.clock()
        request_id = f"req-{uuid4().hex}"
        conversation_id = bilateral_conversation_id(self.local_agent_id, target)
        question = QuestionMessageV1(
            schema="amca.question.v1",
            request_id=request_id,
            task_id=f"task-{uuid4().hex}",
            conversation_id=conversation_id,
            chain_id=conversation_id,
            parent_request_id=None,
            from_agent=self.local_agent_id,
            target_agent=target,
            project_scope=str(payload.get("project_scope") or "general"),
            question=question_text,
            topic=str(payload.get("topic") or "general"),
            trigger_origin=TriggerOrigin.OWNER_EXPLICIT,
            created_at=now,
            nonce=uuid4().hex,
        )
        self.store.record_question(question, status="sending")
        try:
            body = self.prepare_question(question) if self.prepare_question else question.to_payload()
            receipt = self.adapter.send(
                MessageEnvelope(
                    message_id=request_id,
                    from_agent=self.local_agent_id,
                    target_agent=target,
                    message_type="amca.question.v1",
                    body=body,
                )
            )
        except Exception as exc:
            self.store.update_question_status(request_id, "failed", type(exc).__name__)
            raise

        remote_error = (receipt.response or {}).get("error")
        if isinstance(remote_error, dict):
            reason = str(remote_error.get("message") or "对方拒绝处理此问题")[:1000]
            self.store.update_question_status(request_id, "rejected", reason)
            return {"conversation_id": conversation_id, "request_id": request_id,
                    "delivery_status": "rejected", "delivery_mode": receipt.delivery_mode,
                    "answer": None, "reason": reason}
        self.store.update_question_status(request_id, receipt.status)
        answer = _answer_data(receipt.response)
        if answer is not None:
            generated_at = str(answer.get("generated_at") or self.clock().isoformat())
            self.store.record_answer(
                conversation_id=conversation_id,
                request_id=request_id,
                sender_agent_id=target,
                recipient_agent_id=self.local_agent_id,
                status=str(answer.get("status") or "unknown"),
                content=(None if answer.get("answer") is None else str(answer["answer"])),
                reason=(None if answer.get("reason") is None else str(answer["reason"])),
                created_at=generated_at,
            )
        return {
            "conversation_id": conversation_id,
            "request_id": request_id,
            "delivery_status": receipt.status,
            "delivery_mode": receipt.delivery_mode,
            "answer": answer,
        }

    def record_incoming_question(self, message: QuestionMessageV1, *, status: str) -> None:
        self.store.record_question(message, status=status)

    def record_local_answer(
        self, message: QuestionMessageV1, answer: dict[str, Any]
    ) -> None:
        self.store.update_question_status(message.request_id, "received")
        self.store.record_answer(
            conversation_id=message.conversation_id,
            request_id=message.request_id,
            sender_agent_id=self.local_agent_id,
            recipient_agent_id=message.from_agent,
            status=str(answer.get("status") or "unknown"),
            content=(None if answer.get("answer") is None else str(answer["answer"])),
            reason=(None if answer.get("reason") is None else str(answer["reason"])),
            created_at=str(answer.get("generated_at") or self.clock().isoformat()),
        )
