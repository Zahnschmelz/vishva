# MCP (Model Context Protocol)

MCP enables integration of external tool servers via a standardized protocol.

## Setup

Servers can be defined in two equivalent places — they are merged at startup:

### 1. Inside `config/config.json` (`mcp_servers`)

    {
      "mcp_enabled": true,
      "mcp_auto_activate": true,
      "mcp_tool_timeout": 60,
      "mcp_max_desc_chars": 300,
      "mcp_servers": {
        "pycode": {
          "command": "./venv/bin/python",
          "args": ["./mcp/server.py"]
        }
      }
    }

### 2. Separate file `config/mcp.json` (`mcpServers`, standard format)

    {
      "mcpServers": {
        "pycode": {
          "command": "./venv/bin/python",
          "args": ["./mcp/server.py"]
        },
        "fs": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]
        }
      }
    }

## Local MCP Server

Vishva includes a local MCP server (`mcp/server.py`) with basic tools.

### Tools

| Tool | Description |
|---|---|
| `show_current_path` | Returns the working directory |
| `get_time` | Current time |
| `get_date` | Current date |
| `execute` | Runs a shell command (with timeout) |
| `read_file` | Reads a file or directory listing |
| `list_dir` | Lists a directory |
| `create_file` | Creates a file |
| `append_file` | Appends to a file |
| `search` | grep search |

### Environment Variables (all optional)

By default, relative paths resolve against `<repo_root>/working_dir`.
Override with:

    VISHVA_MCP_WORKDIR=/path/to/workdir
    VISHVA_MCP_MAX_OUT=20000
    VISHVA_MCP_TIMEOUT=120

Example with an explicit env block:

    {
      "mcpServers": {
        "pycode": {
          "command": "./venv/bin/python",
          "args": ["./mcp/server.py"],
          "env": {
            "VISHVA_MCP_WORKDIR": "/path/to/workdir"
          }
        }
      }
    }

## Installation

    # mcp 2.x
    pip install mcp

## Testing

    # Standalone test
    echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | ./venv/bin/python ./mcp/server.py

    # Via Vishva
    ./test_tool.sh list | grep mcp__pycode
    ./test_tool.sh call mcp__pycode__get_time '{}'
    ./test_tool.sh call mcp__pycode__execute '{"bashprompt": "uname -a"}'

## Tool Naming

MCP tools have the prefix `mcp__<server>__<tool>`:

- `mcp__pycode__execute`
- `mcp__pycode__read_file`
- `mcp__fs__readFile`

## Dynamic Registration

MCP tools are automatically registered in `available_tools` + `active_tools` on
startup (if `mcp_auto_activate: true`).

Important: MCP tools are **not persisted** in `toolpool.txt` / `activetools.txt`
— they are purely dynamic.

## External MCP Servers

### Filesystem Server

    {
      "mcpServers": {
        "fs": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]
        }
      }
    }

### Time Server

    {
      "mcpServers": {
        "time": {
          "command": "uvx",
          "args": ["mcp-server-time", "--local-timezone=Europe/Berlin"]
        }
      }
    }

### HTTP Server (Streamable HTTP)

    {
      "mcpServers": {
        "remote": {
          "url": "http://127.0.0.1:3100/mcp",
          "headers": {
            "Authorization": "Bearer xyz"
          }
        }
      }
    }

## Troubleshooting

**`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`**

    pip install 'mcp'

**Server doesn't start**

    # Enable debug mode
    "debug": 1  # in config.json

    # Check logs
    # [MCP] connected: pycode
    # [MCP] pycode: refresh failed: ...

**Tool not found**

    # Reload tools (restart Vishva)
    ./run.sh

    # Or: test MCP server manually
    ./venv/bin/python ./mcp/server.py
