from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConfirmationMode(str, Enum):
    CONFIRM_EACH = "confirm_each"
    PREAUTHORIZED = "preauthorized"


class FollowupScope(str, Enum):
    CHAIN = "chain"
    SENDER = "sender"


@dataclass(frozen=True)
class MemberCard:
    agent_id: str; role_name: str; description: str; owner_claim: str; claim_verified: bool = False

    def to_public(self) -> dict:
        return {"agent_id": self.agent_id, "role_name": self.role_name, "description": self.description,
                "owner_claim": self.owner_claim, "claim_verified": self.claim_verified}


@dataclass(frozen=True)
class InboundQuestionPolicy:
    confirmation_mode: ConfirmationMode
    allowed_asker_ids: frozenset[str]
    allowed_topics: frozenset[str]
    allowed_project_scopes: frozenset[str]
    followup_limit: int | None
    followup_scope: FollowupScope


@dataclass(frozen=True)
class MemberProfile:
    card: MemberCard
    inbound_policy: InboundQuestionPolicy
