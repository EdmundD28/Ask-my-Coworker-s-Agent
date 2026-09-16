from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..domain import AuditEvent, EvidenceRecord, TaskSnapshot


def _iso(value: datetime) -> str:
    return value.isoformat()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class _SqliteStore:
    def __init__(self, database: str | Path):
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()


class SqliteEvidenceStore(_SqliteStore):
    def __init__(self, database: str | Path, *, owner_agent_id: str):
        super().__init__(database)
        self.owner_agent_id = owner_agent_id
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, owner_agent_id TEXT NOT NULL,
                topic TEXT NOT NULL, claim_key TEXT NOT NULL, content TEXT NOT NULL,
                source_label TEXT NOT NULL, version TEXT NOT NULL, updated_at TEXT NOT NULL,
                allowed_recipient_ids TEXT NOT NULL)"""
        )
        self.connection.commit()

    def _check_owner(self, owner_agent_id: str) -> None:
        if owner_agent_id != self.owner_agent_id:
            raise PermissionError("private evidence store belongs to another agent")

    def add(self, record: EvidenceRecord) -> None:
        self._check_owner(record.owner_agent_id)
        self.connection.execute(
            "INSERT OR REPLACE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.evidence_id, record.owner_agent_id, record.topic, record.claim_key,
             record.content, record.source_label, record.version, _iso(record.updated_at),
             json.dumps(sorted(record.allowed_recipient_ids), ensure_ascii=False, separators=(",", ":"))),
        )
        self.connection.commit()

    def find(self, owner_agent_id: str, topic: str) -> list[EvidenceRecord]:
        self._check_owner(owner_agent_id)
        rows = self.connection.execute(
            "SELECT * FROM evidence WHERE owner_agent_id = ? AND topic = ? ORDER BY evidence_id",
            (owner_agent_id, topic),
        ).fetchall()
        return [EvidenceRecord(r["evidence_id"], r["owner_agent_id"], r["topic"], r["claim_key"],
                               r["content"], r["source_label"], r["version"], _time(r["updated_at"]),
                               frozenset(json.loads(r["allowed_recipient_ids"]))) for r in rows]


class SqliteAuditStore(_SqliteStore):
    def __init__(self, database: str | Path):
        super().__init__(database)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, details TEXT NOT NULL)"
        )
        self.connection.commit()

    def append(self, event: AuditEvent) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(event_type, occurred_at, details) VALUES (?, ?, ?)",
            (event.event_type, _iso(event.occurred_at), json.dumps(event.details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        self.connection.commit()

    @property
    def events(self) -> list[AuditEvent]:
        return [AuditEvent(r["event_type"], _time(r["occurred_at"]), json.loads(r["details"]))
                for r in self.connection.execute("SELECT event_type, occurred_at, details FROM audit_events ORDER BY id")]


class SqliteTaskStore(_SqliteStore):
    def __init__(self, database: str | Path):
        super().__init__(database)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, participant_ids TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.connection.commit()

    def upsert(self, snapshot: TaskSnapshot) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?)",
            (snapshot.task_id, json.dumps(list(snapshot.participant_ids), separators=(",", ":")), snapshot.status, _iso(snapshot.updated_at)),
        )
        self.connection.commit()

    def get(self, task_id: str) -> TaskSnapshot | None:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskSnapshot(row["task_id"], tuple(json.loads(row["participant_ids"])), row["status"], _time(row["updated_at"]))

    def all(self) -> Iterable[TaskSnapshot]:
        return [self.get(row["task_id"]) for row in self.connection.execute("SELECT task_id FROM tasks ORDER BY task_id")]


class SqliteGovernanceStore(_SqliteStore):
    def __init__(self, database: str | Path):
        super().__init__(database)
        self.connection.execute("CREATE TABLE IF NOT EXISTS governance_spaces (space_id TEXT PRIMARY KEY, members TEXT NOT NULL, pending_owner TEXT)")
        self.connection.commit()

    def save(self, space) -> None:
        record = space.to_record()
        self.connection.execute("INSERT OR REPLACE INTO governance_spaces VALUES (?, ?, ?)",
                                (record["space_id"], json.dumps(record["members"], sort_keys=True, separators=(",", ":")), record["pending_owner"]))
        self.connection.commit()

    def load(self, space_id: str, *, audit, clock):
        from ..governance import CollaborationSpace
        row = self.connection.execute("SELECT * FROM governance_spaces WHERE space_id = ?", (space_id,)).fetchone()
        if row is None:
            return None
        return CollaborationSpace.from_record({"space_id": row["space_id"], "members": json.loads(row["members"]), "pending_owner": row["pending_owner"]}, audit=audit, clock=clock)


class SqliteAdmissionStore(_SqliteStore):
    def __init__(self, database):
        super().__init__(database)
        self.connection.execute("CREATE TABLE IF NOT EXISTS admission_reservations (request_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, from_agent TEXT NOT NULL, chain_id TEXT NOT NULL, conversation_id TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS admission_events (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, nonce TEXT, trigger_origin TEXT, status TEXT, reason_code TEXT)")
        self.connection.commit()

    @property
    def events(self):
        return [dict(row) for row in self.connection.execute("SELECT request_id, nonce, trigger_origin, status, reason_code FROM admission_events ORDER BY id")]

    def has_request(self, request_id):
        return self.connection.execute("SELECT 1 FROM admission_reservations WHERE request_id=?", (request_id,)).fetchone() is not None

    def has_nonce(self, nonce):
        return self.connection.execute("SELECT 1 FROM admission_reservations WHERE nonce=?", (nonce,)).fetchone() is not None

    def reserve(self, message):
        self.connection.execute("INSERT INTO admission_reservations VALUES (?, ?, ?, ?, ?)", (message.request_id, message.nonce, message.from_agent, message.chain_id, message.conversation_id))
        self.connection.commit()

    def count(self, message, scope):
        column, value = ("chain_id", message.chain_id) if scope.value == "chain" else ("from_agent", message.from_agent)
        return self.connection.execute(f"SELECT COUNT(*) FROM admission_reservations WHERE {column}=?", (value,)).fetchone()[0]

    def record_event(self, message, decision):
        self.connection.execute("INSERT INTO admission_events VALUES (NULL, ?, ?, ?, ?, ?)", (message.request_id, message.nonce, message.trigger_origin.value, decision.status.value, decision.reason_code))
        self.connection.commit()


class SqliteMemberDirectory(_SqliteStore):
    def __init__(self, database):
        super().__init__(database)
        self.connection.execute("CREATE TABLE IF NOT EXISTS member_profiles (agent_id TEXT PRIMARY KEY, card TEXT NOT NULL, policy TEXT NOT NULL)")
        self.connection.commit()

    def register(self, profile):
        card = profile.card
        policy = profile.inbound_policy
        self.connection.execute("INSERT OR REPLACE INTO member_profiles VALUES (?, ?, ?)", (card.agent_id, json.dumps(card.to_public(), sort_keys=True), json.dumps({"confirmation_mode": policy.confirmation_mode.value, "allowed_asker_ids": sorted(policy.allowed_asker_ids), "allowed_topics": sorted(policy.allowed_topics), "allowed_project_scopes": sorted(policy.allowed_project_scopes), "followup_limit": policy.followup_limit, "followup_scope": policy.followup_scope.value}, sort_keys=True)))
        self.connection.commit()

    def get(self, agent_id):
        from ..directory import InboundQuestionPolicy, MemberCard, MemberProfile, ConfirmationMode, FollowupScope
        row = self.connection.execute("SELECT * FROM member_profiles WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None: return None
        card = json.loads(row["card"]); policy = json.loads(row["policy"])
        return MemberProfile(MemberCard(**card), InboundQuestionPolicy(ConfirmationMode(policy["confirmation_mode"]), frozenset(policy["allowed_asker_ids"]), frozenset(policy["allowed_topics"]), frozenset(policy["allowed_project_scopes"]), policy["followup_limit"], FollowupScope(policy["followup_scope"])))

    def public_cards(self):
        return [dict(json.loads(row["card"])) for row in self.connection.execute("SELECT card FROM member_profiles ORDER BY agent_id")]
