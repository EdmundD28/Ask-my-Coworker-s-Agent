from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from wsgiref.simple_server import make_server

from .credentials import WindowsCredentialStore
from .directory import ConfirmationMode, FollowupScope, InboundQuestionPolicy, MemberCard, MemberProfile
from .pilot import DEFAULT_ACN_BASE_URL, join_one_time_invite
from .receiver import create_a2a_receiver
from .runtime import DeterministicRuntime
from .transport import AcnNetworkAdapter


TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class BootstrapA2AApp:
    """Minimal reachable endpoint used only while ACN assigns the final identity."""

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/health":
            body = b'{"status":"bootstrapping"}'
            status = "200 OK"
        elif path == "/a2a" and environ.get("REQUEST_METHOD") == "POST":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32601, "message": "Bootstrapping"},
                }
            ).encode("utf-8")
            status = "200 OK"
        else:
            body = b'{"error":"not found"}'
            status = "404 Not Found"
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]


class SwitchableApp:
    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()

    def replace(self, app) -> None:
        with self._lock:
            self._app = app

    def __call__(self, environ, start_response):
        with self._lock:
            app = self._app
        return app(environ, start_response)


class QuickTunnel:
    def __init__(self, process, url: str):
        self.process = process
        self.url = url

    @classmethod
    def start(cls, executable: str | Path, port: int, *, timeout: float = 45.0):
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(executable), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def read_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_stderr, daemon=True).start()
        deadline = __import__("time").monotonic() + timeout
        while __import__("time").monotonic() < deadline:
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            match = TUNNEL_URL.search(line)
            if match:
                return cls(process, match.group(0))
        process.terminate()
        raise RuntimeError("Could not create the temporary public endpoint")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def wait_for_public_endpoint(
    url: str,
    *,
    timeout: float = 180.0,
    request_timeout: float = 5.0,
    sleeper=time.sleep,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not reachable"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=request_timeout) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        sleeper(1.0)
    raise RuntimeError(f"Public endpoint did not become ready: {last_error}")


def _resource_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / name


def run_member(
    *,
    invite_code: str,
    cloudflared: str | Path,
    data_directory: str | Path,
    name: str,
    owner_claim: str,
    base_url: str = DEFAULT_ACN_BASE_URL,
) -> dict:
    data = Path(data_directory)
    data.mkdir(parents=True, exist_ok=True)
    switchable = SwitchableApp(BootstrapA2AApp())
    server = make_server("127.0.0.1", 0, switchable)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    tunnel = None
    receiver = None
    try:
        tunnel = QuickTunnel.start(cloudflared, server.server_port)
        endpoint = tunnel.url + "/a2a"
        result = join_one_time_invite(
            invite_code,
            endpoint=endpoint,
            credential_path=data / "credentials.json",
            base_url=base_url,
            name=name,
            description="Owner-controlled AMCA member agent",
            registration_attempts=36,
            registration_retry_seconds=5.0,
            on_registration_retry=lambda attempt, total: print(
                f"Waiting for the public address to propagate ({attempt}/{total})...",
                flush=True,
            ),
        )
        credentials = WindowsCredentialStore(data / "credentials.json").load()
        profile = MemberProfile(
            card=MemberCard(
                agent_id=credentials.agent_id,
                role_name=name,
                description="Owner-controlled AMCA member agent",
                owner_claim=owner_claim,
            ),
            inbound_policy=InboundQuestionPolicy(
                confirmation_mode=ConfirmationMode.CONFIRM_EACH,
                allowed_asker_ids=frozenset(),
                allowed_topics=frozenset(),
                allowed_project_scopes=frozenset(),
                followup_limit=0,
                followup_scope=FollowupScope.CHAIN,
            ),
        )
        local_access_token = secrets.token_urlsafe(32)
        receiver = create_a2a_receiver(
            profile=profile,
            owner_id=owner_claim,
            private_database=data / "private.sqlite3",
            coordinator_database=data / "coordinator.sqlite3",
            runtime=DeterministicRuntime(""),
            outbound_adapter=AcnNetworkAdapter(
                credentials.base_url,
                agent_id=credentials.agent_id,
                api_key=credentials.api_key,
            ),
            subnet_slug=credentials.subnet_slug,
            local_chat_database=data / "local-chat.sqlite3",
            local_access_token=local_access_token,
        )
        switchable.replace(receiver)
        return {
            **result,
            "public_endpoint": endpoint,
            "local_chat_url": (
                f"http://127.0.0.1:{server.server_port}/#token={local_access_token}"
            ),
            "tunnel": tunnel,
            "server": server,
            "receiver": receiver,
        }
    except Exception:
        if receiver is not None:
            receiver.close()
        if tunnel is not None:
            tunnel.close()
        server.shutdown()
        server.server_close()
        raise


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Join AMCA in one step")
    parser.add_argument("--invite-file", default="invite.json")
    parser.add_argument("--data-dir", default=str(Path.home() / ".amca-member"))
    parser.add_argument("--cloudflared", default=str(_resource_path("cloudflared.exe")))
    parser.add_argument("--name", default=f"{os.environ.get('USERNAME', 'Member')} AMCA Agent")
    parser.add_argument("--owner-claim", default=os.environ.get("USERNAME", "unverified member"))
    args = parser.parse_args(argv)
    invitation = json.loads(Path(args.invite_file).read_text(encoding="utf-8"))
    print("Connecting securely. Keep this window open...")
    try:
        state = run_member(
            invite_code=invitation["invite_code"],
            cloudflared=args.cloudflared,
            data_directory=args.data_dir,
            name=args.name,
            owner_claim=args.owner_claim,
            base_url=invitation.get("base_url", DEFAULT_ACN_BASE_URL),
        )
    except Exception as exc:
        print(f"Connection failed: {exc}")
        print("No owner credentials were exposed. You may close this window.")
        raise SystemExit(1) from None
    print("Connected. Your agent is online while this window remains open.")
    print(f"Agent ID: {state['agent_id']}")
    print("Opening your private local chat page...")
    webbrowser.open(state["local_chat_url"], new=2)
    try:
        state["tunnel"].process.wait()
        raise RuntimeError("The public endpoint stopped unexpectedly")
    except KeyboardInterrupt:
        print("Stopping safely...")
    finally:
        state["server"].shutdown()
        state["server"].server_close()
        state["receiver"].close()
        state["tunnel"].close()


if __name__ == "__main__":
    main()
