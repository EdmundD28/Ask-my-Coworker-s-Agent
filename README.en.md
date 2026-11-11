# Ask My Coworker's Agent

[简体中文](README.md) | **English**

Ask My Coworker's Agent enables agents owned by different people to collaborate directly in bilateral conversations, while keeping private context and control over information disclosure with each owner.

## Local product entry point

On the current machine, double-click the **Ask My Coworker's Agent** desktop shortcut or `打开 Ask My Coworker's Agent.vbs` in the project root. This entry point uses the unified `latest` workspace and reuses the existing “冒烟3” (smoke test 3) data. Select spaces, members, and conversation history on the left, and send messages on the right. Use “＋ 接入” (“Connect”) to generate a setup code; invitations, questions awaiting confirmation, and space settings are available at the top. When offline, click “连接空间” (“Connect to space”) first. After closing the browser, double-click the entry point again; there is no need to ask an agent to find the port.

## Agent-led conversational setup: test entry point

The current test build is always written to `dist/Ask My Coworker's Agent latest/`; separate version-numbered directories are no longer retained side by side. Double-click the program to open the local interface, click “生成配置码” (“Generate setup code”), and give the code to an agent with local file and command tools. Once setup is complete, the creator can generate a single-use invitation code bound to that space. The agent reads the program's built-in handbook; no model-provider API, dependency installation, or previous conversation context is required.

An existing local profile card and general preferences can be reused, but disclosure permissions for a new space must be confirmed separately. A setup code does not execute itself. A temporary public tunnel and registration are started only after the agent invokes `start` with its owner's authorization. Data and executables from older versions are excluded from the new test. Configuration persistence, communication connectivity, and session-challenge verification are reported separately. Agents can actively receive questions and reply, but the program cannot wake a closed model conversation. See [Decision 0011](docs/decisions/0011-ui-generated-entry-and-agent-session-binding.md) for the detailed boundaries.

The first version proves one essential interaction: owner A explicitly asks their agent to question agent B; B's agent searches for evidence within its own boundaries, then answers, reports that it does not know, identifies conflicts in disclosable material, or refuses because of its boundaries. The coordination space handles delivery, minimal shared state, and auditing. It does not gain permission to read B's private material. Automatic questions during reasoning are deferred to a later version.

See [Decision 0012](docs/decisions/0012-conditional-setup-and-network-recovery.md) for simplified setup and network recovery. Initial questions depend on the selected branch: per-question confirmation does not require a member ID, and an unlimited question allowance does not require a reset period. Optional settings are confirmed in a single summary. Registration records with an unknown outcome from older versions still require server-side reconciliation; they should not be deleted and recreated.

Version 4 has completed real space creation, joining by invitation, session challenges, and a synthetic question-and-answer round trip over the public network. The final portable program has also connected from a fresh directory. See the [live onboarding verification record](docs/SETUP_V4_TEST_RECEIPT.md). This evidence does not yet constitute remote acceptance testing by two real people.

## Current implementation status

The M0 local core slice passed 15 deterministic tests covering:

- Scenario-independent question-and-answer state and answer outcomes.
- Recipient-specific disclosure checks before answering.
- Missing, conflicting, and restricted material.
- Structured auditing that does not copy private source text that was never sent.
- Shared task snapshots and a development monitor that exclude bilateral message bodies.
- Third-party publication checks requiring approval from the relevant owners.
- Proposing, accepting, and auditing space-owner transfers.
- A replaceable communication interface and an ACN HTTP adapter.

M1 added SQLite persistence, independent answering processes, and a runnable local HTTP monitoring service. M2 added secure onboarding prerequisites and the first owner's live agent connection, including role cards, inbound-question rules, explicit owner-trigger markers, persistent replay protection, an A2A receiver, credentials encrypted for the current Windows user, Interfaze session replies, single-use member invitations, a Windows all-in-one member program, local message entry points on both sides, and the ACN private-network interface. The full suite currently contains 68 tests.

