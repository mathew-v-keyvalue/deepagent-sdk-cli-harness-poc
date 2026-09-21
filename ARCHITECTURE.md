# Architecture — cybersierra CLI + DeepAgents SDK POC

Diagrams of what's actually built in this repo (see `README.md` for the
full explanation and verification evidence behind each piece). Five views:
system components, one `/chat` turn end to end, the two-layer shell
sandbox, how the real `cyber-sierra` skill pipeline maps onto DeepAgents
primitives, and the access-token decision that was fixed after the
"already logged in" question.

## 1. System components

```mermaid
flowchart TB
    FE["frontend/index.html<br/>vanilla JS · SSE client<br/>(copied byte-identical from the sibling Claude SDK POC)"]

    subgraph SRV["FastAPI process — server/app.py"]
        API["POST /chat · GET /health"]
        SESS["server/sessions.py<br/>SessionStore: session_id → lock + metadata<br/>(no transcript)"]
        LOGCFG["harness/observability.py<br/>configure_logging()"]
    end

    subgraph HARNESS["harness/agent.py — _build_agent(access_token)"]
        MODELPY["harness/model.py<br/>resolve_model()<br/>AGENT_MODEL=provider:model"]
        SANDBOXPY["harness/sandbox.py<br/>AllowlistedShellBackend<br/>ShellSandboxMiddleware"]
        EXECPY["harness/executor_tool.py<br/>run_execution_plan tool"]
        CKPT[("InMemorySaver checkpointer<br/>one per process, keyed by thread_id")]
    end

    subgraph GRAPH["create_deep_agent(...) — compiled LangGraph graph, built fresh per call"]
        SKILLSMW["SkillsMiddleware<br/>discovers skills/cyber-sierra/SKILL.md"]
        NMODEL["model node"]
        NTOOLS["tools node<br/>execute · read_file · write_file · edit_file<br/>glob · grep · ls · task · run_execution_plan"]
    end

    SKILLSDIR["skills/cyber-sierra/<br/>verbatim copy of the real plugin skill<br/>SKILL.md · _internal/ · _generated/index.json"]

    CLIBIN["real cybersierra CLI (subprocess)<br/>cybersierra module resource action"]
    BACKEND[("cybersierra backend API<br/>morpheus-api.prod.cybersierra.ai")]

    FE -- "message, session_id?, access_token" --> API
    API --> SESS
    API -- "stream()/run()" --> HARNESS
    HARNESS -- builds --> GRAPH
    MODELPY --> NMODEL
    SANDBOXPY --> NTOOLS
    EXECPY --> NTOOLS
    CKPT -.->|"config={thread_id}"| GRAPH
    SKILLSMW -.->|"read_file"| SKILLSDIR
    NMODEL <--> NTOOLS
    NTOOLS -- "execute tool call" --> SANDBOXPY
    SANDBOXPY -- "allowed command only" --> CLIBIN
    CLIBIN --> BACKEND
    LOGCFG -.->|"turn_* / skills_available"| HARNESS
    LOGCFG -.->|"cli_call_* / tool_call_* / skill_loaded"| SANDBOXPY
    LOGCFG -.->|"plan_* "| EXECPY
    API -- "SSE: session / delta / tool_use / done / error" --> FE
```

