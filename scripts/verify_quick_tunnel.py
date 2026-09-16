from __future__ import annotations

import sys
import threading
from pathlib import Path
from wsgiref.simple_server import make_server

from amca.member_node import BootstrapA2AApp, QuickTunnel, wait_for_public_endpoint


def main() -> int:
    cloudflared = Path(sys.argv[1])
    server = make_server("127.0.0.1", 0, BootstrapA2AApp())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tunnel = None
    try:
        tunnel = QuickTunnel.start(cloudflared, server.server_port)
        print(f"quick_tunnel_url={tunnel.url}", flush=True)
        wait_for_public_endpoint(tunnel.url + "/health", timeout=180)
        print("quick_tunnel_public_health=ok")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        if tunnel is not None:
            tunnel.close()


if __name__ == "__main__":
    raise SystemExit(main())
