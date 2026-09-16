from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..domain import AuditEvent, EvidenceRecord, TaskSnapshot


class MemoryEvidenceStore:
    def __init__(self): self.records: list[EvidenceRecord] = []
    def add(self, record: EvidenceRecord) -> None: self.records.append(record)
    def find(self, owner_agent_id: str, topic: str) -> list[EvidenceRecord]:
        return [r for r in self.records if r.owner_agent_id == owner_agent_id and r.topic == topic]


class MemoryAuditStore:
    def __init__(self): self.events: list[AuditEvent] = []
    def append(self, event: AuditEvent) -> None: self.events.append(event)


class MemoryNotifier:
    @dataclass(frozen=True)
    class Notification:
        owner_id: str
        request_id: str
        reason: str
    def __init__(self): self.notifications: list[MemoryNotifier.Notification] = []
    def notify(self, owner_id: str, request_id: str, reason: str) -> None:
        self.notifications.append(self.Notification(owner_id, request_id, reason))


class MemoryTaskStore:
    def __init__(self): self.tasks: dict[str, TaskSnapshot] = {}
    def upsert(self, snapshot: TaskSnapshot) -> None: self.tasks[snapshot.task_id] = snapshot
    def get(self, task_id: str) -> TaskSnapshot | None: return self.tasks.get(task_id)
    def all(self) -> Iterable[TaskSnapshot]: return self.tasks.values()
