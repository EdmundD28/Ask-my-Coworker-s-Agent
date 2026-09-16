from __future__ import annotations

import json
import re
import base64
import secrets
import threading
import webbrowser
from pathlib import Path
from wsgiref.simple_server import make_server

from .entry_hub import EntryHub
from .setup_node import QuietHandler, local_call
from .credentials import _unprotect


def open_chat(data):
    current = local_call(data, "GET", "/api/setup/status")
    if not current.get("connected"):
        raise ValueError("请先连接此空间，再打开对话")
    session = json.loads(_unprotect(base64.b64decode((Path(data) / "session.dpapi").read_bytes())))
    port = session["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("本地会话端口无效")
    opened = webbrowser.open(f"http://127.0.0.1:{port}/#token={session['token']}", new=2)
    if not opened:
        raise RuntimeError("无法打开默认浏览器，请检查系统浏览器设置")
    return {"status": "chat_opened"}

from .unified_ui import PAGE



class HubApp:
    def __init__(self, root, token):
        self.hub, self.token = EntryHub(root), token
        self.shutdown = None

    def __call__(self, environ, start):
        code, content_type = "200 OK", "application/json; charset=utf-8"
        try:
            path = environ.get("PATH_INFO", "/")
            if path == "/" and environ.get("REQUEST_METHOD") == "GET":
                value = PAGE
                content_type = "text/html; charset=utf-8"
            else:
                if not secrets.compare_digest(self.token, environ.get("HTTP_X_HUB_TOKEN", "")):
                    raise PermissionError("请从本机程序打开此界面")
                origin = environ.get("HTTP_ORIGIN")
                if origin and origin != "http://" + environ.get("HTTP_HOST", ""):
                    raise PermissionError("不接受来自其他页面的操作")
                if path == "/api/hub/state" and environ.get("REQUEST_METHOD") == "GET":
                    value = self.hub.overview()
                elif path == "/api/hub/chat" and environ.get("REQUEST_METHOD") == "POST":
                    size = int(environ.get("CONTENT_LENGTH") or 0)
                    if not 0 < size <= 65536:
                        raise ValueError("请求大小无效")
                    payload = json.loads(environ["wsgi.input"].read(size).decode("utf-8"))
                    entry = payload.get("entry_id")
                    self.hub.get(entry)
                    method, target = payload.get("method"), payload.get("path")
                    readable = target in {"/api/me", "/api/members", "/api/conversations"} or (isinstance(target, str) and re.fullmatch(r"/api/conversations/[A-Za-z0-9_-]{1,160}/messages", target))
                    if not ((method == "GET" and readable) or (method == "POST" and target == "/api/questions")):
                        raise PermissionError("此操作不属于对话接口")
                    value = local_call(self.hub.data(entry), method, target, payload.get("payload") if method == "POST" else None, timeout=30)
                elif path == "/api/hub/action" and environ.get("REQUEST_METHOD") == "POST":
                    size = int(environ.get("CONTENT_LENGTH") or 0)
                    if not 0 < size <= 4096:
                        raise ValueError("请求大小无效")
                    payload = json.loads(environ["wsgi.input"].read(size).decode("utf-8"))
                    action, entry = payload.get("action"), payload.get("entry_id")
                    if action == "create":
                        value = self.hub.create_code(payload.get("space_name", ""))
                    elif action == "resume":
                        value = self.hub.create_code(existing_id=entry)
                    elif action == "invite":
                        value = self.hub.invitation(entry)
                    elif action == "open_chat":
                        self.hub.get(entry)
                        value = open_chat(self.hub.data(entry))
                    elif action == "connect":
                        from .setup_cli import start as start_node
                        self.hub.get(entry)
                        value = start_node(self.hub.data(entry))
                    elif action == "pending":
                        self.hub.get(entry)
                        value = local_call(self.hub.data(entry), "GET", "/api/setup/approvals")
                    elif action == "approve":
                        self.hub.get(entry)
                        value = local_call(self.hub.data(entry), "POST", "/api/setup/approve", {"request_id": payload["request_id"]})
                    elif action == "stop":
                        self.hub.get(entry)
                        value = local_call(self.hub.data(entry), "POST", "/api/setup/stop", {})
                    elif action == "close" and self.shutdown:
                        self.shutdown()
                        value = {"status": "closing_ui", "nodes_unchanged": True}
                    else:
                        raise ValueError("未知操作")
                else:
                    code, value = "404 Not Found", {"error": "页面不存在"}
        except PermissionError as exc:
            code, value = "403 Forbidden", {"error": str(exc)}
        except (ValueError, KeyError, TypeError) as exc:
            code, value = "400 Bad Request", {"error": str(exc)}
        except Exception:
            code, value = "502 Bad Gateway", {"error": "操作未完成，请检查连接状态后重试"}
        body = (value if content_type.startswith("text/html") else json.dumps(value, ensure_ascii=False)).encode("utf-8")
        start(code, [("Content-Type", content_type), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff"),
                     ("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")])
        return [body]


def run_ui(root, *, port=0, open_browser=True):
    token = secrets.token_urlsafe(32)
    app = HubApp(root, token)
    server = make_server("127.0.0.1", port, app, handler_class=QuietHandler)
    app.shutdown = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    print("UI_URL=" + url, flush=True)
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    finally:
        server.server_close()
