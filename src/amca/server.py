from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .infrastructure.sqlite import SqliteAuditStore, SqliteTaskStore
from .monitor import create_monitor_app


def build_server(app, host: str = "127.0.0.1", port: int = 8000):
    return make_server(host, port, app)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="AMCA local monitor")
    parser.add_argument("--database", default="data/coordinator.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    audit = SqliteAuditStore(args.database)
    tasks = SqliteTaskStore(args.database)

    class Projection:
        def __init__(self):
            self.tasks = tasks

        def shared_task_snapshot(self, task_id):
            item = tasks.get(task_id)
            return {} if item is None else {
                "task_id": item.task_id, "participant_ids": item.participant_ids,
                "status": item.status, "updated_at": item.updated_at.isoformat(),
            }

    app = create_monitor_app(Projection())
    build_server(app, args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()
