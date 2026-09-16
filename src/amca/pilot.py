from __future__ import annotations

import argparse
import json
import time
from datetime import timedelta
from dataclasses import replace
from pathlib import Path

from .credentials import AcnCredentialBundle, WindowsCredentialStore
from .transport import AcnNetworkAdapter, AgentIdentityReceipt, AgentRegistration, JsonHttpError
from .onboarding import OwnerOnboardingService, SqliteInviteStore, join_from_invite


DEFAULT_ACN_BASE_URL = "https://api.acnlabs.dev/api/v1"


def connect_owner(
    *,
    endpoint: str,
    credential_path: str | Path,
    base_url: str = DEFAULT_ACN_BASE_URL,
    name: str = "Edmund AMCA Owner Agent",
    description: str = "Self-hosted AMCA agent representing Edmund in owner-triggered collaboration",
    network_name: str = "AMCA Private Pilot",
    network_description: str = "Private AMCA space for an owner-controlled cross-agent pilot",
) -> dict:
    store = WindowsCredentialStore(credential_path)
    if store.path.exists():
        raise FileExistsError(
            f"credential file already exists: {store.path}; refusing duplicate registration"
        )

    adapter = AcnNetworkAdapter(base_url)
    identity = adapter.register_agent(
        AgentRegistration(
            name=name,
            description=description,
            tags=("amca", "owner-controlled", "collaboration"),
            a2a_endpoint=endpoint,
        )
    )
    credentials = AcnCredentialBundle(
        base_url=base_url,
        agent_id=identity.agent_id,
        api_key=identity.api_key,
        a2a_endpoint=endpoint,
        claim_url=identity.claim_url,
        referral_url=identity.referral_url,
    )
    store.save(credentials)

    network = adapter.create_network(
        name=network_name,
        description=network_description,
        is_private=True,
    )
    subnet_slug = str(network["slug"])
    store.save(replace(credentials, subnet_slug=subnet_slug))
    return {
        "status": identity.status,
        "agent_id": identity.agent_id,
        "claim_status": identity.claim_status,
        "claim_required": identity.claim_status == "unclaimed",
        "referral_url": identity.referral_url,
        "endpoint_reachable": identity.endpoint_reachable,
        "a2a_handshake_ok": identity.a2a_handshake_ok,
        "communication_policy": "open",
        "delivery": "direct",
        "subnet_slug": subnet_slug,
        "subnet_private": not bool(network.get("is_public", False)),
        "join_policy": network.get("join_policy"),
        "credential_path": str(store.path.resolve()),
    }


def inspect_connection(credential_path: str | Path) -> dict:
    credentials = WindowsCredentialStore(credential_path).load()
    adapter = AcnNetworkAdapter(
        credentials.base_url,
        agent_id=credentials.agent_id,
        api_key=credentials.api_key,
    )
    members = adapter.discover(credentials.subnet_slug) if credentials.subnet_slug else []
    return {
        "agent_id": credentials.agent_id,
        "a2a_endpoint": credentials.a2a_endpoint,
        "subnet_slug": credentials.subnet_slug,
        "members": [item.get("agent_id") for item in members],
        "claim_required": credentials.claim_url is not None,
        "referral_url": credentials.referral_url,
        "credential_path": str(Path(credential_path).resolve()),
    }


def verify_endpoint(credential_path: str | Path) -> dict:
    credentials = WindowsCredentialStore(credential_path).load()
    adapter = AcnNetworkAdapter(
        credentials.base_url,
        agent_id=credentials.agent_id,
        api_key=credentials.api_key,
    )
    result = adapter.update_endpoint(credentials.a2a_endpoint)
    return {
        "agent_id": result.get("agent_id", credentials.agent_id),
        "a2a_endpoint": result.get("endpoint", credentials.a2a_endpoint),
        "endpoint_reachable": result.get("endpoint_reachable"),
        "a2a_handshake_ok": result.get("a2a_handshake_ok"),
        "communication_mode": result.get("communication_mode"),
    }


