"""UI-owned entry tickets and reusable persona; grants remain per space."""
from __future__ import annotations

import base64
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .agent_binding import AgentBinding, digest
from .credentials import _protect, _unprotect
from .setup_config import PRODUCT, contract, read_config, QUESTION_RULES
from .setup_node import status, write_json

ENTRY_SCHEMA = "ask-my-coworkers-agent.entry.v2"
MARKER = "ASK-MY-COWORKERS-AGENT-ENTRY"


def render_entry(packet):
    from .setup_cli import package_directory
    locator = ("本机程序位置：" + str(package_directory() / "Ask My Coworker's Agent.exe")
               if packet["kind"] != "join" else "使用你自己电脑上解压的 Ask My Coworker's Agent.exe；找不到时只询问解压位置，不扫描整台电脑。")
    return f"""{PRODUCT} 接入码

请实际完成这份码指定的接入，必要选择问我，可选项允许跳过。产品名称始终写全称。
{locator}
已实测成功的顺序：读取手册 → 环境检查 → 领取 → 简短确认配置 → 保存 → 连接 → 收取挑战并回复。
1. 将程序完整路径赋给 PowerShell 的 $exe，将完整接入码保存成 UTF-8 文本。
2. 设置 [Console]::OutputEncoding 和 $OutputEncoding 为 [System.Text.UTF8Encoding]::new($false)，执行 & $exe guide。manual_version 必须至少为4，否则请使用新版程序。
3. 按手册执行 & $exe preflight --file <接入码文件>。通过后才 accept；所有后续命令保持同一Windows账户和同一工具执行环境。
4. 若工具拦截目录写入或联网，使用该工具正式提供的授权机制重试原命令，不绕过权限，不换目录重建。不能把“配置已保存”当作接入成功。
5. 首次一次最多两个短问题；已有名片复用。逐次确认不问成员编号，不限次数不问周期。职责可不填，程序负责适配服务字段。
6. 严格按内置手册完成 status 和 agent poll/ack；HTTP 422 是请求字段错误，不能叫我“启动接入服务”或原样重试。完成后只报空间、策略和验证结果。
无需安装依赖、模型供应商密钥或修改源码。每次命令调用都设置所需路径变量，不能假设不同工具调用共享PowerShell变量。

{MARKER}-BEGIN
{json.dumps(packet, ensure_ascii=False, indent=2)}
{MARKER}-END
"""


