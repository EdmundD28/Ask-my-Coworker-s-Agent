from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .application import CollaborationService
from .domain import AgentIdentity, QuestionRequest
from .infrastructure.memory import MemoryNotifier
from .infrastructure.sqlite import SqliteAuditStore, SqliteEvidenceStore, SqliteTaskStore
from .runtime import DeterministicRuntime


def _identity(data: dict) -> AgentIdentity:
    return AgentIdentity(data["agent_id"], data["owner_id"], data.get("display_name", data["agent_id"]), data.get("owner_claim", ""), bool(data.get("claim_verified", False)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AMCA private agent process")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--owner-claim", default="")
    parser.add_argument("--private-db", required=True)
    parser.add_argument("--coordinator-db", required=True)
    parser.add_argument("--deterministic-answer")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("only --once mode is supported")
    line = sys.stdin.readline()
    try:
        payload = json.loads(line)
        if payload.get("target_agent_id") != args.agent_id:
            print(json.dumps({"error": "target_agent_mismatch"}), flush=True)
            return 2
        asker = _identity(payload["asker"])
        responder = AgentIdentity(args.agent_id, args.owner_id, payload.get("responder_display_name", args.agent_id), args.owner_claim)
        request = QuestionRequest(
            payload["request_id"], payload["task_id"], payload["conversation_id"], asker, responder,
            payload["question"], payload["topic"], datetime.fromisoformat(payload["created_at"]),
        )
        evidence = SqliteEvidenceStore(args.private_db, owner_agent_id=args.agent_id)
        audit = SqliteAuditStore(args.coordinator_db)
        tasks = SqliteTaskStore(args.coordinator_db)
        runtime = DeterministicRuntime(args.deterministic_answer or "")
        service = CollaborationService(evidence=evidence, runtime=runtime, audit=audit, tasks=tasks,
                                       notifier=MemoryNotifier(), clock=lambda: datetime.now(timezone.utc))
        result = service.respond(request)
        output = {"status": result.status.value, "answer": result.answer, "reason": result.reason,
                  "generated_at": result.generated_at.isoformat(), "evidence_refs": result.evidence_refs}
        print(json.dumps(output, ensure_ascii=False), flush=True)
        evidence.close(); audit.close(); tasks.close()
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
