"""One local tool session per space, with an observed receive/echo handshake."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AgentBinding:
    def __init__(self, data):
        self.path = Path(data) / "binding.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL)")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _get(self, db):
        row = db.execute("SELECT body FROM binding WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, db, state):
        db.execute("INSERT OR REPLACE INTO binding VALUES (1, ?)", (json.dumps(state),))

    def bind(self, label, *, binding_id=None, secret=None):
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("请为当前智能体对话提供简短名称")
        secret = secret or secrets.token_urlsafe(32)
        state = {"binding_id": binding_id or secrets.token_hex(16), "label": label,
                 "secret_hash": digest(secret), "verified": False, "agent_id": None, "space_id": None,
                 "last_poll_at": None, "challenge": None, "challenge_read": False}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save(db, state)
        return {"binding_id": state["binding_id"], "secret": secret}

    def activate(self, agent_id, space_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            if state is None:
                return
            if state["agent_id"] not in (None, agent_id) or state["space_id"] not in (None, space_id):
                raise PermissionError("绑定目标不匹配，不能把当前对话接入其他身份或空间")
            state.update(agent_id=agent_id, space_id=space_id)
            self._save(db, state)

    def authorized(self, secret, *, verified=False):
        with self.db() as db:
            state = self._get(db)
        if not state or not secrets.compare_digest(state["secret_hash"], digest(secret)):
            raise PermissionError("智能体会话凭证无效或已被更换")
        if verified and not state["verified"]:
            raise PermissionError("请先收取并回复接入挑战")
        return state

    def poll(self, secret):
        self.authorized(secret)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            if not secrets.compare_digest(state["secret_hash"], digest(secret)):
                raise PermissionError("智能体会话已更换")
            if not state["space_id"]:
                raise ValueError("通讯节点尚未接入空间")
            state["last_poll_at"] = time.time()
            result = None
            if not state["verified"]:
                if not state["challenge"] or state["challenge"]["expires_at"] <= time.time():
                    state["challenge"] = {"kind": "connection_challenge", "challenge_id": secrets.token_hex(16),
                                          "nonce": secrets.token_urlsafe(32), "expires_at": time.time() + 300,
                                          "agent_id": state["agent_id"], "space_id": state["space_id"]}
                state["challenge_read"] = True
                result = state["challenge"]
            self._save(db, state)
        return result

    def acknowledge(self, secret, challenge_id, nonce):
        self.authorized(secret)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            challenge = state["challenge"]
            if not secrets.compare_digest(state["secret_hash"], digest(secret)):
                raise PermissionError("智能体会话已更换")
            if state["verified"] or not state["challenge_read"] or not challenge or challenge["expires_at"] <= time.time():
                raise PermissionError("接入挑战未收取、已过期或已使用")
            if challenge_id != challenge["challenge_id"] or not secrets.compare_digest(nonce, challenge["nonce"]):
                raise PermissionError("接入挑战回复不匹配")
            state.update(verified=True, verified_at=time.time(), challenge=None, challenge_read=False)
            self._save(db, state)
        return self.status()

    def status(self):
        with self.db() as db:
            state = self._get(db)
        if not state:
            return {"session_bound": False, "channel_verified": False, "receive_mode": "manual_poll"}
        return {"session_bound": True, "channel_verified": state["verified"], "binding_id": state["binding_id"],
                "session_label": state["label"], "agent_id": state["agent_id"], "space_id": state["space_id"],
                "last_poll_at": state["last_poll_at"], "receive_mode": "manual_poll",
                "provider_conversation_identity": "not_attested", "can_wake_closed_conversation": False}
