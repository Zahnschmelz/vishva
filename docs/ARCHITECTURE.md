# Architecture

## System Overview

```mermaid
graph TB
    subgraph "User Interfaces"
        CLI["CLI<br/>agent.py"]
        GUI["GUI<br/>gui.py"]
        TG["Telegram Bot<br/>bot.py"]
    end
    subgraph "Core"
        Agent["AgentCore<br/>agent.py"]
        SM["SessionManager<br/>session_manager.py"]
        TM["ToolManager<br/>tool_manager.py"]
    end
    subgraph "Memory"
        Sessions["Sessions<br/>data/sessions/"]
        Cache["Agent Cache<br/>data/agent_cache/"]
        RAG["RAG Manager<br/>rag_manager.py"]
        RAGDB[("RAG DB<br/>data/rag_db/")]
    end
    subgraph "Tools"
        Tools30["30+ Tools<br/>vishva/tools/"]
        MCP["MCP Client<br/>mcp_client.py"]
        MCPServer["MCP Server<br/>mcp/server.py"]
    end
    subgraph "Background"
        BgTasks["Background Tasks<br/>background_tasks.py"]
        Scheduler["Scheduler<br/>scheduler.py"]
        Daemon["Scheduler Daemon<br/>scheduler_daemon.py"]
    end
    subgraph "External"
        LLM["Main LLM<br/>llama-server :8080"]
        Meta["Meta Model<br/>llama-server :8090"]
        Vision["Vision Model<br/>multimodal"]
        TTS["TTS Server<br/>Kokoro :8091"]
    end
    CLI --> Agent
    GUI --> Agent
    TG --> Agent
    Agent --> SM
    Agent --> TM
    Agent --> RAG
    SM --> Sessions
    SM --> Cache
    RAG --> RAGDB
    RAG --> Meta
    TM --> Tools30
    TM --> MCP
    MCP --> MCPServer
    Agent --> LLM
    Agent --> Vision
    Agent --> TTS
    BgTasks -.-> Agent
    Scheduler -.-> Agent
    Daemon -.-> Scheduler
```

## Components

### AgentCore (`agent.py`)

The central orchestrator:

- Loads config + persona (SOUL.md + ENGINE.md)
- Builds system prompt (static + dynamic)
- Manages sessions
- Executes tool loops
- Protects context (compression, loop rescue)
- Injects RAG context (`[ESSENTIAL_CONTEXT]` / `[RAG_ENRICHMENT]`)
- Extracts new knowledge (via meta model)

### SessionManager (`session_manager.py`)

Session and context management:

- JSON session storage
- Token counting (server-side or tiktoken)
- History compression (via meta model)
- Tool result offloading (to `data/agent_cache/`)
- Tool TTL handling

### ToolManager (`tool_manager.py`)

Dynamic tool routing:

- Loads tools from `vishva/tools/`
- Manages `toolpool.txt` + `activetools.txt`
- Executes tools
- Enforces the confirmation layer for destructive tools
- Integrates MCP servers

### RAGManager (`rag_manager.py`)

Semantic long-term memory:

- Embeddings via local sentence-transformer models
- NumPy matrix scoring (fast)
- Meta model enrichment (query decision + filter)
- Meta model extraction (auto-extraction after turn)
- Dedup (vector similarity + meta model borderline)
- Access tracking + aging + rotation

### Scheduler (`scheduler.py` + `scheduler_daemon.py`)

Task scheduling:

- Delayed tasks with trigger time
- Multi-channel delivery (CLI/GUI/Telegram)
- Claim/TTL mechanism for concurrency
- Desktop notifications + sound
- Terminal opening via `pstree` detection

### MCP Client (`mcp_client.py`)

Model Context Protocol integration:

- Stdio transport (subprocess)
- HTTP transport (Streamable HTTP + SSE)
- Lazy connect + auto reconnect
- Dynamic tool registration

### Personas (Identity Layer)

The agent's identity is assembled at runtime from three live files:

- `personas/_active/SOUL.md` — personality, tone, language
- `personas/_active/ENGINE.md` — operational directives and tool discipline
- `config/activetools.txt` — the active tool set

Persona templates (`personas/<name>.md`) bundle all three layers into a single
file and are applied via `/persona <name>` — without restart and without losing
session history. The onboarding assembles personal personas from
`personas/snippets/` into `personas/custom/`. SOUL + ENGINE are injected into
the system prompt on every request; ACTIVETOOLS filters the tool pool offered
to the model.

