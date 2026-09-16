from __future__ import annotations

import argparse
import json
import logging
import secrets
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import uuid4
from wsgiref.simple_server import make_server

from .admission import QuestionAdmissionService
from .application import CollaborationService
from .directory import (
    ConfirmationMode,
    FollowupScope,
    InboundQuestionPolicy,
    MemberCard,
    MemberProfile,
)
from .domain import AgentIdentity, QuestionRequest
from .infrastructure.memory import MemoryNotifier
from .infrastructure.sqlite import (
    SqliteAdmissionStore,
    SqliteAuditStore,
    SqliteEvidenceStore,
    SqliteTaskStore,
)
from .credentials import WindowsCredentialStore
from .chat_ui import CHAT_PAGE
from .interfaze import (
    DEFAULT_CHAT_API_BASE,
    InterfazeChatWriteback,
    extract_chat_envelope,
)
from .onboarding import OwnerOnboardingService, SqliteInviteStore
from .local_chat import LocalChatService, LocalConversationStore
from .transport import AcnNetworkAdapter
from .protocol import AdmissionStatus, QuestionMessageV1
from .runtime import DeterministicRuntime


MAX_REQUEST_BYTES = 64 * 1024
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiverResponse:
    status: int
    body: str
    content_type: str = "application/json; charset=utf-8"


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _a2a_result(request_id: Any, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "kind": "message",
            "messageId": str(uuid4()),
            "role": "agent",
            "parts": [{"kind": "data", "data": data}],
        },
    }


def _a2a_text_result(request_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "kind": "message",
            "messageId": str(uuid4()),
            "role": "agent",
            "parts": [{"kind": "text", "text": message}],
        },
    }


def _extract_interfaze_text(payload: dict[str, Any]) -> str | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    message = params.get("message")
    if not isinstance(message, dict):
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    agentplanet = metadata.get("agentplanet")
    if not isinstance(agentplanet, dict):
        return None
    if agentplanet.get("reply_channel") != "agentplanet.chat":
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("kind") == "text"
    ]
    text = "\n".join(item for item in texts if isinstance(item, str) and item.strip())
    return text or None


def _extract_question(payload: dict[str, Any]) -> QuestionMessageV1:
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    message = params.get("message")
    if not isinstance(message, dict):
        raise ValueError("params.message must be an object")
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise ValueError("message.parts must be an array")
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data":
            data = part.get("data")
            if isinstance(data, dict):
                return QuestionMessageV1.from_payload(data)
    raise ValueError("an AMCA data part is required")


