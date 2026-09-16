"""Provider-independent configuration contract. No network or model calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

PRODUCT = "Ask My Coworker's Agent"
SCHEMA = "ask-my-coworkers-agent.setup.v1"


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label} 必须包含且仅包含字段：{', '.join(keys)}")


def _text(value, label, maximum=2000, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise ValueError(f"{label} 必须是非空文本，最多 {maximum} 字符")
    if any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(f"{label} 含非法控制字符")
    if "待用户填写" in value:
        raise ValueError(f"{label} 仍含模板占位内容，必须先询问用户")


def _strings(value, label):
    if not isinstance(value, list) or len(value) > 100 or len({str(x) for x in value}) != len(value):
        raise ValueError(f"{label} 必须是最多 100 项、无重复的文本数组")
    for item in value:
        _text(item, label, 500)
        if item == "*":
            raise ValueError(f"{label} 不支持通配符；请明确指定或保留空数组")


def validate_config(value):
    _object(value, ["schema", "mode", "space_name", "invite_code", "card", "policy"], "配置")
    if value["schema"] != SCHEMA or value["mode"] not in {"create", "join"}:
        raise ValueError("配置版本或模式不支持")
    _text(value["space_name"], "space_name", 120, empty=value["mode"] == "join")
    _text(value["invite_code"], "invite_code", 12000, empty=value["mode"] == "create")
    if value["mode"] == "create" and value["invite_code"]:
        raise ValueError("创建空间不能同时提供邀请码")
    if value["mode"] == "join":
        from .onboarding import _decode
        try:
            invitation = _decode(value["invite_code"])
            parsed = urlparse(invitation["join_url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError
            _text(invitation["token"], "邀请令牌", 500)
        except Exception as exc:
            raise ValueError("邀请码格式无效，需要群主提供有效的 HTTPS 一次性邀请") from exc
    card = value["card"]
    _object(card, ["role_name", "description", "owner_claim"], "card")
    for key in card:
        _text(card[key], "card." + key, {"role_name": 100, "description": 500, "owner_claim": 1000}[key])
    policy = value["policy"]
    _object(policy, ["confirmation_mode", "allowed_asker_ids", "allowed_topics", "allowed_project_scopes", "answer_quota", "disclosure"], "policy")
    if policy["confirmation_mode"] not in {"confirm_each", "preauthorized"}:
        raise ValueError("confirmation_mode 必须为 confirm_each 或 preauthorized")
    for key in ("allowed_asker_ids", "allowed_topics", "allowed_project_scopes"):
        _strings(policy[key], key)
    quota = policy["answer_quota"]
    _object(quota, ["limit", "scope", "window_seconds"], "answer_quota")
    if quota["limit"] is not None and (type(quota["limit"]) is not int or not 0 <= quota["limit"] <= 1000000):
        raise ValueError("limit 必须为 0 至 1000000 的整数，或 null 表示不限")
    if quota["scope"] not in {"chain", "sender", "total"}:
        raise ValueError("scope 必须为 chain、sender 或 total")
    if quota["window_seconds"] is not None and (type(quota["window_seconds"]) is not int or not 60 <= quota["window_seconds"] <= 31536000):
        raise ValueError("window_seconds 必须为 60 至 31536000 的整数，或 null 表示不自动重置")
    disclosure = policy["disclosure"]
    _object(disclosure, ["allow", "deny"], "disclosure")
    _strings(disclosure["allow"], "disclosure.allow")
    _strings(disclosure["deny"], "disclosure.deny")
    return value


def config_digest(value):
    validate_config(value)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("配置文件超过 64 KiB")
    return validate_config(json.loads(path.read_text(encoding="utf-8-sig")))


def public_config(value):
    result = json.loads(json.dumps(value))
    result["invite_code"] = "[已隐藏]" if result["invite_code"] else ""
    return result


def contract():
    return {
        "product": PRODUCT,
        "schema": SCHEMA,
        "platform": "Windows x64；智能体需要本机命令执行和文件读写工具；无需模型供应商密钥",
        "required_questions": [
            "首次且未提供名称：你的智能体叫什么？职责和代表谁可以跳过。",
            "建议每次回答先问你，也可选择预授权。是否采用？",
        ],
        "question_rules": QUESTION_RULES,
        "template": {
            "schema": SCHEMA, "mode": "create", "space_name": "待用户填写", "invite_code": "",
            "card": {"role_name": "待用户填写", "description": "未填写", "owner_claim": "未声明"},
            "policy": {"confirmation_mode": "confirm_each", "allowed_asker_ids": [], "allowed_topics": [], "allowed_project_scopes": [],
                       "answer_quota": {"limit": None, "scope": "total", "window_seconds": None},
                       "disclosure": {"allow": [], "deny": []}},
        },
        "semantics": {
            "template": "可选项采用公开的保守建议，提交摘要中说明并取得一次确认；未回答不是同意。名称占位必须替换。",
            "empty_lists": "这些列表只控制预授权，不控制空间成员提问资格。逐次确认时全部留空，已加入成员仍可提交待确认问题；非成员拒绝。没有通配符。",
            "answer_quota": "本人选择的处理上限；绑定智能体通道后，问题进入处理队列即预留一次额度，未知与拒绝也计入。",
            "disclosure": "保存给本人智能体遵守的披露规则，不能仅凭文字配置宣称内容披露判断已实现。",
            "runtime": "界面生成的接入码可绑定主动收取的智能体通道；按实际策略预授权或请本人批准，不能唤醒关闭的模型对话。旧版未绑定节点仍统一要求本人介入。",
            "consent": "由智能体展示摘要并询问本人；摘要哈希绑定提交内容，不是独立的人类身份认证。",
        },
        "commands": ["ui", "guide", "accept --file <界面生成的接入码> --label <当前对话名称>", "describe", "validate --file <JSON>", "apply --file <JSON> --confirmed-digest <SHA256>", "start", "status", "diagnose", "stop", "members", "agent poll --session-file <凭证文件>", "agent ack --session-file <凭证文件> --file <挑战回复JSON>", "agent reply --session-file <凭证文件> --file <回答JSON>"],
    }


QUESTION_RULES = {
    "presentation": "一次最多两个短问题；只追问当前分支缺失的信息，不把字段表发给用户。",
    "target": "创建或加入已由码决定，不再询问。创建且名称为空时可建议‘我的协作空间’，摘要确认；加入不能改目标。",
    "card": "复用已有名片；首次只必填角色名称。职责及代表谁可跳过，保存未填写/未声明，不编造身份。",
    "confirm_each": "不问成员编号、话题白名单或项目白名单，三项保留空数组。仅已加入成员能提问，回答仍须逐次批准。",
    "quota": "可选，摘要建议不限；选择不限或0时不问计数范围、周期，写total/null。选择正整数才追问按谁计数及是否重置。",
    "disclosure": "逐次确认可跳过披露配置，空列表不授予披露权；实际回答前仍需明确本次允许披露什么。用户已给范围则保留。",
    "preauthorized": "仅选择预授权才询问成员、话题、项目和披露范围。成员未知不阻塞联网：说明先逐次确认，联网后选择真实成员再配置；须本人确认，不擅自降级。",
    "confirmation": "用不超过五行摘要合并确认建议与用户选择，一次确认后直接写入连接，不再层层确认。",
}
