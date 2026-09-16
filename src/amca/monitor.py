from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorResponse:
    status: int
    body: str


def create_monitor_app(service):
    class App:
        def handle(self, method: str, path: str) -> MonitorResponse:
            if method == "GET" and path == "/health": return MonitorResponse(200, json.dumps({"status": "ok"}))
            if method == "GET" and path == "/api/tasks": return MonitorResponse(200, json.dumps(list(service.shared_task_snapshot(t.task_id) for t in service.tasks.all()), default=str))
            if method == "GET" and path == "/":
                return MonitorResponse(200, "<!doctype html><html><head><title>AMCA</title></head><body><h1>AMCA monitor</h1></body></html>")
            return MonitorResponse(404, json.dumps({"error": "not found"}))

        def __call__(self, environ, start_response):
            response = self.handle(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/"))
            content_type = "text/html; charset=utf-8" if environ.get("PATH_INFO", "/") == "/" else "application/json; charset=utf-8"
            body = response.body.encode("utf-8")
            start_response(f"{response.status} {'OK' if response.status == 200 else 'Not Found'}",
                           [("Content-Type", content_type), ("Content-Length", str(len(body)))])
            return [body]
    return App()