def invite_agent(
    credential_path: str | Path,
    *,
    invitee_agent_id: str,
    note: str | None = None,
) -> dict:
    credentials = WindowsCredentialStore(credential_path).load()
    if not credentials.subnet_slug:
        raise ValueError("the owner credential bundle has no private subnet")
    if invitee_agent_id == credentials.agent_id:
        raise ValueError("the owner agent is already a member")
    adapter = AcnNetworkAdapter(
        credentials.base_url,
        agent_id=credentials.agent_id,
        api_key=credentials.api_key,
    )
    result = adapter.invite(credentials.subnet_slug, invitee_agent_id, note=note)
    return {
        "subnet_slug": credentials.subnet_slug,
        "invitee_agent_id": invitee_agent_id,
        "invitation_id": result.get("invitation_id"),
        "status": result.get("status"),
    }


def enable_direct_chat(credential_path: str | Path) -> dict:
    credentials = WindowsCredentialStore(credential_path).load()
    adapter = AcnNetworkAdapter(
        credentials.base_url,
        agent_id=credentials.agent_id,
        api_key=credentials.api_key,
    )
    endpoint = adapter.update_endpoint(credentials.a2a_endpoint)
    if endpoint.get("endpoint_reachable") is not True:
        raise RuntimeError("ACN could not confirm the direct endpoint is reachable")
    if endpoint.get("a2a_handshake_ok") is not True:
        raise RuntimeError("ACN could not confirm the A2A handshake")
    adapter.update_communication_policy("open")
    profile = adapter.communication_profile()
    return {
        "agent_id": credentials.agent_id,
        "delivery": "direct",
        "endpoint_reachable": endpoint.get("endpoint_reachable"),
        "a2a_handshake_ok": endpoint.get("a2a_handshake_ok"),
        "communication_mode": profile.get("mode"),
        "unread_manifest_count": profile.get("unread_manifest_count"),
    }


def create_one_time_invite(
    credential_path: str | Path,
    *,
    coordinator_database: str | Path,
    join_url: str,
    lifetime_hours: int = 24,
) -> dict:
    credentials = WindowsCredentialStore(credential_path).load()
    if not credentials.subnet_slug:
        raise ValueError("the owner credential bundle has no private subnet")
    store = SqliteInviteStore(coordinator_database)
    try:
        service = OwnerOnboardingService(
            store,
            AcnNetworkAdapter(
                credentials.base_url,
                agent_id=credentials.agent_id,
                api_key=credentials.api_key,
            ),
        )
        receipt = service.create_invite(
            join_url=join_url,
            subnet_slug=credentials.subnet_slug,
            lifetime=timedelta(hours=lifetime_hours),
        )
    finally:
        store.close()
    return {
        "invite_code": receipt.invite_code,
        "expires_at": receipt.expires_at,
        "single_use": True,
    }