## 2. One `/chat` turn, end to end

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser (frontend/index.html)
    participant S as FastAPI (server/app.py)
    participant St as SessionStore (server/sessions.py)
    participant H as harness.agent.stream()
    participant G as DeepAgents graph (LangGraph)
    participant M as Chat model (Anthropic/OpenAI)
    participant Mw as ShellSandboxMiddleware
    participant B as AllowlistedShellBackend
    participant C as real cybersierra CLI

    U->>S: POST /chat (message, session_id?, access_token optional)
    alt session_id unknown to SessionStore
        S-->>U: 404 unknown_session
    else session already mid-turn
        S-->>U: 409 session_busy
    end
    S->>St: create()/get() then lock.acquire()
    S->>H: stream(prompt, access_token or "", session_id | resume)
    H->>H: _build_agent(access_token)<br/>fresh backend + middleware; shared checkpointer
    H->>G: astream_events(HumanMessage, config={thread_id})
    G->>M: model node (system prompt + skills catalogue in context)
    M-->>G: AIMessage with tool_calls, e.g. execute("cybersierra ...")
    G->>Mw: awrap_tool_call(request)
    Mw->>Mw: is_command_allowed(command)?
    alt allowed
        Mw->>B: handler(request) → backend.execute(command)
        B->>C: subprocess.run (real CLI)
        C-->>B: stdout / stderr / exit_code
        B-->>Mw: ExecuteResponse
    else denied
        Mw-->>G: ToolMessage(status="error") — handler never called
    end
    G->>M: model node again, with the tool result in context
    M-->>G: streamed AIMessageChunk deltas
    G-->>H: on_chat_model_stream / on_tool_start / on_chat_model_end events
    H-->>S: TextDelta / ToolUseStarted / Done (harness event vocabulary)
    S-->>U: SSE: delta* / tool_use* / done
    S->>St: lock.release()
```

## 3. Shell sandbox — two independent enforcement layers

```mermaid
flowchart TD
    TC["Model emits a tool_call<br/>e.g. execute(command)"]

    TC --> L2{"Layer 2 — ShellSandboxMiddleware<br/>awrap_tool_call / wrap_tool_call<br/>(harness/sandbox.py)"}
    L2 -- "command not in ALLOWED_COMMAND_PREFIXES<br/>or tool not in the known-safe set" --> DENY2["ToolMessage(status='error')<br/>handler() never called<br/>logged: tool_call_denied"]
    L2 -- allowed --> L1{"Layer 1 — AllowlistedShellBackend.execute<br/>(harness/sandbox.py)"}
    L1 -- "command not in ALLOWED_COMMAND_PREFIXES" --> DENY1["ExecuteResponse(exit_code=126)<br/>subprocess.run never called<br/>logged: cli_call_denied"]
    L1 -- allowed --> RUN["subprocess.run(command)<br/>real cybersierra / npm / npx / python3<br/>logged: cli_call_start → cli_call_done"]

    NOTE1["Both layers check the SAME allowlist independently.<br/>Layer 1 is trusted even if layer 2 were ever silently<br/>bypassed — the way the sibling Claude SDK POC found<br/>can_use_tool silently skipped for Bash."]
    NOTE2["DeepAgents' own LocalShellBackend ships with NO<br/>allowlist of its own; SKILL.md's 'allowed-tools' frontmatter<br/>is descriptive-only in both SDKs — neither layer here<br/>came for free."]

    L1 -.-> NOTE1
    L2 -.-> NOTE2
```

## 4. Real `cyber-sierra` pipeline → DeepAgents primitives

The 7-phase pipeline in `skills/cyber-sierra/SKILL.md` (Initialize → Route
→ Plan → Present-Plan-&-Confirm → Execute → Reflect-&-Learn → Report) was
ported by mapping each phase onto whichever DeepAgents primitive actually
fit, not by forcing one subagent per phase — see `README.md` "Porting the
real pipeline" for the reasoning behind each choice below.

```mermaid
flowchart LR
    subgraph SOURCE["Real skill pipeline (skills/cyber-sierra/)"]
        P1["Initialize + Route<br/>SKILL.md"]
        P2["Plan: Skill Resolver<br/>or Planner<br/>_internal/planner/"]
        P3["Present Plan & Confirm"]
        P4["Execute<br/>(deterministic runtime logic,<br/>per _internal/shared/contracts.md)"]
        P5["Reflect & Learn<br/>_internal/reflection/"]
        P6["Report"]
    end

    subgraph IMPL["How this port implements it"]
        I1["Single continuously-reasoning agent,<br/>SkillsMiddleware progressive disclosure<br/>(same Agent Skills spec as Claude Code)"]
        I2["Same agent, reading<br/>_internal/planner/SKILL.md + references<br/>on demand via read_file"]
        I3["Ordinary conversational turn —<br/>model states the plan and stops;<br/>next /chat call = approval or rejection<br/>(NOT interrupt_on — see README)"]
        I4["harness/executor_tool.py:<br/>run_execution_plan — plain Python,<br/>not an LLM subagent or subgraph"]
        I5["Same agent, reading<br/>_internal/reflection/SKILL.md,<br/>writing _generated/&lt;name&gt;/SKILL.md<br/>+ metadata.json via write_file"]
        I6["Same agent's final answer<br/>in the chat turn"]
    end

    P1 --> I1
    P2 --> I2
    P3 --> I3
    P4 --> I4
    P5 --> I5
    P6 --> I6

    GAP["R4 gap reproduced on purpose:<br/>no DB, no tenant scoping,<br/>no approval gate on the persist step —<br/>same as the real system today"]
    I5 -.-> GAP