class A2AReceiverApp:
    def __init__(
        self,
        *,
        profile: MemberProfile,
        owner_id: str,
        private_database,
        coordinator_database,
        runtime,
        clock,
        max_age: timedelta,
        chat_writeback=None,
        onboarding=None,
        local_chat=None,
        local_access_token: str | None = None,
    ):
        self.profile = profile
        self.owner_id = owner_id
        self.clock = clock
        self.evidence = SqliteEvidenceStore(
            private_database,
            owner_agent_id=profile.card.agent_id,
        )
        self.audit = SqliteAuditStore(coordinator_database)
        self.tasks = SqliteTaskStore(coordinator_database)
        self.admission_store = SqliteAdmissionStore(coordinator_database)
        self.admission = QuestionAdmissionService(
            store=self.admission_store,
            clock=clock,
            max_age=max_age,
        )
        self.chat_writeback = chat_writeback
        self.onboarding = onboarding
        self.local_chat = local_chat
        self.local_access_token = local_access_token
        self.agent_bridge = None
        self.collaboration = CollaborationService(
            evidence=self.evidence,
            runtime=runtime,
            audit=self.audit,
            tasks=self.tasks,
            notifier=MemoryNotifier(),
            clock=clock,
        )

    def close(self) -> None:
        self.evidence.close()
        self.audit.close()
        self.tasks.close()
        self.admission_store.close()
        if self.onboarding is not None:
            self.onboarding.store.close()
        if self.local_chat is not None:
            self.local_chat.close()

    def _local_authorized(self, headers: dict[str, str]) -> bool:
        supplied = headers.get("x-amca-local-token", "")
        return bool(
            self.local_access_token
            and supplied
            and secrets.compare_digest(self.local_access_token, supplied)
        )

    def _handle_local_chat(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> ReceiverResponse | None:
        if self.local_chat is None:
            return None
        if method == "GET" and path == "/":
            return ReceiverResponse(200, CHAT_PAGE, "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return None
        if not self._local_authorized(headers):
            return ReceiverResponse(403, json.dumps({"error": "local_access_denied"}))
        try:
            if method == "GET" and path == "/api/me":
                card = self.profile.card
                return ReceiverResponse(
                    200,
                    json.dumps(
                        {
                            "agent_id": card.agent_id,
                            "display_name": card.role_name,
                            "owner_claim": card.owner_claim,
                            "identity_verification": "unverified_self_claim",
                        },
                        ensure_ascii=False,
                    ),
                )
            if method == "GET" and path == "/api/members":
                return ReceiverResponse(
                    200, json.dumps(self.local_chat.members(), ensure_ascii=False)
                )
            if method == "GET" and path == "/api/conversations":
                return ReceiverResponse(
                    200,
                    json.dumps(self.local_chat.store.list_conversations(), ensure_ascii=False),
                )
            prefix, suffix = "/api/conversations/", "/messages"
            if method == "GET" and path.startswith(prefix) and path.endswith(suffix):
                conversation_id = path[len(prefix) : -len(suffix)]
                if not conversation_id or "/" in conversation_id:
                    raise ValueError("invalid conversation id")
                return ReceiverResponse(
                    200,
                    json.dumps(
                        self.local_chat.store.messages(conversation_id), ensure_ascii=False
                    ),
                )
            if method == "POST" and path == "/api/questions":
                if len(body) > MAX_REQUEST_BYTES:
                    return ReceiverResponse(413, json.dumps({"error": "request_too_large"}))
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                return ReceiverResponse(
                    201,
                    json.dumps(self.local_chat.send_question(payload), ensure_ascii=False),
                )
        except PermissionError as exc:
            return ReceiverResponse(403, json.dumps({"error": str(exc)}))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError) as exc:
            return ReceiverResponse(400, json.dumps({"error": str(exc)}))
        except Exception as exc:
            LOGGER.error("local chat request failed path=%s error=%s", path, type(exc).__name__)
            return ReceiverResponse(502, json.dumps({"error": "message_delivery_failed"}))
        return ReceiverResponse(404, json.dumps({"error": "not found"}))

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> ReceiverResponse:
        local_response = self._handle_local_chat(method, path, headers, body)
        if local_response is not None:
            return local_response
        if method == "GET" and path == "/health":
            return ReceiverResponse(
                200,
                json.dumps(
                    {
                        "status": "ok",
                        "agent_id": self.profile.card.agent_id,
                        "identity_verification": "unverified_self_claim",
                    }
                ),
            )
        if method == "POST" and path == "/onboarding/join":
            if self.onboarding is None:
                return ReceiverResponse(404, json.dumps({"error": "not found"}))
            if len(body) > MAX_REQUEST_BYTES:
                return ReceiverResponse(413, json.dumps({"error": "request_too_large"}))
            try:
                request = json.loads(body.decode("utf-8"))
                token = request["token"]
                agent_id = request["agent_id"]
                if not isinstance(token, str) or not isinstance(agent_id, str):
                    raise ValueError
                result = self.onboarding.admit(token, agent_id)
            except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return ReceiverResponse(400, json.dumps({"error": "invalid_join_request"}))
            except PermissionError as exc:
                return ReceiverResponse(403, json.dumps({"error": str(exc)}))
            return ReceiverResponse(200, json.dumps(result))
        if method != "POST" or path != "/a2a":
            return ReceiverResponse(404, json.dumps({"error": "not found"}))
        if len(body) > MAX_REQUEST_BYTES:
            return ReceiverResponse(413, json.dumps({"error": "request_too_large"}))

        request_id = None
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON-RPC body must be an object")
            request_id = payload.get("id")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return ReceiverResponse(
                400,
                json.dumps(_jsonrpc_error(request_id, -32700, "Parse error")),
            )

        if payload.get("jsonrpc") != "2.0":
            return ReceiverResponse(
                400,
                json.dumps(_jsonrpc_error(request_id, -32600, "Invalid Request")),
            )
        params = payload.get("params")
        inbound_message = params.get("message") if isinstance(params, dict) else None
        inbound_parts = inbound_message.get("parts") if isinstance(inbound_message, dict) else None
        part_kinds = [
            part.get("kind")
            for part in inbound_parts
            if isinstance(part, dict) and isinstance(part.get("kind"), str)
        ] if isinstance(inbound_parts, list) else []
        inbound_metadata = (
            inbound_message.get("metadata") if isinstance(inbound_message, dict) else None
        )
        inbound_agentplanet = (
            inbound_metadata.get("agentplanet") if isinstance(inbound_metadata, dict) else None
        )
        LOGGER.info(
            "A2A inbound method=%s part_kinds=%s agentplanet=%s reply_channel=%s",
            payload.get("method"),
            part_kinds,
            isinstance(inbound_agentplanet, dict),
            inbound_agentplanet.get("reply_channel")
            if isinstance(inbound_agentplanet, dict)
            else None,
        )
        if payload.get("method") not in {"message/send", "message/stream"}:
            return ReceiverResponse(
                200,
                json.dumps(_jsonrpc_error(request_id, -32601, "Method not found")),
            )

        interfaze_text = _extract_interfaze_text(payload)
        if interfaze_text is not None:
            # Interfaze owner-console smoke path. It deliberately does not turn
            # arbitrary chat text into an AMCA question, access private
            # evidence, or invoke a model. Cross-agent collaboration still
            # requires the versioned DataPart contract below.
            response_text = (
                "连接测试通过：AMCA 接收端在线。当前普通聊天只返回状态回执，"
                "不会读取私人资料或调用模型；跨智能体提问仍须使用所有者明确触发的结构化请求。"
            )
            envelope = extract_chat_envelope(payload)
            if self.chat_writeback is not None:
                if envelope is None:
                    LOGGER.error("Interfaze message rejected: invalid chat writeback envelope")
                else:
                    try:
                        self.chat_writeback.write(envelope, response_text)
                        LOGGER.info("Interfaze chat writeback succeeded chat_id=%s", envelope.chat_id)
                    except Exception as exc:
                        LOGGER.error(
                            "Interfaze chat writeback failed chat_id=%s error=%s",
                            envelope.chat_id,
                            type(exc).__name__,
                        )
            return ReceiverResponse(
                200,
                json.dumps(_a2a_text_result(request_id, response_text), ensure_ascii=False),
            )

        if self.agent_bridge is not None:
            try:
                parts = payload.get("params", {}).get("message", {}).get("parts", [])
                data = next((part["data"] for part in parts if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict)), None)
                if not data:
                    raise ValueError("需要结构化智能体消息")
                if data.get("schema") == "amca.agent-answer.v1":
                    result = self.agent_bridge.receive_answer(data)
                else:
                    result = self.agent_bridge.receive_question(QuestionMessageV1.from_payload(data), data)
                return ReceiverResponse(200, json.dumps(_a2a_result(request_id, result), ensure_ascii=False))
            except (ValueError, KeyError, TypeError, PermissionError) as exc:
                return ReceiverResponse(200, json.dumps(_jsonrpc_error(request_id, -32602, str(exc)), ensure_ascii=False))
            except Exception:
                return ReceiverResponse(502, json.dumps(_jsonrpc_error(request_id, -32000, "智能体消息处理暂未完成，请保留原请求重试"), ensure_ascii=False))

        try:
            question = _extract_question(payload)
        except (KeyError, TypeError, ValueError) as exc:
            return ReceiverResponse(
                200,
                json.dumps(_jsonrpc_error(request_id, -32602, f"Invalid params: {exc}")),
            )

        # ACN direct HTTP delivery currently provides no signed sender header.
        # For the v1 pilot this remains an explicitly unverified self-claim.
        decision = self.admission.admit(
            question,
            recipient=self.profile,
            authenticated_sender_id=question.from_agent,
        )
        if decision.status is not AdmissionStatus.ACCEPT:
            result = {
                "schema": "amca.admission.v1",
                "request_id": question.request_id,
                "status": decision.status.value,
                "reason_code": decision.reason_code,
                "transport_identity": "unverified_self_claim",
            }
            if self.local_chat is not None and decision.status is AdmissionStatus.ASK_HUMAN:
                self.local_chat.record_incoming_question(
                    question, status=decision.status.value
                )
                self.local_chat.record_local_answer(question, result)
            return ReceiverResponse(200, json.dumps(_a2a_result(request_id, result)))

        asker = AgentIdentity(
            agent_id=question.from_agent,
            owner_id=f"unverified:{question.from_agent}",
            display_name=question.from_agent,
            owner_claim="unverified self-claim",
            claim_verified=False,
        )
        card = self.profile.card
        responder = AgentIdentity(
            agent_id=card.agent_id,
            owner_id=self.owner_id,
            display_name=card.role_name,
            owner_claim=card.owner_claim,
            claim_verified=card.claim_verified,
        )
        answer = self.collaboration.respond(
            QuestionRequest(
                request_id=question.request_id,
                task_id=question.task_id,
                conversation_id=question.conversation_id,
                asker=asker,
                responder=responder,
                question=question.question,
                topic=question.topic,
                created_at=question.created_at,
            )
        )
        result = {
            "schema": "amca.answer.v1",
            "request_id": question.request_id,
            "status": answer.status.value,
            "answer": answer.answer,
            "reason": answer.reason,
            "generated_at": answer.generated_at.isoformat(),
            "evidence_refs": list(answer.evidence_refs),
            "transport_identity": "unverified_self_claim",
        }
        if self.local_chat is not None:
            self.local_chat.record_incoming_question(question, status="received")
            self.local_chat.record_local_answer(question, result)
        return ReceiverResponse(200, json.dumps(_a2a_result(request_id, result)))

    def __call__(self, environ, start_response):
        length_text = environ.get("CONTENT_LENGTH") or "0"
        try:
            length = int(length_text)
        except ValueError:
            length = MAX_REQUEST_BYTES + 1
        body = environ["wsgi.input"].read(min(length, MAX_REQUEST_BYTES + 1))
        headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        response = self.handle(
            environ.get("REQUEST_METHOD", "GET"),
            environ.get("PATH_INFO", "/"),
            headers,
            body,
        )
        payload = response.body.encode("utf-8")
        phrase = HTTPStatus(response.status).phrase
        start_response(
            f"{response.status} {phrase}",
            [
                ("Content-Type", response.content_type),
                ("Content-Length", str(len(payload))),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"),
            ],
        )
        return [payload]


