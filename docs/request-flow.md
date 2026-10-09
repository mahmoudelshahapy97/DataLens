# Chat request flow: what happens when a question is submitted

> **Viewing the diagrams:** GitHub renders the Mermaid blocks below natively. VS Code's built-in preview does not; install the *Markdown Preview Mermaid Support* extension (`bierner.markdown-mermaid`), or open [request-flow.html](request-flow.html) in a browser, which renders both diagrams standalone.

## Summary

1. **Browser**: `<vanna-chat>` POSTs to `/api/vanna/v2/chat_sse` with the session cookie (`frontend/src/services/api-client.ts`, `streamChat`).
2. **FastAPI route**: `chat_sse` (`backend/vanna/servers/fastapi/routes.py`) builds a `RequestContext` and runs the handler in a background task. Chunks go over a queue, with keepalive comments and a final `data: [DONE]`.
3. **Tenant dispatch**: `TenantDispatchChatHandler` (`backend/vanna_app/wiring.py`) resolves the user, requires a full session, picks the tenant and data-source runtime, and optionally swaps in the user's own LLM key.
4. **Agent**: `Agent._send_message` (`backend/vanna/core/agent/agent.py`) runs hooks, loads the conversation, tries the workflow handler (slash commands), builds the tool context, tool schemas and system prompt, then runs the turn graph.
5. **Turn graph**: `llm_turn -> tools -> llm_turn ... -> answer`, stopping early with a warning card if `max_tool_iterations` is reached. Optional `PlannerNode` and `CriticNode` live in `backend/vanna_app/agent_nodes.py`.
6. **Response**: every `UiComponent` becomes a `ChatStreamChunk` (create and update frames) as soon as it is produced, and is streamed back as SSE while the turn is still running. The browser renders status, tasks, text and tables or charts live.

## Flowchart

Solid arrows are control flow. Dotted arrows are UI output that is streamed to the browser as it happens.

```mermaid
flowchart TD
    A["User submits question<br/>vanna-chat / AskPage.tsx"] --> B["api-client.streamChat<br/>POST /api/vanna/v2/chat_sse<br/>(cookie, SSE, AbortSignal)"]
    B --> C["routes.py chat_sse<br/>build RequestContext<br/>(cookies, headers, IP)"]
    C --> D["pump task: handle_stream"]
    C --> U["SSE response reads the queue:<br/>data: {...} frames<br/>keepalive comments<br/>data: [DONE] at the end"]
    D --> E["TenantDispatchChatHandler<br/>wiring.py _delegate"]

    E --> E1["resolve_user from cookie"]
    E1 --> E2{"Full session?<br/>(not temp password)"}
    E2 -- No --> ERR
    E2 -- Yes --> E3["Resolve data source<br/>platform.runtime_for(tenant, ds)"]
    E3 --> E4{"BYO key?"}
    E4 -- Yes --> E5["Use user's own LLM service<br/>quota not charged"]
    E4 -- No --> E6["Server LLM key"]
    E5 --> F
    E6 --> F

    F["ChatHandler.handle_stream<br/>agent.send_message"] --> G["Agent._send_message"]
    G --> G1["resolve user, before_message hooks"]
    G1 --> G2{"Empty message<br/>or starter?"}
    G2 -- Yes --> G3["Return starter UI<br/>no LLM"]
    G2 -- No --> G4["Load or create conversation"]
    G4 --> G5{"Workflow handler<br/>matches? (slash cmd)"}
    G5 -- Yes --> G6["Run command, skip LLM"]
    G5 -- No --> G7["Append user message<br/>build ToolContext + enrichers"]
    G7 --> G8["Get tool schemas<br/>build system prompt<br/>(+ domain, memory, instructions)"]
    G8 --> G9["Build LlmRequest + TurnState"]

    G9 --> P

    subgraph H["Turn graph"]
        direction TB
        P["PlannerNode (optional)"] --> L["llm_turn<br/>LLM streams text + tool calls"]
        L --> Q{"Tool calls?"}
        Q -- Yes --> T["tools node<br/>run_sql etc. on tenant data source"]
        T --> LIM{"Hit max_tool_iterations?"}
        LIM -- No --> L
        LIM -- Yes --> W["Warning card:<br/>Tool limit reached"]
        Q -- No --> CR{"Data tool ran<br/>and rows returned?"}
        CR -- Yes --> CK["CriticNode<br/>OK or reject"]
        CK -- Reject, retry left --> L
        CK -- OK --> AN["answer node"]
        CR -- No --> AN
    end

    AN --> S["Save conversation<br/>status idle, re-enable input"]
    W --> S

    ERR["Error or UserFacingError"] --> EC["Error status card<br/>+ error chunk"]
    H -. exception .-> ERR

    G -.->|"status: Processing..."| R
    H -.->|"text, tasks, tables, charts<br/>as they are produced"| R
    G3 -.-> R
    G6 -.-> R
    EC -.-> R
    S -.->|"status idle"| R

    R["UiComponent becomes ChatStreamChunk<br/>create + update frames"] --> QU["asyncio.Queue"]
    QU --> U
    U --> V["Browser renders live:<br/>status, tasks, text, table/chart"]
```

## Sequence diagram

```mermaid
sequenceDiagram
    participant UI as vanna-chat (browser)
    participant API as FastAPI chat_sse
    participant TD as TenantDispatchChatHandler
    participant AG as Agent
    participant LLM as LLM service
    participant DB as Tenant data source

    UI->>API: POST /chat_sse {message, conversation_id}
    API->>TD: handle_stream(request)
    TD->>TD: resolve user, pick tenant runtime, BYO key?
    TD->>AG: send_message(...)
    AG-->>UI: status "Processing..." (SSE)
    AG->>AG: load conversation, hooks, workflow check
    AG->>AG: tools + system prompt + context
    loop until the answer is accepted or the iteration limit is hit
        AG->>LLM: request (stream)
        LLM-->>AG: text / tool calls
        AG-->>UI: partial text (create/update frames)
        alt tool calls
            AG->>DB: run_sql
            DB-->>AG: rows
            AG-->>UI: table / chart component
        else no tool calls, rows were returned, CriticNode wired
            AG->>LLM: does this answer the question?
            LLM-->>AG: OK (leave loop) or reject (retry)
        end
    end
    AG->>AG: save conversation
    AG-->>UI: status idle, input re-enabled
    API-->>UI: data: [DONE]
```

## Not covered

The internals of the `run_sql` tool, system-prompt and memory enrichment, row-level guards (`read_guard.py`, `authz.py`), and quota and audit handling are shown as single boxes and were not traced.