def parse_entry(text):
    if not isinstance(text, str) or len(text) > 32000:
        raise ValueError("接入码过长或不是文本")
    match = re.search(MARKER + r"-BEGIN\s*(.*?)\s*" + MARKER + r"-END", text, re.S)
    if not match:
        raise ValueError("请粘贴界面生成的完整接入码")
    packet = json.loads(match.group(1))
    required = {"schema", "kind", "entry_id", "registry_path", "ticket", "space_id", "space_name", "invite_code", "expires_at"}
    if not isinstance(packet, dict) or set(packet) != required or packet["schema"] != ENTRY_SCHEMA:
        raise ValueError("不支持的接入码结构")
    if packet["kind"] not in {"create", "join", "resume"}:
        raise ValueError("不支持的接入模式")
    if not isinstance(packet["entry_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", packet["entry_id"]):
        raise ValueError("接入编号无效")
    for key in ("registry_path", "ticket", "space_name", "invite_code"):
        if not isinstance(packet[key], str):
            raise ValueError("接入码字段类型无效")
    if not isinstance(packet["expires_at"], (int, float)) or packet["expires_at"] <= time.time():
        raise PermissionError("接入码已过期，请从界面重新生成")
    if packet["kind"] == "join" and (not packet["invite_code"] or not isinstance(packet["space_id"], str) or not packet["space_id"]):
        raise ValueError("邀请码必须指定唯一空间和一次性邀请凭证")
    return packet


class EntryHub:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "hub.sqlite3"
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS entries (entry_id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def data(self, entry_id):
        if not re.fullmatch(r"[a-f0-9]{32}", entry_id):
            raise ValueError("接入编号无效")
        return self.root / "spaces" / entry_id

    def get(self, entry_id, db=None):
        if db is None:
            with self.db() as connection:
                return self.get(entry_id, connection)
        row = db.execute("SELECT body FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        if not row:
            raise ValueError("找不到此接入记录")
        return json.loads(row[0])

    def save(self, entry, db):
        db.execute("INSERT OR REPLACE INTO entries VALUES (?, ?)", (entry["entry_id"], json.dumps(entry)))

    def create_code(self, space_name="", *, existing_id=None):
        if not isinstance(space_name, str) or len(space_name) > 120:
            raise ValueError("空间名称最多 120 字符")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = self.get(existing_id, db) if existing_id else {"entry_id": secrets.token_hex(16), "space_name": space_name,
                "mode": "create", "space_id": None, "invite_code": "", "claimed": False}
            token = secrets.token_urlsafe(32)
            entry.update(ticket_hash=digest(token), expires_at=time.time() + 900, claimed=False)
            self.save(entry, db)
        packet = {"schema": ENTRY_SCHEMA, "kind": "resume" if existing_id else "create", "entry_id": entry["entry_id"],
                  "registry_path": str(self.root), "ticket": token, "space_id": entry["space_id"],
                  "space_name": entry["space_name"], "invite_code": "", "expires_at": entry["expires_at"]}
        return {"entry_id": entry["entry_id"], "code": render_entry(packet), "expires_at": packet["expires_at"]}

    def invitation(self, entry_id):
        from .pilot import create_one_time_invite
        entry = self.get(entry_id)
        data = self.data(entry_id)
        current = status(data)
        if entry["mode"] != "create" or not current["connected"]:
            raise ValueError("请先完成创建者的空间接入")
        receipt = create_one_time_invite(data / "credentials.json", coordinator_database=data / "coordinator.sqlite3",
            join_url=current["public_endpoint"].removesuffix("/a2a") + "/onboarding/join")
        packet = {"schema": ENTRY_SCHEMA, "kind": "join", "entry_id": secrets.token_hex(16), "registry_path": "",
                  "ticket": "", "space_id": current["space_id"], "space_name": entry["space_name"],
                  "invite_code": receipt["invite_code"], "expires_at": time.time() + 86400}
        return {"code": render_entry(packet), "space_id": packet["space_id"], "expires_at": packet["expires_at"]}

    def accept(self, packet, label):
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("请给当前智能体对话提供简短名称")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if packet["kind"] == "join":
                rows = [json.loads(x[0]) for x in db.execute("SELECT body FROM entries")]
                entry = next((x for x in rows if x["space_id"] == packet["space_id"]), None)
                if entry:
                    return {"status": "already_known_space", "entry_id": entry["entry_id"], "data_dir": str(self.data(entry["entry_id"])),
                            "next": "此空间已有接入记录；无需重注册。继续原对话，或由本人在界面点击更换负责对话。"}
                entry = {"entry_id": secrets.token_hex(16), "mode": "join", "space_id": packet["space_id"],
                         "space_name": packet["space_name"], "invite_code": packet["invite_code"], "claimed": False}
            else:
                entry = self.get(packet["entry_id"], db)
                if entry["expires_at"] <= time.time() or not secrets.compare_digest(entry["ticket_hash"], digest(packet["ticket"])):
                    raise PermissionError("本机配置码无效、已撤换或已过期")
                if entry.get("claimed"):
                    return {"status": "already_claimed", "entry_id": entry["entry_id"], "data_dir": str(self.data(entry["entry_id"])),
                            "next": "此接入码已经领取；继续原对话，或由本人在界面更换负责对话。"}
            data = self.data(entry["entry_id"])
            data.mkdir(parents=True, exist_ok=True)
            binding = AgentBinding(data)
            credential = binding.bind(label)
            session_file = data / ("tool-session-" + credential["binding_id"] + ".dpapi")
            session_file.write_bytes(base64.b64encode(_protect(json.dumps({**credential, "data_dir": str(data)}).encode("utf-8"))))
            (data / "execution-context.dpapi").write_bytes(base64.b64encode(_protect(b"Ask My Coworker's Agent execution context")))
            write_json(data / "entry-target.json", {"entry_id": entry["entry_id"], "registry_path": str(self.root), "space_id": entry["space_id"]})
            entry.update(claimed=True, session_label=label)
            self.save(entry, db)
        template = contract()["template"]
        preferences = self.preferences()
        if preferences:
            template["card"] = preferences["card"]
            template["policy"]["confirmation_mode"] = preferences["confirmation_mode"]
            template["policy"]["answer_quota"] = preferences["answer_quota"]
        template.update(mode=entry["mode"], space_name=entry["space_name"], invite_code=entry["invite_code"])
        configured = (data / "setup.json").exists()
        live = status(data)
        if live["connected"]:
            binding.activate(live["agent_id"], live["space_id"])
        return {"status": "session_claimed", "entry_id": entry["entry_id"], "session_file": str(session_file), "data_dir": str(data),
                "profile_reused": bool(preferences), "configuration_required": not configured,
                "template": template if not configured else None,
                "question_rules": QUESTION_RULES if not configured else {},
                "space_permissions_inherited": False,
                "next": "遵循 question_rules 按分支简短询问；可选项可跳过。一次摘要确认后保存并 start，再用 agent poll/ack 验证通道。"}

    def preferences(self):
        path = self.root / "preferences.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def remember(self, config):
        # Never copy recipients, projects, topics, private material or disclosure grants.
        write_json(self.root / "preferences.json", {"card": config["card"],
            "confirmation_mode": config["policy"]["confirmation_mode"], "answer_quota": config["policy"]["answer_quota"]})

    def overview(self):
        with self.db() as db:
            entries = [json.loads(x[0]) for x in db.execute("SELECT body FROM entries ORDER BY rowid DESC")]
        result = []
        for entry in entries:
            current = status(self.data(entry["entry_id"]))
            remote_space = current.get("space_id") or current.get("last_connection", {}).get("space_id")
            name = current.get("configuration", {}).get("space_name") or entry["space_name"]
            if (remote_space and entry["space_id"] != remote_space) or name != entry["space_name"]:
                entry["space_id"] = remote_space or entry["space_id"]
                entry["space_name"] = name
                with self.db() as db:
                    self.save(entry, db)
            result.append({"entry_id": entry["entry_id"], "space_name": entry["space_name"] or "尚未命名的空间",
                           "mode": entry["mode"], "space_id": entry["space_id"], "state": current})
        return {"product": PRODUCT, "profile_exists": self.preferences() is not None, "spaces": result}


def accept_file(path, *, registry, label):
    packet = parse_entry(Path(path).read_text(encoding="utf-8-sig"))
    if packet["kind"] != "join":
        registry = Path(packet["registry_path"])
        if not (registry / "hub.sqlite3").is_file():
            raise ValueError("这份配置码属于另一台电脑或已移动的本机目录；请从本机界面重新生成")
    return EntryHub(registry).accept(packet, label)


def load_session(path):
    value = json.loads(_unprotect(base64.b64decode(Path(path).read_bytes())))
    if not isinstance(value, dict) or set(value) != {"binding_id", "secret", "data_dir"}:
        raise ValueError("智能体会话文件格式无效")
    return value
