from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class AgentRegistration:
    name: str
    description: str
    tags: tuple[str, ...]
    a2a_endpoint: str


@dataclass(frozen=True)
class AgentIdentityReceipt:
    agent_id: str
    api_key: str
    status: str | None = None
    claim_status: str | None = None
    claim_url: str | None = None
    referral_url: str | None = None
    endpoint_reachable: bool | None = None
    a2a_handshake_ok: bool | None = None


@dataclass(frozen=True)
class MessageEnvelope:
    message_id: str
    from_agent: str
    target_agent: str
    message_type: str
    body: dict


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: str
    status: str
    delivery_mode: str | None = None
    response: dict | None = None


class JsonHttpError(RuntimeError):
    def __init__(self, status: int | None, message: str, *, request_not_sent=False, failure_kind=None, validation_fields=None):
        super().__init__(message)
        self.status = status
        self.request_not_sent = request_not_sent
        self.failure_kind = failure_kind
        self.validation_fields = validation_fields or []


class UrllibJsonHttpClient:
    """Small dependency-free JSON client used by the ACN pilot adapter."""

    def __init__(self, *, timeout: float = 15.0, user_agent: str = "AMCA/0.1"):
        self.timeout = timeout
        self.user_agent = user_agent

    def request_json(self, method, url, *, headers=None, payload=None):
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            fields = []
            if exc.code == 422:
                try:
                    document = json.loads(raw)
                    details = document.get("detail", document.get("details", {}).get("pydantic_errors", []))
                    for detail in details if isinstance(details, list) else []:
                        loc = detail.get("loc", [])
                        if len(loc) == 2 and loc[0] == "body" and loc[1] in {"name", "description", "a2a_endpoint", "slug", "tags"}:
                            fields.append({"field": loc[1], "constraint": {k: v for k, v in detail.get("ctx", {}).items()
                                           if k in {"min_length", "max_length"} and type(v) is int}})
                except (ValueError, TypeError, AttributeError):
                    pass
            raise JsonHttpError(exc.code, raw[:1000], failure_kind="invalid_request" if exc.code == 422 else "http_error", validation_fields=fields) from exc
        except URLError as exc:
            reason = exc.reason
            # Only connection-establishment failures prove the HTTP request was not sent.
            # A timeout/reset can occur after a successful remote mutation.
            not_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError, PermissionError, ssl.SSLCertVerificationError))
            kind = ("network_permission_denied" if isinstance(reason, PermissionError) else
                    "dns_failed" if isinstance(reason, socket.gaierror) else
                    "connection_refused" if isinstance(reason, ConnectionRefusedError) else
                    "certificate_failed" if isinstance(reason, ssl.SSLCertVerificationError) else "response_uncertain")
            raise JsonHttpError(None, "网络请求失败", request_not_sent=not_sent, failure_kind=kind) from exc
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise JsonHttpError(None, "expected a JSON object response")
        return parsed


class AcnNetworkAdapter:
    def __init__(self, base_url: str, *, http=None, agent_id: str | None = None, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or UrllibJsonHttpClient()
        self.agent_id = agent_id
        self.api_key = api_key

    def register_agent(self, registration: AgentRegistration) -> AgentIdentityReceipt:
        # Product card fields may be brief/optional. ACN's registration schema is not.
        # Add neutral product context, never invent the owner's identity or abilities.
        name = registration.name
        description = registration.description
        if not name.strip() or len(name) > 100 or len(description) > 500:
            raise ValueError("名片名称需为1至100字符，职责说明最多500字符")
        if len(name) < 2:
            name = "Ask My Coworker's Agent: " + name
        if len(description) < 10:
            description = "Ask My Coworker's Agent member. " + description
        payload = {
            "name": name,
            "description": description,
            "tags": list(registration.tags),
            "a2a_endpoint": registration.a2a_endpoint,
            "delivery": "direct",
            "self_hosted": True,
            "communication_policy": {"mode": "open"},
        }
        data = self.http.request_json("POST", self.base_url + "/agents/join", payload=payload)
        self.agent_id, self.api_key = data["agent_id"], data["api_key"]
        return AgentIdentityReceipt(
            self.agent_id,
            self.api_key,
            status=data.get("status"),
            claim_status=data.get("claim_status"),
            claim_url=data.get("claim_url"),
            referral_url=data.get("referral_url"),
            endpoint_reachable=data.get("endpoint_reachable"),
            a2a_handshake_ok=data.get("a2a_handshake_ok"),
        )

    def _headers(self):
        if not self.api_key:
            raise ValueError("adapter is not registered")
        return {"Authorization": "Bearer " + self.api_key}

    def create_network(self, *, name, description, is_private=True):
        return self.http.request_json(
            "POST",
            self.base_url + "/subnets",
            headers=self._headers(),
            payload={
                "name": name,
                "description": description,
                "is_private": is_private,
                "join_policy": "approval",
            },
        )

    def invite(self, slug, agent_id, *, note=None):
        payload = {"agent_id": agent_id}
        if note is not None:
            payload["note"] = note
        return self.http.request_json(
            "POST",
            self.base_url + f"/subnets/{slug}/invitations",
            headers=self._headers(),
            payload=payload,
        )

    def accept_invitation(self, slug, invitation_id):
        return self.http.request_json("POST", self.base_url + f"/subnets/{slug}/invitations/{invitation_id}/accept", headers=self._headers(), payload={})

    def discover(self, slug):
        return self.http.request_json("GET", self.base_url + f"/subnets/{slug}/agents", headers=self._headers()).get("agents", [])

    def update_endpoint(self, endpoint: str):
        if not self.agent_id:
            raise ValueError("adapter is not registered")
        return self.http.request_json(
            "PATCH",
            self.base_url + f"/agents/{self.agent_id}/endpoint",
            headers=self._headers(),
            payload={"endpoint": endpoint},
        )

    def update_communication_policy(self, mode: str):
        if mode not in {"open", "closed", "manifest", "allowlist"}:
            raise ValueError("unsupported ACN communication policy mode")
        if not self.agent_id:
            raise ValueError("adapter is not registered")
        return self.http.request_json(
            "PATCH",
            self.base_url + f"/agents/{self.agent_id}/policy",
            headers=self._headers(),
            payload={"communication_policy": {"mode": mode}},
        )

    def communication_profile(self):
        if not self.agent_id:
            raise ValueError("adapter is not registered")
        return self.http.request_json(
            "GET",
            self.base_url + f"/agents/{self.agent_id}/communication_profile",
        )

    def leave(self, slug):
        return self.http.request_json("DELETE", self.base_url + f"/agents/{self.agent_id}/subnets/{slug}", headers=self._headers())

    def send(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        if not self.agent_id or not self.api_key:
            raise ValueError("adapter is not registered")
        payload = {
            "from_agent": envelope.from_agent,
            "target_agent": envelope.target_agent,
            "message_type": "inquiry",
            "message": {
                "role": "user",
                "parts": [{"kind": "data", "data": envelope.body}],
            },
        }
        data = self.http.request_json(
            "POST",
            self.base_url + "/communication/send",
            headers={"Authorization": "Bearer " + self.api_key},
            payload=payload,
        )
        response = data.get("response")
        if response is None and "result" in data:
            response = data
        return DeliveryReceipt(
            data.get("message_id", envelope.message_id),
            data.get("status", "delivered" if response is not None else "accepted"),
            data.get("delivery_mode"),
            response if isinstance(response, dict) else None,
        )