def create_a2a_receiver(
    *,
    profile: MemberProfile,
    owner_id: str,
    private_database,
    coordinator_database,
    runtime,
    clock=lambda: datetime.now(UTC),
    max_age: timedelta = timedelta(minutes=5),
    chat_writeback=None,
    onboarding=None,
    outbound_adapter=None,
    subnet_slug: str | None = None,
    local_chat_database=None,
    local_access_token: str | None = None,
) -> A2AReceiverApp:
    local_chat = None
    if outbound_adapter is not None or subnet_slug is not None or local_chat_database is not None:
        if outbound_adapter is None or not subnet_slug or local_chat_database is None:
            raise ValueError("local chat requires adapter, subnet, and database")
        local_chat = LocalChatService(
            local_agent_id=profile.card.agent_id,
            subnet_slug=subnet_slug,
            adapter=outbound_adapter,
            store=LocalConversationStore(
                local_chat_database, local_agent_id=profile.card.agent_id
            ),
            clock=clock,
        )
    return A2AReceiverApp(
        profile=profile,
        owner_id=owner_id,
        private_database=private_database,
        coordinator_database=coordinator_database,
        runtime=runtime,
        clock=clock,
        max_age=max_age,
        chat_writeback=chat_writeback,
        onboarding=onboarding,
        local_chat=local_chat,
        local_access_token=local_access_token,
    )


