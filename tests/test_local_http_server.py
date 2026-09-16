from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from tests import _bootstrap  # noqa: F401

from amca.application import CollaborationService
from amca.domain import AgentIdentity, EvidenceRecord, QuestionRequest
from amca.infrastructure.memory import MemoryEvidenceStore, MemoryNotifier
from amca.infrastructure.sqlite import SqliteAuditStore, SqliteTaskStore
from amca.monitor import create_monitor_app
from amca.runtime import DeterministicRuntime
from amca.server import build_server


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class LocalHttpServerTests(unittest.TestCase):
    def test_real_http_server_exposes_health_html_and_body_free_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "coordinator.sqlite3"
            evidence = MemoryEvidenceStore()
            evidence.add(
                EvidenceRecord(
                    evidence_id="ev-private",
                    owner_agent_id="agent-b",
                    topic="project.deadline",
                    claim_key="deadline",
                    content="PRIVATE BODY Friday",
                    source_label="Bob private notes",
                    version="v1",
                    updated_at=NOW,
                    allowed_recipient_ids=frozenset({"agent-a"}),
                )
            )
            audit = SqliteAuditStore(database)
            tasks = SqliteTaskStore(database)
            service = CollaborationService(
                evidence=evidence,
                runtime=DeterministicRuntime(answer="Friday"),
                audit=audit,
                tasks=tasks,
                notifier=MemoryNotifier(),
                clock=lambda: NOW,
            )
            service.respond(
                QuestionRequest(
                    request_id="req-http",
                    task_id="task-http",
                    conversation_id="conv-http",
                    asker=AgentIdentity("agent-a", "owner-a", "A", "Alice"),
                    responder=AgentIdentity("agent-b", "owner-b", "B", "Bob"),
                    question="PRIVATE QUESTION",
                    topic="project.deadline",
                    created_at=NOW,
                )
            )
            app = create_monitor_app(service)
            server = build_server(app, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/health", timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                with urlopen(base + "/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                with urlopen(base + "/api/tasks", timeout=5) as response:
                    task_body = response.read().decode("utf-8")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                audit.close()
                tasks.close()

            self.assertEqual("ok", health["status"])
            self.assertIn("AMCA", html)
            self.assertIn("task-http", task_body)
            self.assertIn("agent-a", task_body)
            self.assertIn("agent-b", task_body)
            for forbidden in ("PRIVATE QUESTION", "PRIVATE BODY Friday", "Bob private notes"):
                self.assertNotIn(forbidden, task_body)
                self.assertNotIn(forbidden, html)


if __name__ == "__main__":
    unittest.main()