Live owner-side verification on 2026-09-16 demonstrated that Interfaze messages reached the local public receiver through ACN. The receiver exchanged credentials for a short-lived ACN agent token and posted a fixed status receipt back to the original session; the page displayed the reply automatically. The ACN server also confirmed endpoint reachability and a successful A2A handshake. The private space still contained only the creator's own agent, so this result did not establish cross-owner question-and-answer acceptance. The next stage requires a second real person to register their agent in an independent environment, accept the invitation, and complete a question-and-answer exchange through ACN.

## Relationship with ACN

Ask My Coworker's Agent selectively adopts mature architecture from `Agent-Collaboration-Network` (ACN), rather than copying the entire project:

- Separate communication boundaries for registration, discovery, and transport.
- An organization based on message envelopes, service layers, infrastructure adapters, and structured audits.
- An ACN adapter that follows the current `/api/v1/agents/join` and `/api/v1/communication/send` contracts.
- A product core that remains replaceable with respect to ACN, avoiding transport-provider lock-in.

The first version defines the product's minimum message contract; it does not require A2A as the product's communication protocol. ACN's A2A capabilities are adopted as a transport implementation only if they meet that contract, remote-delivery requirements, and onboarding simplicity requirements.

The central boundary is explicit: ACN determines whether a message can be delivered; Ask My Coworker's Agent determines what B may consult, what B may disclose to whom, and when a real person must be notified. Joining the same network does not authorize access to private context.

The current implementation references ACN's public interfaces and layered design without copying ACN source files into this repository. ACN uses Apache License 2.0. Any future direct port of implementation code must include the required license, attribution, and modification notices.

## Directory layout

```text
src/amca/                 General product core and adapters
tests/                    Independent initial behavior acceptance tests
docs/PRODUCT_ARCHITECTURE.md
docs/decisions/           Accepted architecture decisions
docs/IMPLEMENTATION_PLAN_V1.md
```

## Local acceptance tests

The project core requires only Python 3.12. The initial tests do not depend on external services:

```powershell
python -m unittest discover -s tests -v
```

If the current machine has no global `python` command, the existing ACN Python environment can be used temporarily:

```powershell
& 'D:\AI\myProject\Agent-Collaboration-Network\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

This command verifies local core behavior only. It does not connect to a live ACN service, call a real model, start a public endpoint, or prove a remote two-account workflow.

## M1 local runtime

M1 separates three types of data:

- Each answering process opens only its own private-material database.
- The coordination database stores only shared task projections, minimal audits, and space-governance state.
- The web monitor opens only the coordination database and does not load anyone's private-material database.

After installing the project, start the monitor locally:

```powershell
amca-monitor --database data/coordinator.sqlite3 --host 127.0.0.1 --port 8000
```

Or run it directly from source:

```powershell
$env:PYTHONPATH = 'src'
python -m amca.server --database data/coordinator.sqlite3 --host 127.0.0.1 --port 8000
```

The local page is `http://127.0.0.1:8000/`. It listens on the local machine by default. It has no account authentication yet, so changing the address to `0.0.0.0` does not make it a secure public service.

## M2 onboarding boundaries

In the first version, only the asking agent's owner may explicitly trigger a question. Transport messages must carry a version, request and task identifiers, question chain, sender, answering agent, project scope, question type, creation time, and replay identifier. Before reading private material or calling a model, the answering side checks:

- Whether the ACN-authenticated sender matches the message sender.
- Whether the message is addressed to this agent.
- Whether it has the explicit owner trigger allowed in the first version.
- Whether it has expired, is a duplicate, or exceeds the follow-up limit.
- Whether the questioner, topic, and project fall within the answering side's declared scope.
- Whether the answering side requires owner confirmation for every question.

Role cards may show responsibilities, supported question types, and owner-provided descriptions to space members. They must not expose private-material directories or specific disclosure rules. Role cards and the first version's owner-trigger field remain unverified self-declarations; they must not be presented as formal identity verification.

The first owner's agent has completed live ACN registration. Its API key is stored in `.amca/acn-owner.json`, encrypted through the Windows Data Protection API and bound to the current Windows user. Command output does not expose the key or the single-use claim link. The directory is ignored by Git.

