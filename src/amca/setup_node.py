"""Explicitly started, isolated connection node for conversational setup."""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .credentials import AcnCredentialBundle, WindowsCredentialStore, _protect, _unprotect
from .directory import ConfirmationMode, FollowupScope, InboundQuestionPolicy, MemberCard, MemberProfile
from .member_node import BootstrapA2AApp, QuickTunnel, SwitchableApp
from .onboarding import OwnerOnboardingService, SqliteInviteStore
from .pilot import DEFAULT_ACN_BASE_URL, join_one_time_invite
from .receiver import create_a2a_receiver
from .runtime import DeterministicRuntime
from .setup_config import PRODUCT, config_digest, public_config, read_config
from .transport import AcnNetworkAdapter, AgentRegistration, JsonHttpError


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def node_lock(data):
    """OS releases this lock even when a background process is terminated."""
    import msvcrt
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (data / "node.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("此配置目录的节点正在运行或配置中；请先查询状态或停止节点") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def apply_config(data, value, confirmed_digest):
    digest = config_digest(value)
    if not secrets.compare_digest(digest, confirmed_digest):
        raise ValueError("确认摘要与配置不一致；请重新展示配置并取得本人确认")
    data = Path(data)
    with node_lock(data):
        path = data / "setup.json"
        if (data / "credentials.json").exists() or (data / "pending-network-operation.json").exists():
            if not path.exists():
                raise ValueError("目录已有身份却没有对应配置；请使用新的独立目录")
            previous = read_config(path)
            if any(previous[key] != value[key] for key in ("mode", "space_name", "invite_code", "card")):
                raise ValueError("已登记身份不能通过配置覆盖；仅可修改本人策略，或使用新目录建立新身份")
        write_json(path, value)
        result = {"product": PRODUCT, "status": "configured", "config_digest": digest,
                  "configuration": public_config(value), "connected": False,
                  "automatic_answers_enabled": False,
                  "next": "执行 start 后通过 status 验证实际连接；配置保存不等于联网成功"}
        write_json(data / "configuration-receipt.json", result)
        return result


def local_call(data, method, path, payload=None, timeout=5, agent_token=None):
    # The session is encrypted for this Windows user; no bearer is printed to the agent.
    session = json.loads(_unprotect(base64.b64decode((Path(data) / "session.dpapi").read_bytes())))
    port = session["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("本地会话端口无效")
    if not path.startswith("/api/") or any(x in path for x in ("?", "#", "\\", "..", ":")):
        raise ValueError("仅支持本机 /api/ 接口路径")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    auth = {"X-Agent-Session-Token": agent_token} if agent_token is not None else {"X-AMCA-Local-Token": session["token"]}
    request = Request(f"http://127.0.0.1:{port}" + path, data=body, method=method,
                      headers={**auth, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(4096).decode("utf-8")).get("error", "请求未成功")
        except (ValueError, UnicodeDecodeError):
            detail = "请求未成功"
        raise RuntimeError(f"本机接口返回 HTTP {exc.code}：{str(detail)[:500]}") from None


def check_execution_context(data):
    path = Path(data) / "execution-context.dpapi"
    if path.exists():
        _unprotect(base64.b64decode(path.read_bytes()))


def status(data):
    data = Path(data)
    result = {"product": PRODUCT, "status": "not_configured", "connected": False, "automatic_answers_enabled": False}
    if (data / "binding.sqlite3").exists():
        from .agent_binding import AgentBinding
        result["binding"] = AgentBinding(data).status()
    if not (data / "setup.json").exists():
        return result
    config = read_config(data / "setup.json")
    result.update(status="configured_offline", config_digest=config_digest(config), configuration=public_config(config))
    if (data / "connection-receipt.json").exists():
        receipt = json.loads((data / "connection-receipt.json").read_text(encoding="utf-8"))
        result["last_connection"] = receipt
    try:
        live = local_call(data, "GET", "/api/setup/status", timeout=2)
        if live.get("config_digest") == result["config_digest"]:
            result.update(live)
    except PermissionError as exc:
        result.update(status="local_access_denied", local_access_error=str(exc))
    except (OSError, ValueError, RuntimeError):
        pass
    if (data / "startup-error.json").exists() and not result["connected"]:
        result["startup_error"] = json.loads((data / "startup-error.json").read_text(encoding="utf-8"))
    return result


def _retry_endpoint(operation, sleeper=time.sleep):
    for attempt in range(36):
        try:
            return operation()
        except JsonHttpError as exc:
            transient = exc.status == 400 and ("Endpoint host cannot be resolved" in str(exc) or "not in an allowed address range" in str(exc))
            if not transient or attempt == 35:
                raise
            sleeper(5)


def diagnose(data):
    """Read-only recovery classification. Never guesses whether a POST succeeded."""
    data = Path(data)
    result = {"product": PRODUCT, "connected": status(data)["connected"], "configuration_preserved": (data / "setup.json").exists()}
    pending = data / "pending-network-operation.json"
    if pending.exists():
        operation = json.loads(pending.read_text(encoding="utf-8"))["operation"]
        store = WindowsCredentialStore(data / "credentials.json")
        saved = store.load() if store.path.exists() else None
        recoverable = bool(saved and (operation == "register" or (operation == "create_space" and saved.subnet_slug)))
        result.update(recovery="resume_saved_result" if recoverable else "remote_result_unknown", operation=operation,
                      can_retry=recoverable, next=("已保存操作结果，重试同一 start 即可恢复。" if recoverable else
                      "旧记录没有足够证据判断服务端是否创建成功。需服务端核对或恢复凭据；不要删除记录、重建身份或重填配置。当前服务未提供按本地接入编号查询注册结果的接口。"))
    else:
        error_path = data / "startup-error.json"
        error = json.loads(error_path.read_text(encoding="utf-8")) if error_path.exists() else {}
        if error.get("http_status") == 422:
            result.update(recovery="fix_request", can_retry=False, validation_fields=error.get("validation_fields", []),
                          next="服务端拒绝请求字段（HTTP 422），不是服务未启动。请更新程序或修正所列字段后再连接；不要原样重试。")
        else:
            result.update(recovery="retry_same_entry", can_retry=True, next="保留配置；检查网络与执行环境权限后重试同一 start。")
    return result


def connect(config, data, endpoint):
    """Retain partial identities; do not silently repeat uncertain create operations."""
    data = Path(data)
    store = WindowsCredentialStore(data / "credentials.json")
    card = config["card"]
    if config["mode"] == "join":
        target_path = data / "entry-target.json"
        expected_space = json.loads(target_path.read_text(encoding="utf-8")).get("space_id") if target_path.exists() else None
        join_one_time_invite(config["invite_code"], endpoint=endpoint, credential_path=store.path,
                             name=card["role_name"], description=card["description"],
                             registration_attempts=36, registration_retry_seconds=5, expected_subnet=expected_space)
        credentials = store.load()
    else:
        uncertain = data / "pending-network-operation.json"
        if uncertain.exists():
            if diagnose(data)["recovery"] == "resume_saved_result":
                uncertain.unlink()
            else:
                raise RuntimeError("此前创建操作结果不确定；执行 diagnose 查看恢复分类。保留本机状态，禁止重复创建身份或空间")
        if store.path.exists():
            credentials = store.load()
            adapter = AcnNetworkAdapter(credentials.base_url, agent_id=credentials.agent_id, api_key=credentials.api_key)
        else:
            adapter = AcnNetworkAdapter(DEFAULT_ACN_BASE_URL)
            write_json(uncertain, {"operation": "register", "started_at": datetime.now(UTC).isoformat()})
            try:
                identity = _retry_endpoint(lambda: adapter.register_agent(AgentRegistration(
                    card["role_name"], card["description"], ("owner-controlled", "collaboration"), endpoint)))
            except JsonHttpError as exc:
                if exc.request_not_sent or (exc.status is not None and 400 <= exc.status < 500):
                    uncertain.unlink(missing_ok=True)
                raise
            credentials = AcnCredentialBundle(DEFAULT_ACN_BASE_URL, identity.agent_id, identity.api_key, endpoint,
                                               identity.claim_url, identity.referral_url)
            store.save(credentials)
            uncertain.unlink()
        if not credentials.subnet_slug:
            write_json(uncertain, {"operation": "create_space", "agent_id": credentials.agent_id})
            try:
                network = adapter.create_network(name=config["space_name"], description=f"{PRODUCT} private collaboration space", is_private=True)
            except JsonHttpError as exc:
                if exc.request_not_sent or (exc.status is not None and 400 <= exc.status < 500):
                    uncertain.unlink(missing_ok=True)
                raise
            credentials = replace(credentials, subnet_slug=str(network["slug"]))
            store.save(credentials)
            uncertain.unlink()
    adapter = AcnNetworkAdapter(credentials.base_url, agent_id=credentials.agent_id, api_key=credentials.api_key)
    probe = _retry_endpoint(lambda: adapter.update_endpoint(endpoint))
    if probe.get("endpoint_reachable") is not True or probe.get("a2a_handshake_ok") is not True:
        raise RuntimeError("服务端未确认公网端点和握手；凭据已保留，可重试")
    credentials = replace(credentials, a2a_endpoint=endpoint)
    store.save(credentials)
    members = adapter.discover(credentials.subnet_slug)
    if credentials.agent_id not in {x.get("agent_id") for x in members}:
        raise RuntimeError("服务端未确认本智能体空间成员身份")
    return credentials, adapter


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def serve(data, cloudflared):
    data = Path(data)
    check_execution_context(data)
    with node_lock(data):
        config = read_config(data / "setup.json")
        (data / "startup-error.json").unlink(missing_ok=True)
        switchable = SwitchableApp(BootstrapA2AApp())
        server = make_server("127.0.0.1", 0, switchable, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        tunnel = receiver = None
        stopped = threading.Event()
        try:
            tunnel = QuickTunnel.start(cloudflared, server.server_port)
            credentials, adapter = connect(config, data, tunnel.url + "/a2a")
            target_file = data / "entry-target.json"
            if target_file.exists():
                target = json.loads(target_file.read_text(encoding="utf-8"))
                if target.get("space_id") and target["space_id"] != credentials.subnet_slug:
                    raise PermissionError("邀请码的目标空间与服务端实际加入结果不一致")
            token = secrets.token_urlsafe(32)
            # Saving permissions does not activate a nonexistent model/agent runtime.
            policy = config["policy"]
            profile = MemberProfile(MemberCard(credentials.agent_id, **config["card"]), InboundQuestionPolicy(
                ConfirmationMode.CONFIRM_EACH, frozenset(policy["allowed_asker_ids"]), frozenset(policy["allowed_topics"]),
                frozenset(policy["allowed_project_scopes"]), 0, FollowupScope.CHAIN))
            onboarding = None
            if config["mode"] == "create":
                onboarding = OwnerOnboardingService(SqliteInviteStore(data / "coordinator.sqlite3"), adapter)
            receiver = create_a2a_receiver(profile=profile, owner_id=config["card"]["owner_claim"],
                private_database=data / "private.sqlite3", coordinator_database=data / "coordinator.sqlite3",
                runtime=DeterministicRuntime(""), onboarding=onboarding, outbound_adapter=adapter,
                subnet_slug=credentials.subnet_slug, local_chat_database=data / "local-chat.sqlite3", local_access_token=token)
            bridge = None
            if (data / "binding.sqlite3").exists():
                from .agent_bridge import AgentBridge
                bridge = AgentBridge(data, config, credentials, adapter, receiver.local_chat)
                receiver.agent_bridge = bridge
            live = {"product": PRODUCT, "status": "connected", "connected": True,
                    "agent_id": credentials.agent_id, "space_id": credentials.subnet_slug,
                    "public_endpoint": credentials.a2a_endpoint, "config_digest": config_digest(config),
                    "endpoint_reachable": True, "a2a_handshake_ok": True, "membership_verified": True,
                    "automatic_answers_enabled": False, "effective_inbound_mode": "confirm_each",
                    "runtime_note": "策略已保存；自动回答尚未启用。需要本人介入的请求可在本地会话查询。",
                    "checked_at": datetime.now(UTC).isoformat(), "identity_verification": "unverified_self_claim"}

            def app(environ, start_response):
                path = environ.get("PATH_INFO", "")
                if path.startswith("/api/agent/"):
                    try:
                        if bridge is None:
                            raise PermissionError("此节点尚未绑定智能体会话")
                        secret = environ.get("HTTP_X_AGENT_SESSION_TOKEN", "")
                        bridge.binding.authorized(secret)
                        if environ["REQUEST_METHOD"] == "GET" and path == "/api/agent/poll":
                            value = bridge.poll(secret)
                        elif environ["REQUEST_METHOD"] == "POST":
                            size = int(environ.get("CONTENT_LENGTH") or 0)
                            if not 0 < size <= 65536:
                                raise ValueError("请求大小无效")
                            payload = json.loads(environ["wsgi.input"].read(size).decode("utf-8"))
                            if path == "/api/agent/ack":
                                value = bridge.binding.acknowledge(secret, payload["challenge_id"], payload["nonce"])
                            elif path == "/api/agent/reply":
                                value = bridge.reply(secret, payload)
                            elif path == "/api/agent/ask":
                                bridge.binding.authorized(secret, verified=True)
                                value = receiver.local_chat.send_question(payload)
                            else:
                                raise ValueError("未知会话接口")
                        else:
                            raise ValueError("未知会话接口")
                        code = "200 OK"
                    except PermissionError as exc:
                        code, value = "403 Forbidden", {"error": str(exc)}
                    except (ValueError, KeyError, TypeError) as exc:
                        code, value = "400 Bad Request", {"error": str(exc)}
                    except Exception:
                        code, value = "502 Bad Gateway", {"error": "请求暂未完成，请保留原请求后重试"}
                    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                    start_response(code, [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))])
                    return [body]
                if path.startswith("/api/setup/"):
                    supplied = environ.get("HTTP_X_AMCA_LOCAL_TOKEN", "")
                    if not secrets.compare_digest(token, supplied):
                        code, value = "403 Forbidden", {"error": "local_access_denied"}
                    elif path == "/api/setup/status" and environ["REQUEST_METHOD"] == "GET":
                        code, value = "200 OK", {**live, "binding": bridge.binding.status() if bridge else {"session_bound": False}}
                        if bridge:
                            value.update(effective_inbound_mode=config["policy"]["confirmation_mode"],
                                         agent_channel_ready=bridge.binding.status()["channel_verified"],
                                         background_wakeup_enabled=False,
                                         runtime_note="已连接本机智能体收发通道；须由该对话主动收取，不能唤醒关闭的对话。")
                    elif path == "/api/setup/approvals" and environ["REQUEST_METHOD"] == "GET":
                        code, value = "200 OK", bridge.pending_approvals() if bridge else []
                    elif path == "/api/setup/approve" and environ["REQUEST_METHOD"] == "POST" and bridge:
                        try:
                            size = int(environ.get("CONTENT_LENGTH") or 0)
                            if not 0 < size <= 4096:
                                raise ValueError("确认请求大小无效")
                            payload = json.loads(environ["wsgi.input"].read(size).decode("utf-8"))
                            value = bridge.approve(payload["request_id"])
                            code = "200 OK"
                        except (ValueError, KeyError, PermissionError) as exc:
                            code, value = "400 Bad Request", {"error": str(exc)}
                    elif path == "/api/setup/stop" and environ["REQUEST_METHOD"] == "POST":
                        stopped.set()
                        code, value = "200 OK", {"status": "stopping"}
                    else:
                        code, value = "404 Not Found", {"error": "not found"}
                    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                    start_response(code, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                    return [body]
                return receiver(environ, start_response)

            switchable.replace(app)
            encrypted = base64.b64encode(_protect(json.dumps({"port": server.server_port, "token": token}).encode("utf-8")))
            (data / "session.dpapi").write_bytes(encrypted)
            write_json(data / "connection-receipt.json", live)
            while not stopped.wait(0.5):
                if tunnel.process.poll() is not None:
                    raise RuntimeError("公网隧道已经停止；连接失效")
        except Exception as exc:
            # Remote response bodies can contain secrets. Never copy them into receipts.
            write_json(data / "startup-error.json", {"error_type": type(exc).__name__,
                "message": str(exc) if isinstance(exc, (ValueError, RuntimeError)) and not isinstance(exc, JsonHttpError) else "连接失败；请检查网络或接入服务状态",
                "http_status": exc.status if isinstance(exc, JsonHttpError) else None,
                "failure_kind": exc.failure_kind if isinstance(exc, JsonHttpError) else None,
                "validation_fields": exc.validation_fields if isinstance(exc, JsonHttpError) else [],
                "request_not_sent": exc.request_not_sent if isinstance(exc, JsonHttpError) else None,
                "next": ("请求字段被服务端拒绝（HTTP 422）；执行 diagnose 查看字段，不能原样重试或要求用户启动服务。" if isinstance(exc, JsonHttpError) and exc.status == 422 else
                         "请求尚未发出；检查联网权限、域名解析或证书后重试同一 start，配置已保留。" if isinstance(exc, JsonHttpError) and exc.request_not_sent else
                         "保留原目录；若存在未决创建记录，不可删除标记或重新注册。")})
            raise
        finally:
            (data / "session.dpapi").unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if receiver:
                receiver.close()
            if tunnel:
                tunnel.close()
