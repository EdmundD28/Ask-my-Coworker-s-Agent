from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .setup_config import PRODUCT, config_digest, contract, public_config, read_config
from .setup_handbook import write_handbook
from .setup_node import apply_config, check_execution_context, local_call, node_lock, serve, status
from .transport import JsonHttpError


def package_directory():
    if getattr(sys, "frozen", False):
        # A packaged copy is authoritative for its own machine.  Never carry a
        # build-machine path or a versioned development path into its codes.
        return Path(sys.executable).resolve().parent

    project = Path(__file__).resolve().parents[2]
    dist = project / "dist"
    executable_name = PRODUCT + ".exe"
    candidates = [path for path in dist.glob(PRODUCT + "*/" + executable_name) if path.is_file()]
    if candidates:
        # Source-mode development may have several versioned builds.  Use the
        # executable most recently built on this machine instead of a fixed v4
        # directory name.
        return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve().parent
    return dist / PRODUCT


def start(data):
    data = Path(data).resolve()
    read_config(data / "setup.json")
    check_execution_context(data)
    current = status(data)
    if current["status"] == "local_access_denied":
        raise PermissionError(current["local_access_error"])
    if current["connected"]:
        return current
    try:
        with node_lock(data):
            pass
    except RuntimeError:
        return {"product": PRODUCT, "status": "starting", "connected": False, "next": "请查询 status；已有启动进程，未重复创建"}
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        command = [sys.executable, "-m", "amca.setup_cli"]
    command += ["--data-dir", str(data), "_serve"]
    (data / "startup-error.json").unlink(missing_ok=True)
    # Child errors are sanitized by main; this log never contains raw remote bodies.
    with (data / "startup.log").open("ab") as output:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                 env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                 close_fds=True)
    return {"product": PRODUCT, "status": "starting", "connected": False, "process_id": child.pid,
            "next": "查询 status；需要 endpoint_reachable、a2a_handshake_ok 和 membership_verified 均为 true"}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=PRODUCT + " 智能体对话配置接口")
    parser.add_argument("--data-dir", default=str(package_directory() / "test-session"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("describe")
    commands.add_parser("guide")
    check = commands.add_parser("preflight")
    check.add_argument("--file", required=True)
    check.add_argument("--registry-dir", default=str(package_directory() / "ui-data"))
    ui = commands.add_parser("ui")
    ui.add_argument("--registry-dir", default=str(package_directory() / "ui-data"))
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-open", action="store_true")
    accept = commands.add_parser("accept")
    accept.add_argument("--file", required=True)
    accept.add_argument("--label", required=True)
    accept.add_argument("--registry-dir", default=str(package_directory() / "ui-data"))
    agent = commands.add_parser("agent")
    operations = agent.add_subparsers(dest="agent_action", required=True)
    for operation in ("poll", "ack", "reply", "ask"):
        item = operations.add_parser(operation)
        item.add_argument("--session-file", required=True)
        if operation != "poll":
            item.add_argument("--file", required=True)
    for name in ("validate", "apply"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--file", required=True)
        if name == "apply":
            cmd.add_argument("--confirmed-digest", required=True)
    for name in ("status", "start", "stop", "members", "diagnose", "_serve"):
        commands.add_parser(name)
    handbook = commands.add_parser("handbook")
    handbook.add_argument("--output", required=True)
    handbook.add_argument("--package-dir", default=str(package_directory()))
    invite = commands.add_parser("invite")
    invite.add_argument("--output", required=True)
    call = commands.add_parser("call")
    call.add_argument("--method", choices=["GET", "POST"], required=True)
    call.add_argument("--path", required=True)
    call.add_argument("--file")
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "amca.setup_cli"]
        subprocess.Popen([*command, "ui"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}, close_fds=True)
        return 0
    args = parser.parse_args(arguments)
    data = Path(args.data_dir).resolve()
    try:
        if args.command == "describe":
            result = contract()
        elif args.command == "guide":
            from .agent_guide import GUIDE
            result = {"product": PRODUCT, "manual_version": 4, "manual": GUIDE}
        elif args.command == "preflight":
            from .setup_preflight import preflight
            result = {"product": PRODUCT, **preflight(args.file, args.registry_dir)}
        elif args.command == "ui":
            from .hub_ui import run_ui
            run_ui(args.registry_dir, port=args.port, open_browser=not args.no_open)
            return 0
        elif args.command == "accept":
            from .entry_hub import accept_file
            result = accept_file(args.file, registry=args.registry_dir, label=args.label)
        elif args.command == "agent":
            from .entry_hub import load_session
            session = load_session(args.session_file)
            payload = None
            if args.agent_action != "poll":
                if Path(args.file).stat().st_size > 65536:
                    raise ValueError("智能体提交文件超过 64 KiB")
                payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            result = local_call(session["data_dir"], "GET" if args.agent_action == "poll" else "POST",
                "/api/agent/" + args.agent_action, payload, timeout=30, agent_token=session["secret"])
        elif args.command == "handbook":
            path = write_handbook(args.output, package_directory=args.package_dir)
            result = {"product": PRODUCT, "handbook": str(path.resolve()), "contains_credentials": False}
        elif args.command == "validate":
            value = read_config(args.file)
            result = {"product": PRODUCT, "status": "valid", "config_digest": config_digest(value), "configuration": public_config(value)}
        elif args.command == "apply":
            config = read_config(args.file)
            target = data / "entry-target.json"
            if target.exists():
                from .entry_hub import EntryHub
                scope = json.loads(target.read_text(encoding="utf-8"))
                hub = EntryHub(scope["registry_path"])
                entry = hub.get(scope["entry_id"])
                if hub.data(scope["entry_id"]).resolve() != data or config["mode"] != entry["mode"] or config["invite_code"] != entry["invite_code"]:
                    raise PermissionError("配置目标与界面生成的接入码不一致")
            result = apply_config(data, config, args.confirmed_digest)
            if target.exists():
                hub.remember(config)
        elif args.command == "status":
            result = status(data)
        elif args.command == "diagnose":
            from .setup_node import diagnose
            result = diagnose(data)
        elif args.command == "start":
            result = start(data)
        elif args.command == "stop":
            result = local_call(data, "POST", "/api/setup/stop", {})
        elif args.command == "members":
            result = local_call(data, "GET", "/api/members")
        elif args.command == "call":
            allowed = args.method == "GET" and (args.path in {"/api/me", "/api/members", "/api/conversations"} or (args.path.startswith("/api/conversations/") and args.path.endswith("/messages")))
            allowed |= args.method == "POST" and args.path == "/api/questions"
            if not allowed:
                raise ValueError("此接口不在智能体接入工具的允许列表")
            payload = None
            if args.method == "POST":
                if not args.file or Path(args.file).stat().st_size > 65536:
                    raise ValueError("发送问题需要最多 64 KiB 的 JSON 文件")
                payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            result = local_call(data, args.method, args.path, payload, timeout=30)
        elif args.command == "invite":
            raise ValueError("请在界面点击“邀请成员”生成邀请码，或双击程序打开界面")
        else:
            resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2] / ".amca" / "tools"))
            serve(data, resource / "cloudflared.exe")
            result = {"product": PRODUCT, "status": "stopped"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Do not print HTTP bodies, invitation tokens or decrypted credentials.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError, PermissionError)) and not isinstance(exc, JsonHttpError) else "操作失败；请检查文件、网络及 status 中的状态"
        print(json.dumps({"product": PRODUCT, "status": "error", "error_type": type(exc).__name__, "message": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