The current public endpoint uses Cloudflare Quick Tunnel and is suitable only for temporary onboarding verification. Its random domain changes when the tunnel restarts. After a change, the endpoint registered with ACN must be updated; this is not a production deployment address.

Every member node with private-space credentials now provides the same local bilateral messaging page. The page opens automatically after an invitee joins; the creator-side receiver also opens it by default. Use `--no-open-chat` for unattended operation. Owners can select an answering agent from the current private-space members and initiate a structured question. Questions and returned results are written only to the local `local-chat.sqlite3`, never to the coordination database. The server filters conversations using the current node's identity; the browser cannot declare its own viewer identity.

The local messaging interface uses a high-entropy token generated when the node starts. The token appears only in the fragment of the automatically opened URL, so it is not sent with HTTP requests. The page stores it in the current tab's session storage and removes it from the address bar. A request without a token sees only a static page containing no identity, members, or message bodies. This prevents a port shared through Quick Tunnel from directly exposing the local messaging interface, but it is not formal account authentication or operating-system-level multi-user isolation.

Inspect the registered identity, private space, and members:

```powershell
$env:PYTHONPATH = 'src'
python -m amca.pilot inspect --credentials .amca/acn-owner.json
```

After the public entry point changes, ask ACN to probe and update the endpoint:

```powershell
$env:PYTHONPATH = 'src'
python -m amca.pilot verify-endpoint --credentials .amca/acn-owner.json
```

The space owner generates a single-use invitation code that expires within 24 hours and becomes invalid immediately after successful use:

```powershell
$env:PYTHONPATH = 'src'
python -m amca.pilot create-invite --join-url https://<owner-public-endpoint>/onboarding/join
```

When running from source, execute the following only once, after the invitee explicitly agrees to join. The program registers the invitee's agent, submits its identifier, accepts the space owner's invitation, and queries the member list for verification:

```powershell
$env:PYTHONPATH = 'src'
python -m amca.pilot join-invite --invite-code <invitation-code> --endpoint https://<invitee-public-endpoint>/a2a
```

The Windows all-in-one package for ordinary participants bundles the Python runtime, the member program, and `cloudflared` into one executable. The archive generated by the space owner also contains a single-use invitation and a double-click entry point. Invitees do not need to preinstall Python or create a public endpoint manually. The member program starts a temporary receiver and Quick Tunnel, then automatically completes registration, invitation processing, acceptance, verification, and local credential storage. If the new Quick Tunnel address has not yet propagated to ACN's DNS resolvers, the program waits and retries only that endpoint-resolution error, for up to three minutes.

```powershell
$env:PYTHONPATH = 'src'
python -m amca.pilot build-invite-package --member-exe dist/bin/AMCA-member.exe --join-url https://<owner-public-endpoint>/onboarding/join
```

The executable is not yet code-signed, and Quick Tunnel is not a stable deployment address. Final acceptance on a real second Windows device is still required. This is therefore a candidate for “one package, double-click, automatic onboarding”; it should not be described as a completed production-grade installation. An invitation establishes space membership only. It does not grant access to the other party's local material, credentials, or private context.

A structured answer returned by a current online direct delivery can immediately be written back to the questioner's local conversation. If ACN returns only an asynchronous inbox status, the page shows only delivery status. Inbox polling, delayed processing, and actively returning results to the original questioner are not yet implemented. “Entered the inbox” must not be displayed as “answered”. Requests for per-question confirmation can be displayed, but resuming processing after confirmation is also still unimplemented.

Ordinary Interfaze text messages are used only for owner-side connection checks. The receiver returns a visible online status; it does not automatically promote plain text into a cross-agent question, read private material, or call a model. Replies follow ACN's established boundaries: validate the session identifier and `/api/chats/{chat_id}/agent-messages` path strictly, exchange the long-lived ACN key for a short-lived agent token, then post the reply. The long-lived key never enters the browser, logs, or replies. Actual product questions still accept only versioned `DataPart` messages.

The public direct endpoint uses ACN's `open` transport mode so Interfaze and ACN can deliver messages proactively. Here, `open` refers only to the transport entry point; it does not authorize information disclosure. Inbound questions still pass the product's own admission checks before material retrieval or model invocation.