```

## 5. Access-token decision (default vs. injection mode)

Two sequential fixes, each caught by a direct question, not by testing:
(1) unconditionally injecting `access_token` into the subprocess env
overrode an already-authenticated `cybersierra auth login-browser` session
with a UI placeholder, since the CLI's `??` fallback only triggers on a
*missing* env var, not a wrong one — fixed by making injection opt-in;
(2) even opt-in, `access_token` was still a *required* `/chat` field for
no real reason in this POC's scope — fixed by dropping the requirement
entirely (`400 no_token` removed).

**Correction:** the diagram and prose below originally named the override
variable `CYBERSIERRA_TOKEN`. That was never correct — the real installed
CLI binary contains zero references to that string; the actual variable it
reads is `MORPHEUS_TOKEN` (confirmed by reading the binary's own
profile-resolution code and reproducing live — see README "Authentication
model" for the full correction). Fixed below.

**Second correction, added alongside the first:** the diagram below
originally implied `MORPHEUS_TOKEN` injection was the *only* thing needed
for a real deployment. It isn't — `MORPHEUS_BASE_URL` also has to resolve,
and nothing previously forwarded it to the subprocess at all (this repo's
own `CYBERSIERRA_BASE_URL` was dead config). Unlike token injection, this
is deployment-wide, not per-request, so it's injected unconditionally,
independent of the `CYBERSIERRA_INJECT_ACCESS_TOKEN` branch entirely — see
the new `BASEURL` node below and README "Authentication model" (Fix 3) for
the full story.

```mermaid
flowchart TD
    START["_build_agent(access_token='' by default) builds the subprocess env"]
    BASEURL["MORPHEUS_BASE_URL = CYBERSIERRA_BASE_URL,<br/>injected unconditionally if set --<br/>independent of the branch below,<br/>same for every user/request"]
    CHECK{"CYBERSIERRA_INJECT_ACCESS_TOKEN set?"}
    START --> BASEURL
    START --> CHECK

    CHECK -- "unset (default)" --> DEFAULT["MORPHEUS_TOKEN key NOT added to env<br/>CLI's own token ?? persistedProfile.token<br/>resolves to the persisted profile"]
    DEFAULT --> ASSUME["Matches 'assume already authenticated'<br/>— one-time cybersierra auth login-browser<br/>covers every /chat call.<br/>access_token is fully OPTIONAL now<br/>(400 no_token removed — see README)."]

    CHECK -- "=1 (opt-in)" --> INJECT["MORPHEUS_TOKEN = access_token<br/>added to the subprocess env,<br/>only if access_token is non-empty"]
    INJECT --> REAL["CLI uses this value, overriding<br/>whatever profile is on disk.<br/>access_token MUST be a real per-user JWT<br/>or every cybersierra call fails."]
    REAL --> SHAPE["The mechanism a real shape-B/C<br/>multi-tenant deployment would need —<br/>exercised by<br/>verify_server_multi_session_isolation.py.<br/>Combined with BASEURL above, needs<br/>zero persisted profile on any host."]
```
