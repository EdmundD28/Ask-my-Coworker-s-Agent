from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .domain import AuditEvent


class GovernanceError(ValueError): pass
class Role(str, Enum): OWNER = "owner"; MEMBER = "member"; ADMIN = "admin"


@dataclass
class CollaborationSpace:
    space_id: str
    audit: object
    clock: object
    members: dict[str, Role]
    pending_owner: str | None = None

    def to_record(self) -> dict:
        """Return the stable, private-state-free representation used by persistence."""
        return {
            "space_id": self.space_id,
            "members": {user_id: role.value for user_id, role in self.members.items()},
            "pending_owner": self.pending_owner,
        }

    @classmethod
    def from_record(cls, record: dict, *, audit, clock):
        members = {user_id: Role(role) for user_id, role in record["members"].items()}
        return cls(record["space_id"], audit, clock, members, record.get("pending_owner"))

    @classmethod
    def create(cls, *, space_id, creator_user_id, audit, clock=lambda: datetime.now(timezone.utc)):
        return cls(space_id, audit, clock, {creator_user_id: Role.OWNER})

    def add_member(self, *, actor_user_id: str, member_user_id: str) -> None:
        if self.role_of(actor_user_id) not in (Role.OWNER, Role.ADMIN): raise GovernanceError("only owner or admin may add members")
        self.members.setdefault(member_user_id, Role.MEMBER)

    def role_of(self, user_id: str) -> Role | None: return self.members.get(user_id)

    def propose_owner_transfer(self, *, actor_user_id: str, target_user_id: str) -> None:
        if self.role_of(actor_user_id) != Role.OWNER: raise GovernanceError("only owner may transfer")
        if target_user_id not in self.members: raise GovernanceError("target must already be a member")
        self.pending_owner = target_user_id

    def accept_owner_transfer(self, *, actor_user_id: str) -> None:
        if self.pending_owner != actor_user_id: raise GovernanceError("no pending transfer for this member")
        previous = next(k for k, v in self.members.items() if v == Role.OWNER)
        self.members[previous] = Role.MEMBER
        self.members[actor_user_id] = Role.OWNER
        self.pending_owner = None
        self.audit.append(AuditEvent("owner_transferred", self.clock(), {"previous_owner": previous, "new_owner": actor_user_id, "space_id": self.space_id}))
