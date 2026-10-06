# Tutorial

## First Steps

### 1. Installation

    git clone <repo-url>
    cd vishva
    chmod +x *.sh *.py
    ./installer.py        # system scan, config, venv, deps
    ./onboarding.sh       # first-run interview + RAG setup

The installer walks you through:

- Creating a virtual environment
- Installing dependencies
- Configuring the LLM endpoint(s)
- Choosing tool categories and a persona

The onboarding interview creates your initial RAG entries and a personal
persona file.

### 2. Start

    ./run.sh

You'll see the CLI prompt:

    vishva> _

### 3. First Message

    vishva> Hello, who are you?

Vishva responds based on `personas/_active/SOUL.md`.

## Customizing Personality

### Use Templates

    /personality coder

Available templates:

- `default` — all-rounder with the full tool set
- `clean` — minimal, factual, reduced tool set
- `coder` — programming focus
- `secretary` — office assistant
- `sharp` — direct, concise, system-control focus

### Custom Personality

Copy a template:

    cp personas/default.md personas/custom/my_persona.md

Edit the `---SOUL---` and `---ENGINE---` sections in the file, then activate:

    /personality my_persona

Personas in `personas/custom/` are found exactly like built-in templates.

## Using Tools

### Example: Read a File

    vishva> Read the file config/config.json

Vishva automatically calls `read_file`.

### Example: Web Scraping

    vishva> What's on https://example.com?

Vishva uses `web_read` with Playwright.

### Example: Product Search

    vishva> Find me cheap offers for Samsung 990 PRO 2TB

Vishva uses `product_search` (Idealo + Geizhals + DDG).

## RAG (Long-Term Memory)

### Save Manually

    vishva> Remember: I like pepperoni pizza

Vishva automatically extracts and saves to RAG.

### Search RAG

    ./test_tool.sh call rag_search '{"query": "what do I like to eat?"}'

### Mark Essentials

Essentials are always injected into context (first-run setup only, max 3):

    ./test_tool.sh call rag_essential '{"text": "The user is called Alexander"}'

## Scheduler

### Delayed Task

    vishva> Remind me about the meeting in 30 minutes

Vishva schedules a task via `sched_task`.

### List Tasks

    ./test_tool.sh call ls_tasks '{}'

## MCP Servers

### Use the Local Server

Enable MCP and define servers either in `config/config.json` (`mcp_servers`)
or in `config/mcp.json` (`mcpServers`):

    {
      "mcp_enabled": true,
      "mcp_servers": {
        "pycode": {
          "command": "./venv/bin/python",
          "args": ["./mcp/server.py"]
        }
      }
    }

Vishva loads MCP tools automatically:

    vishva> Execute: uname -a

Vishva uses `mcp__pycode__execute`.

## Troubleshooting

### Tool Not Found

    ./test_tool.sh list

Shows all available tools and their active state.

### RAG Empty

    ./test_tool.sh call rag_reindex '{}'

Reindexes the `knowledge/` folder. If searches still return nothing, check
that an embedding model exists in `models/` (see INSTALL.md).

### Context Too Full

Vishva compresses automatically at `compression_threshold`. Trigger manually:

    /zip

### Debug Mode

    {
      "debug": 2
    }

Shows verbose logs (RAG injection, tool calls, etc.).

## Next Steps

- [CONFIGURATION.md](CONFIGURATION.md) — all config keys
- [TOOLS.md](TOOLS.md) — tool reference
- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture
- [MCP.md](MCP.md) — MCP integration
- [BEST_PRACTICES.md](BEST_PRACTICES.md) — LLM backend setup
