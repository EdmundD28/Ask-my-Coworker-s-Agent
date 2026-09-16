from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .transport import AcnNetworkAdapter, UrllibJsonHttpClient


INVITE_SCHEMA = "amca.invite.v1"


def _encode(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    if not isinstance(decoded, dict) or decoded.get("schema") != INVITE_SCHEMA:
        raise ValueError("unsupported AMCA invite code")
    return decoded


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InviteReceipt:
    invite_code: str
    expires_at: str


class SqliteInviteStore:
    def __init__(self, database: str | Path):
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS onboarding_invites (
                token_hash TEXT PRIMARY KEY,
                subnet_slug TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                invitee_agent_id TEXT
            )"""
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create(self, token: str, subnet_slug: str, expires_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO onboarding_invites VALUES (?, ?, ?, 'ready', NULL)",
            (_token_hash(token), subnet_slug, expires_at.isoformat()),
        )
        self.connection.commit()

    def claim(self, token: str, invitee_agent_id: str, now: datetime) -> str:
        digest = _token_hash(token)
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT * FROM onboarding_invites WHERE token_hash=?", (digest,)
        ).fetchone()
        if row is None:
            self.connection.rollback()
            raise PermissionError("invalid invite")
        if row["status"] != "ready":
            self.connection.rollback()
            raise PermissionError("invite already used")
        if datetime.fromisoformat(row["expires_at"]) <= now:
            self.connection.execute(
                "UPDATE onboarding_invites SET status='expired' WHERE token_hash=?", (digest,)
            )
            self.connection.commit()
            raise PermissionError("invite expired")
        self.connection.execute(
            "UPDATE onboarding_invites SET status='claiming', invitee_agent_id=? WHERE token_hash=?",
            (invitee_agent_id, digest),
        )
        self.connection.commit()
        return str(row["subnet_slug"])

    def finish(self, token: str, *, success: bool) -> None:
        self.connection.execute(
            "UPDATE onboarding_invites SET status=? WHERE token_hash=? AND status='claiming'",
            ("used" if success else "ready", _token_hash(token)),
        )
        self.connection.commit()


class OwnerOnboardingService:
    def __init__(self, store: SqliteInviteStore, adapter: AcnNetworkAdapter, *, clock=None):
        self.store = store
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(UTC))

    def create_invite(
        self,
        *,
        join_url: str,
        subnet_slug: str,
        lifetime: timedelta = timedelta(hours=24),
    ) -> InviteReceipt:
        parsed = urlparse(join_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("public join URL must use HTTPS")
        if lifetime <= timedelta(0):
            raise ValueError("invite lifetime must be positive")
        token = secrets.token_urlsafe(32)
        expires_at = self.clock() + lifetime
        self.store.create(token, subnet_slug, expires_at)
        return InviteReceipt(
            invite_code=_encode(
                {"schema": INVITE_SCHEMA, "join_url": join_url, "token": token}
            ),
            expires_at=expires_at.isoformat(),
        )

    def admit(self, token: str, invitee_agent_id: str) -> dict:
        subnet_slug = self.store.claim(token, invitee_agent_id, self.clock())
        try:
            invitation = self.adapter.invite(
                subnet_slug, invitee_agent_id, note="AMCA one-time owner invitation"
            )
        except Exception:
            self.store.finish(token, success=False)
            raise
        self.store.finish(token, success=True)
        return {
            "schema": "amca.join-receipt.v1",
            "subnet_slug": subnet_slug,
            "invitation_id": invitation["invitation_id"],
        }


def join_from_invite(
    invite_code: str,
    *,
    adapter: AcnNetworkAdapter,
    registration,
    http=None,
    existing_identity=None,
    on_registered=None,
    expected_subnet=None,
) -> tuple[object, dict]:
    invitation = _decode(invite_code)
    identity = existing_identity or adapter.register_agent(registration)
    if existing_identity is None and on_registered is not None:
        on_registered(identity)
    client = http or UrllibJsonHttpClient()
    receipt = client.request_json(
        "POST",
        invitation["join_url"],
        payload={"token": invitation["token"], "agent_id": identity.agent_id},
    )
    if expected_subnet is not None and receipt.get("subnet_slug") != expected_subnet:
        raise PermissionError("邀请返回的空间与接入码目标不一致，未接受邀请")
    adapter.accept_invitation(receipt["subnet_slug"], receipt["invitation_id"])
    members = adapter.discover(receipt["subnet_slug"])
    if identity.agent_id not in {item.get("agent_id") for item in members}:
        raise RuntimeError("ACN did not confirm invitee membership")
    return identity, receipt
