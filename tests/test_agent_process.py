from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from tests._bootstrap import SRC

from amca.domain import EvidenceRecord
from amca.infrastructure.sqlite import SqliteEvidenceStore


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
ROOT = SRC.parent


def run_agent(
    *,
    agent_id: str,
    owner_id: str,
    private_db: Path,
    coordinator_db: Path,
    target_agent_id: str,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "request_id": f"req-{agent_id}",
        "task_id": f"task-{agent_id}",
        "conversation_id": f"conv-{agent_id}",
        "target_agent_id": target_agent_id,
        "asker": {
            "agent_id": "agent-a",
            "owner_id": "owner-a",
            "display_name": "A",
            "owner_claim": "Alice",
        },
        "question": "When is the deadline?",
        "topic": "project.deadline",
        "created_at": NOW.isoformat(),
    }
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "amca.agent_process",
            "--agent-id",
            agent_id,
            "--owner-id",
            owner_id,
            "--owner-claim",
            owner_id,
            "--private-db",
            str(private_db),
            "--coordinator-db",
            str(coordinator_db),
            "--deterministic-answer",
            "Friday",
            "--once",
        ],
        input=json.dumps(payload) + "\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        timeout=15,
        check=False,
    )


class IndependentAgentProcessTests(unittest.TestCase):
    def test_two_processes_use_separate_private_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a_private = root / "alice" / "private.sqlite3"
            b_private = root / "bob" / "private.sqlite3"
            coordinator = root / "coordinator.sqlite3"
            b_store = SqliteEvidenceStore(b_private, owner_agent_id="agent-b")
            b_store.add(
                EvidenceRecord(
                    evidence_id="ev-b",
                    owner_agent_id="agent-b",
                    topic="project.deadline",
                    claim_key="deadline",
                    content="Friday",
                    source_label="Bob private notes",
                    version="v1",
                    updated_at=NOW,
                    allowed_recipient_ids=frozenset({"agent-a"}),
                )
            )
            b_store.close()

            b_result = run_agent(
                agent_id="agent-b",
                owner_id="owner-b",
                private_db=b_private,
                coordinator_db=coordinator,
                target_agent_id="agent-b",
            )
            a_result = run_agent(
                agent_id="agent-a-local",
                owner_id="owner-a",
                private_db=a_private,
                coordinator_db=coordinator,
                target_agent_id="agent-a-local",
            )

            self.assertEqual(0, b_result.returncode, b_result.stderr)
            self.assertEqual(0, a_result.returncode, a_result.stderr)
            self.assertEqual("answer", json.loads(b_result.stdout)["status"])
            self.assertEqual("unknown", json.loads(a_result.stdout)["status"])

    def test_process_rejects_question_targeted_at_another_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_agent(
                agent_id="agent-b",
                owner_id="owner-b",
                private_db=root / "bob.sqlite3",
                coordinator_db=root / "coordinator.sqlite3",
                target_agent_id="agent-c",
            )

            self.assertEqual(2, result.returncode)
            error = json.loads(result.stdout)
            self.assertEqual("target_agent_mismatch", error["error"])


if __name__ == "__main__":
    unittest.main()