→ Full format reference and examples: [PERSONAS.md](PERSONAS.md)

## Data Flow

### Chat Request

```mermaid
sequenceDiagram
    participant User
    participant Agent
    participant SM
    participant TM
    participant LLM
    participant RAG
    participant Meta
    User->>Agent: Message
    Agent->>SM: load_session
    SM-->>Agent: History
    Agent->>RAG: enrich_context
    RAG->>Meta: Query decision
    Meta-->>RAG: YES/NO + queries
    RAG->>RAG: Search + filter
    RAG-->>Agent: [RAG_ENRICHMENT]
    Agent->>Agent: Inject RAG + system prompt
    Agent->>LLM: chat/completions
    LLM-->>Agent: Response + tool calls
    loop Tool Loop
        Agent->>TM: execute_tool
        TM-->>Agent: Tool result
        Agent->>LLM: chat/completions (with result)
        LLM-->>Agent: Response
    end
    Agent->>SM: save_session
    Agent->>RAG: extract_from_turn (background)
    RAG->>Meta: Extraction prompt
    Meta-->>RAG: New facts
    RAG->>RAG: Dedup + save
    Agent-->>User: Answer
```

### Context Protection

```mermaid
graph LR
    A["Chat Start"] --> B{Tokens > Threshold?}
    B -->|No| C["Normal Flow"]
    B -->|Yes| D["Compress History"]
    D --> E{Compression OK?}
    E -->|Yes| C
    E -->|No| F["Loop Rescue"]
    F --> G["Extract Tool Trace"]
    G --> H{Trace > 6000 chars?}
    H -->|No| I["Use Trace as Summary"]
    H -->|Yes| J["Map-Reduce via Meta"]
    J --> K["Chunk Summaries"]
    K --> L["Merge Summaries"]
    L --> M["Inject Summary"]
    M --> C
    I --> C
```

## File Structure

    vishva/
    ├── vishva/                    # Python package
    │   ├── agent.py              # AgentCore (orchestrator)
    │   ├── session_manager.py    # Session/context management
    │   ├── tool_manager.py       # Tool routing + confirmation + MCP
    │   ├── rag_manager.py        # RAG logic
    │   ├── rag_storage.py        # RAG persistence
    │   ├── scheduler.py          # Scheduler logic
    │   ├── scheduler_daemon.py   # Standalone daemon
    │   ├── mcp_client.py         # MCP client
    │   ├── background_agent.py   # Meta model client
    │   ├── background_tasks.py   # Systemd background tasks
    │   ├── bot.py                # Telegram bot
    │   ├── gui.py                # PySide6 GUI
    │   ├── tts_manager.py        # TTS integration
    │   ├── paths.py              # Path resolution
    │   └── tools/                # Individual tool modules
    │       ├── bash.py
    │       ├── web_read.py
    │       ├── product_search.py
    │       ├── subagent.py
    │       └── ...
    ├── config/
    │   ├── config.example.json   # Template — copy to config.json
    │   ├── mcp.json              # MCP server config
    │   ├── toolpool.txt          # Tool registry (JSON)
    │   └── activetools.txt       # Active tools
    ├── personas/                 # → see docs/PERSONAS.md
    │   ├── _active/
    │   │   ├── SOUL.md           # LIVE personality (system prompt)
    │   │   └── ENGINE.md         # LIVE directives (system prompt)
    │   ├── snippets/             # Onboarding building blocks
    │   ├── custom/               # Generated personas (gitignored)
    │   └── *.md                  # Templates (applied via /persona)
    ├── mcp/
    │   └── server.py             # Local MCP server
    ├── data/                     # Runtime state (gitignored)
    │   ├── sessions/             # Session JSONs
    │   ├── agent_cache/          # Tool result cache
    │   ├── rag_db/               # RAG vectors + meta
    │   ├── bg_tasks/             # Background task meta
    │   └── scheduled_tasks.json  # Scheduler tasks
    ├── services/                 # Systemd units (generated by installer)
    ├── knowledge/                # RAG knowledge files
    ├── assets/                   # Logos + sounds
    ├── scripts/                  # Helper scripts
    └── docs/                     # Documentation

## Design Principles

- **Local-First** — everything runs locally, external services optional
- **Modular** — tools are individual modules, easily extensible
- **Context-Aware** — RAG + meta model for intelligent memory
- **Resilient** — context protection, fallbacks, auto recovery
- **Safe** — confirmation layer for destructive operations
- **Extensible** — MCP support for external tool servers