def join_one_time_invite(
    invite_code: str,
    *,
    endpoint: str,
    credential_path: str | Path,
    base_url: str = DEFAULT_ACN_BASE_URL,
    name: str = "AMCA Member Agent",
    description: str = "Owner-controlled AMCA member agent",
    registration_attempts: int = 10,
    registration_retry_seconds: float = 3.0,
    sleeper=time.sleep,
    on_registration_retry=None,
    expected_subnet=None,
) -> dict:
    store = WindowsCredentialStore(credential_path)
    existing_identity = None
    if store.path.exists():
        existing = store.load()
        if existing.base_url != base_url:
            raise ValueError("saved partial registration does not match this join request")
        adapter = AcnNetworkAdapter(
            base_url, agent_id=existing.agent_id, api_key=existing.api_key
        )
        for attempt in range(registration_attempts):
            try:
                probe = adapter.update_endpoint(endpoint)
                break
            except JsonHttpError as exc:
                if exc.status != 400 or "Endpoint host cannot be resolved" not in str(exc) or attempt + 1 >= registration_attempts:
                    raise
                if on_registration_retry is not None:
                    on_registration_retry(attempt + 1, registration_attempts)
                sleeper(registration_retry_seconds)
        else:
            raise RuntimeError("endpoint update did not complete")
        if probe.get("endpoint_reachable") is not True or probe.get("a2a_handshake_ok") is not True:
            raise RuntimeError("ACN has not confirmed the new endpoint and handshake; saved identity retained")
        store.save(replace(existing, a2a_endpoint=endpoint))
        if existing.subnet_slug:
            if expected_subnet is not None and existing.subnet_slug != expected_subnet:
                raise PermissionError("已有身份的空间与邀请码目标不一致")
            members = adapter.discover(existing.subnet_slug)
            if existing.agent_id not in {member.get("agent_id") for member in members}:
                raise RuntimeError("Saved agent membership was not confirmed; no new registration performed")
            return {
                "status": "resumed", "agent_id": existing.agent_id,
                "subnet_slug": existing.subnet_slug,
                "endpoint_reachable": True, "a2a_handshake_ok": True,
                "credential_path": str(store.path.resolve()),
            }
        existing_identity = AgentIdentityReceipt(
            existing.agent_id,
            existing.api_key,
            claim_url=existing.claim_url,
            referral_url=existing.referral_url,
        )
    else:
        adapter = AcnNetworkAdapter(base_url)

    def save_partial(identity):
        store.save(
            AcnCredentialBundle(
                base_url=base_url,
                agent_id=identity.agent_id,
                api_key=identity.api_key,
                a2a_endpoint=endpoint,
                claim_url=identity.claim_url,
                referral_url=identity.referral_url,
            )
        )

    registration = AgentRegistration(
        name=name,
        description=description,
        tags=("amca", "owner-controlled", "collaboration"),
        a2a_endpoint=endpoint,
    )
    if existing_identity is None:
        for attempt in range(registration_attempts):
            try:
                existing_identity = adapter.register_agent(registration)
                save_partial(existing_identity)
                break
            except JsonHttpError as exc:
                transient_dns = exc.status == 400 and (
                    "Endpoint host cannot be resolved" in str(exc)
                    or "not in an allowed address range" in str(exc)
                )
                if not transient_dns or attempt + 1 >= registration_attempts:
                    raise
                if on_registration_retry is not None:
                    on_registration_retry(attempt + 1, registration_attempts)
                sleeper(registration_retry_seconds)
        if existing_identity is None:
            raise RuntimeError("agent registration did not complete")

    identity, receipt = join_from_invite(
        invite_code,
        adapter=adapter,
        registration=registration,
        existing_identity=existing_identity,
        expected_subnet=expected_subnet,
    )
    store.save(
        AcnCredentialBundle(
            base_url=base_url,
            agent_id=identity.agent_id,
            api_key=identity.api_key,
            a2a_endpoint=endpoint,
            claim_url=identity.claim_url,
            referral_url=identity.referral_url,
            subnet_slug=receipt["subnet_slug"],
        )
    )
    return {
        "status": "joined",
        "agent_id": identity.agent_id,
        "subnet_slug": receipt["subnet_slug"],
        "endpoint_reachable": identity.endpoint_reachable,
        "a2a_handshake_ok": identity.a2a_handshake_ok,
        "credential_path": str(store.path.resolve()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the first real AMCA/ACN pilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect-owner", help="register the owner agent and create its private space")
    connect.add_argument("--endpoint", required=True)
    connect.add_argument("--credentials", default=".amca/acn-owner.json")
    connect.add_argument("--base-url", default=DEFAULT_ACN_BASE_URL)
    connect.add_argument("--name", default="Edmund AMCA Owner Agent")
    connect.add_argument(
        "--description",
        default="Self-hosted AMCA agent representing Edmund in owner-triggered collaboration",
    )
    connect.add_argument("--network-name", default="AMCA Private Pilot")
    connect.add_argument(
        "--network-description",
        default="Private AMCA space for an owner-controlled cross-agent pilot",
    )

    inspect = subparsers.add_parser("inspect", help="verify the saved private-space membership")
    inspect.add_argument("--credentials", default=".amca/acn-owner.json")

    verify = subparsers.add_parser("verify-endpoint", help="ask ACN to probe the saved A2A endpoint again")
    verify.add_argument("--credentials", default=".amca/acn-owner.json")

    invite = subparsers.add_parser("invite", help="invite an already-registered agent to the private space")
    invite.add_argument("--credentials", default=".amca/acn-owner.json")
    invite.add_argument("--agent-id", required=True)
    invite.add_argument("--note")

    direct = subparsers.add_parser("enable-direct-chat", help="verify the endpoint and switch ACN reception to direct push")
    direct.add_argument("--credentials", default=".amca/acn-owner.json")

    create_invite = subparsers.add_parser("create-invite", help="create a single-use AMCA onboarding code")
    create_invite.add_argument("--credentials", default=".amca/acn-owner.json")
    create_invite.add_argument("--coordinator-db", default=".amca/coordinator.db")
    create_invite.add_argument("--join-url", required=True)
    create_invite.add_argument("--lifetime-hours", type=int, default=24)

    join_invite = subparsers.add_parser("join-invite", help="register, accept, and verify a one-time AMCA invitation")
    join_invite.add_argument("--invite-code", required=True)
    join_invite.add_argument("--endpoint", required=True)
    join_invite.add_argument("--credentials", default=".amca/acn-member.json")
    join_invite.add_argument("--base-url", default=DEFAULT_ACN_BASE_URL)
    join_invite.add_argument("--name", default="AMCA Member Agent")
    join_invite.add_argument("--description", default="Owner-controlled AMCA member agent")

    package = subparsers.add_parser("build-invite-package", help="create one shareable Windows invitation package")
    package.add_argument("--member-exe", required=True)
    package.add_argument("--output", default="dist/invitations")
    package.add_argument("--credentials", default=".amca/acn-owner.json")
    package.add_argument("--coordinator-db", default=".amca/coordinator.db")
    package.add_argument("--join-url", required=True)
    package.add_argument("--lifetime-hours", type=int, default=24)

    args = parser.parse_args()
    if args.command == "connect-owner":
        result = connect_owner(
            endpoint=args.endpoint,
            credential_path=args.credentials,
            base_url=args.base_url,
            name=args.name,
            description=args.description,
            network_name=args.network_name,
            network_description=args.network_description,
        )
    elif args.command == "inspect":
        result = inspect_connection(args.credentials)
    elif args.command == "verify-endpoint":
        result = verify_endpoint(args.credentials)
    elif args.command == "invite":
        result = invite_agent(
            args.credentials,
            invitee_agent_id=args.agent_id,
            note=args.note,
        )
    elif args.command == "enable-direct-chat":
        result = enable_direct_chat(args.credentials)
    elif args.command == "create-invite":
        result = create_one_time_invite(
            args.credentials,
            coordinator_database=args.coordinator_db,
            join_url=args.join_url,
            lifetime_hours=args.lifetime_hours,
        )
    elif args.command == "join-invite":
        result = join_one_time_invite(
            args.invite_code,
            endpoint=args.endpoint,
            credential_path=args.credentials,
            base_url=args.base_url,
            name=args.name,
            description=args.description,
        )
    else:
        from .invite_package import build_invite_package

        result = build_invite_package(
            member_executable=args.member_exe,
            output_directory=args.output,
            credential_path=args.credentials,
            coordinator_database=args.coordinator_db,
            join_url=args.join_url,
            lifetime_hours=args.lifetime_hours,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