def build_receiver_server(app, host: str = "127.0.0.1", port: int = 8001):
    return make_server(host, port, app)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AMCA A2A pilot receiver")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--owner-claim", required=True)
    parser.add_argument("--role-name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--private-db", required=True)
    parser.add_argument("--coordinator-db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--confirmation-mode", choices=[x.value for x in ConfirmationMode], default=ConfirmationMode.CONFIRM_EACH.value)
    parser.add_argument("--allow-asker", action="append", default=[])
    parser.add_argument("--allow-topic", action="append", default=[])
    parser.add_argument("--allow-project", action="append", default=[])
    parser.add_argument("--followup-limit", type=int)
    parser.add_argument("--followup-scope", choices=[x.value for x in FollowupScope], default=FollowupScope.CHAIN.value)
    parser.add_argument("--deterministic-answer", default="")
    parser.add_argument("--chat-writeback", action="store_true")
    parser.add_argument("--credentials", default=".amca/acn-owner.json")
    parser.add_argument("--chat-api-base", default=DEFAULT_CHAT_API_BASE)
    parser.add_argument("--log-file")
    parser.add_argument("--enable-onboarding", action="store_true")
    parser.add_argument("--no-open-chat", action="store_true")
    return parser


def main(argv=None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        filename=args.log_file,
        encoding="utf-8",
    )
    profile = MemberProfile(
        card=MemberCard(
            agent_id=args.agent_id,
            role_name=args.role_name,
            description=args.description,
            owner_claim=args.owner_claim,
        ),
        inbound_policy=InboundQuestionPolicy(
            confirmation_mode=ConfirmationMode(args.confirmation_mode),
            allowed_asker_ids=frozenset(args.allow_asker),
            allowed_topics=frozenset(args.allow_topic),
            allowed_project_scopes=frozenset(args.allow_project),
            followup_limit=args.followup_limit,
            followup_scope=FollowupScope(args.followup_scope),
        ),
    )
    chat_writeback = None
    onboarding = None
    outbound_adapter = None
    local_access_token = None
    credentials = None
    if Path(args.credentials).exists():
        saved_credentials = WindowsCredentialStore(args.credentials).load()
        if saved_credentials.agent_id != args.agent_id:
            if args.chat_writeback or args.enable_onboarding:
                raise ValueError(
                    "receiver agent id does not match saved ACN credentials"
                )
        else:
            credentials = saved_credentials
    if credentials is not None:
        if credentials.subnet_slug:
            outbound_adapter = AcnNetworkAdapter(
                credentials.base_url,
                agent_id=credentials.agent_id,
                api_key=credentials.api_key,
            )
            local_access_token = secrets.token_urlsafe(32)
    elif args.chat_writeback or args.enable_onboarding:
        raise FileNotFoundError("saved ACN credentials are required")
    if args.chat_writeback:
        chat_writeback = InterfazeChatWriteback(
            acn_base_url=credentials.base_url,
            chat_api_base=args.chat_api_base,
            agent_id=credentials.agent_id,
            api_key=credentials.api_key,
        )
    if args.enable_onboarding:
        if not credentials.subnet_slug:
            raise ValueError("owner credentials have no private subnet")
        onboarding = OwnerOnboardingService(
            SqliteInviteStore(args.coordinator_db),
            AcnNetworkAdapter(
                credentials.base_url,
                agent_id=credentials.agent_id,
                api_key=credentials.api_key,
            ),
        )
    app = create_a2a_receiver(
        profile=profile,
        owner_id=args.owner_id,
        private_database=args.private_db,
        coordinator_database=args.coordinator_db,
        runtime=DeterministicRuntime(args.deterministic_answer),
        chat_writeback=chat_writeback,
        onboarding=onboarding,
        outbound_adapter=outbound_adapter,
        subnet_slug=credentials.subnet_slug if outbound_adapter is not None else None,
        local_chat_database=(
            Path(args.coordinator_db).with_name("local-chat.sqlite3")
            if outbound_adapter is not None
            else None
        ),
        local_access_token=local_access_token,
    )
    server = build_receiver_server(app, args.host, args.port)
    LOGGER.info(
        "AMCA receiver started agent_id=%s chat_writeback=%s port=%s",
        args.agent_id,
        bool(chat_writeback),
        server.server_port,
    )
    print(f"AMCA receiver listening on http://{args.host}:{server.server_port}/a2a")
    if local_access_token is not None:
        local_chat_url = (
            f"http://127.0.0.1:{server.server_port}/#token={local_access_token}"
        )
        print(f"本地消息入口：{local_chat_url}")
        if not args.no_open_chat:
            webbrowser.open(local_chat_url, new=2)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
