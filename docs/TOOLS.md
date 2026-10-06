# Tools

Vishva ships 30+ integrated tools plus dynamically loaded MCP tools. Tools are
routed through `ToolManager` (`vishva/tool_manager.py`), registered in
`config/toolpool.txt` and enabled via `config/activetools.txt`.

All tools can be inspected and tested without starting the agent:

    ./test_tool.sh list                 # all tools + status
    ./test_tool.sh info <tool>          # description + parameter table
    ./test_tool.sh call <tool> '<json>' # execute a tool

For a full automated smoke test over all tools, run `./test/tools.sh`
(add `--dangerous` to include `sys_update`/`sys_clean`/`turnoff_screen`).

> 🔐 Tools marked **confirmation** require explicit user approval before
> execution (see `tool_confirm_tools` in CONFIGURATION.md).

## File System

### `bash` 🔐

Run a shell command (bash) in the persistent working directory. 120s timeout;
stdout+stderr returned. Auto-activates the `working_dir/.venv` when present.
The most common destructive patterns are blocked by a best-effort validation
layer; the confirmation prompt is the primary safety gate. For long-running
or daemon tasks use `bg_task` instead.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `command` | string | ✅ | Shell command to execute |

    ./test_tool.sh call bash '{"command": "ls -la && pwd"}'

### `cd`

Change the persistent working directory (affects `bash` and all relative path
resolution). Without argument it shows the current directory.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | | Target directory; omit to show cwd |

    ./test_tool.sh call cd '{"path": "/home/user/projects"}'
    ./test_tool.sh call cd '{}'

### `read_file`

Read a file's content. Very large results are offloaded to `data/agent_cache/`
and returned as a pointer — follow it with `read_cache`.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | File to read |

    ./test_tool.sh call read_file '{"path": "config/config.json"}'

### `write_file` 🔐

Create or overwrite a file. Code belongs in files via this tool, not in chat
answers. Content is syntax-checked for python/json/bash/js unless
`skip_validation` is set. Automatic backups are written to `backup_dir`.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | Target file |
| `content` | string | ✅ | Full file content |
| `skip_validation` | boolean | | Bypass syntax check |

    ./test_tool.sh call write_file '{"path": "test.txt", "content": "Hello World"}'

### `edit_file` 🔐

Surgical file edit: replace an exact `old_string` with `new_string`
(whitespace-normalized matching). Prefer over `write_file` for partial changes.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | File to edit |
| `old_string` | string | ✅ | Must match existing text |
| `new_string` | string | ✅ | Replacement |
| `skip_validation` | boolean | | Bypass syntax check |

    ./test_tool.sh call edit_file '{"path": "t.py", "old_string": "print(1)", "new_string": "print(2)"}'

### `list_dir`

List a directory, paginated, with configurable depth.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | Directory to list |
| `maxdepth` | integer | | 1–3 |
| `max_entries` | integer | | Page size |
| `show_hidden` | boolean | | Include dotfiles |

    ./test_tool.sh call list_dir '{"path": ".", "maxdepth": 2}'

### `lint_code`

Check code syntax without writing anything (python/json/bash/js).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `content` | string | ✅ | Code to check |
| `path` | string | | Or: file to check |
| `lang` | string | | `python` \| `json` \| `bash` \| `javascript` \| `auto` |

    ./test_tool.sh call lint_code '{"content": "def f():\n    return 1"}'

### `read_cache`

Read an offloaded tool result from `data/agent_cache`. Big results are stored
in chunks — page through them with `chunk`.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | `agent_cache/<sid>/<tcid>.json` |
| `chunk` | integer | | Chunk index 0..N-1 for big results |

    ./test_tool.sh call read_cache '{"path": "agent_cache/abc123/call_xyz.json", "chunk": 0}'

## Web & Scraping

### `web_read`

Fetch a webpage and return readable markdown. Playwright with stealth mode and
JS rendering; e-commerce aware (cookie-banner auto-click); archive fallback
(Google Cache / Wayback / archive.ph) when blocked.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `url` | string | ✅ | Page to fetch |
| `wait_for` | string | | CSS selector to wait for |
| `max_chars` | integer | | Output cap |
| `cache_fallback` | boolean | | Use web archives when blocked |
| `timeout` | integer | | Seconds |
| `screenshot` | boolean | | Save full-page screenshot |
| `js_render` | boolean | | Enable JS rendering |
| `stealth` | boolean | | Enable anti-bot stealth |

    ./test_tool.sh call web_read '{"url": "https://example.com"}'

### `web_search`

DuckDuckGo web search. Returns title/url/snippet list.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `query` | string | ✅ | Search terms |
| `max_results` | integer | | Result count |

    ./test_tool.sh call web_search '{"query": "python venv tutorial"}'

### `product_search`

Product & price-comparison search across Idealo, Geizhals and DuckDuckGo
Shopping. Returns title, price, URL and merchant per offer; deduplicated and
relevance-filtered.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `query` | string | ✅ | Product name / search terms |
| `sources` | string | | Comma list: `idealo,geizhals,ddg` |
| `max_results` | integer | | Per source |

    ./test_tool.sh call product_search '{"query": "Samsung 990 PRO 2TB", "sources": "idealo,geizhals,ddg"}'

## System

### `sys_update` 🔐

Run a full system update. Package manager is auto-detected
(pacman / apt / dnf / zypper). Long-running; use with care. No parameters.

    ./test_tool.sh call sys_update '{}'

### `sys_clean` 🔐

Remove orphan packages and clean the package cache. Package manager
auto-detected. No parameters.

    ./test_tool.sh call sys_clean '{}'

### `vol_ctl`

Control system volume. Backend auto-detected (pactl / wpctl / amixer).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `action` | string | ✅ | `vol_up` \| `vol_down` \| `vol_percent` |
| `percent` | integer | | 0–100, only for `vol_percent` |

    ./test_tool.sh call vol_ctl '{"action": "vol_percent", "percent": 50}'

### `brightness_ctl`

Control screen brightness. Backend auto-detected (KDE / GNOME / sysfs).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `action` | string | ✅ | `up` \| `down` \| `percent` |
| `value` | integer | | 0–100, only for `percent` |

    ./test_tool.sh call brightness_ctl '{"action": "percent", "value": 75}'

### `turnoff_screen`

Turn the screen off. Multi-DE support (KDE / GNOME / XFCE / MATE) with
xset/swaymsg fallbacks. No parameters.

    ./test_tool.sh call turnoff_screen '{}'

## Agent & Meta

### `subagent`

Spawn an isolated subagent with its own session and tool loop for a complex
sub-task; returns its final answer plus a condensed trace.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `task` | string | ✅ | Self-contained instruction |
| `max_turns` | integer | | Cap on tool turns |

    ./test_tool.sh call subagent '{"task": "Summarize the file docs/ARCHITECTURE.md"}'

### `bg_task` 🔐

Run/manage long-running commands as transient systemd user services. Reports
running/failed/completed after a brief delay. Note: commands run unattended —
this tool is part of the confirmation layer for exactly that reason.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `action` | string | ✅ | `start` \| `status` \| `list` \| `stop` \| `restart` \| `clean` |
| `command` | string | | Shell command (required for `start`) |
| `task_id` | string | | For `status`/`stop`/`restart` |
| `description` | string | | Optional label |
| `restart_on_failure` | boolean | | Auto-restart on failure |

    ./test_tool.sh call bg_task '{"action": "start", "command": "sleep 30 && echo done"}'

### `speak`

Speak text aloud via TTS (OpenAI-compatible server, e.g. Kokoro).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `speech` | string | ✅ | Text to speak |

    ./test_tool.sh call speak '{"speech": "Hello World"}'

## Scheduler

### `sched_task`

Schedule a task for later execution with multi-channel delivery
(cli / gui / telegram).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `trigger_time` | string | ✅ | `YYYY-MM-DD HH:MM:SS` |
| `prompt` | string | ✅ | What to do / remind |

    ./test_tool.sh call sched_task '{"trigger_time": "2026-10-05 14:00:00", "prompt": "Remind me about the meeting"}'

### `ls_tasks`

List all scheduled tasks with status. No parameters.

    ./test_tool.sh call ls_tasks '{}'

### `cancel_task`

Cancel a scheduled task.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `task_id` | string | ✅ | Task to cancel |

    ./test_tool.sh call cancel_task '{"task_id": "abc123"}'

## RAG (Long-Term Memory)

### `rag_search`

Semantic search over the long-term memory index.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `query` | string | ✅ | Natural-language query |
| `top_k` | integer | | Result count |

    ./test_tool.sh call rag_search '{"query": "my hobbies", "top_k": 5}'

### `rag_save`

Save a fact to long-term memory. Priorities: 0 = none, 1 = low, 2 = mid, 3 = high.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `text` | string | ✅ | Fact to store |
| `category` | string | | `user_info` \| `preference` \| `system` \| `agent` \| `knowhow` \| `project` \| `miscellaneous` |
| `priority` | integer | | 0–3 |
| `source` | string | | Origin label |

    ./test_tool.sh call rag_save '{"text": "User likes pizza", "category": "preference", "priority": 2}'

### `rag_update`

Update/correct an existing RAG entry (e.g. fix a misspelled name). Find the
entry via `id` or `query`, then set new `text`/`category`/`priority`.
Re-embeds automatically.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `id` | string | | Exact entry id |
| `query` | string | | Or: semantic lookup |
| `text` | string | | New text |
| `category` | string | | New category |
| `priority` | integer | | 0–3 |

    ./test_tool.sh call rag_update '{"query": "user name", "text": "The user is called Alexander"}'

### `rag_delete`

Delete RAG entries by exact id or source prefix.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `id` | string | | Exact entry id |
| `source` | string | | Delete all with this source prefix |

    ./test_tool.sh call rag_delete '{"source": "conversation"}'

### `rag_essential`

Create an ESSENTIAL always-injected memory entry (auto-injected into every
future prompt). Only works during first-run setup, max 3 total.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `text` | string | ✅ | Permanent fact |

    ./test_tool.sh call rag_essential '{"text": "The user is called Alexander"}'

### `rag_reindex`

Reindex the `knowledge/` folder into the RAG index. No parameters.

    ./test_tool.sh call rag_reindex '{}'

## Media

### `spotify`

Control Spotify playback via spotifyd/DBus. `play` requires a search query.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `action` | string | ✅ | `play` \| `stop` |
| `query` | string | | Search term (needed for `play`) |

    ./test_tool.sh call spotify '{"action": "play", "query": "Beatles"}'

### `news_digest`

Fetch and summarize current news via DuckDuckGo News.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `topics` | array | ✅ | List of topic strings |

    ./test_tool.sh call news_digest '{"topics": ["AI", "Linux"]}'

## Weather

### `weather`

Weather forecast for the configured location (config keys: `weather_latitude`,
`weather_longitude`, `weather_location_name`, `weather_timezone`;
Open-Meteo API).

| Parameter | Type | Required | Details |
|---|---|---|---|
| `period` | string | ✅ | `today` \| `tomorrow` \| `week` |

    ./test_tool.sh call weather '{"period": "today"}'
    ./test_tool.sh call weather '{"period": "week"}'

## Telegram

### `send_image`

Send an image to the user via Telegram.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `path` | string | ✅ | Image file |
| `caption` | string | | Optional caption |

    ./test_tool.sh call send_image '{"path": "/tmp/plot.png", "caption": "Result"}'

### `send_file`

Send a file to the user via Telegram.

| Parameter | Type | Required | Details |
|---|---|---|---|
| `file_path` | string | ✅ | File to send |

    ./test_tool.sh call send_file '{"file_path": "/tmp/report.pdf"}'

## MCP Tools (dynamic)

MCP tools are loaded at startup from `config/mcp.json` / `config/mcp_servers`
and carry the prefix `mcp__<server>__<tool>`. They are not persisted in
`toolpool.txt` / `activetools.txt`.

Example: local `pycode` server (`mcp/server.py`):

| Tool | Parameters | Description |
|---|---|---|
| `mcp__pycode__show_current_path` | — | Return the server's base working directory |
| `mcp__pycode__get_time` | — | Current local time (HH:MM:SS) |
| `mcp__pycode__get_date` | — | Current date (YYYY-MM-DD) |
| `mcp__pycode__execute` 🔐 | `bashprompt` (string, ✅), `timeout` (integer) | Run a shell command via bash; returns stdout+stderr |
| `mcp__pycode__read_file` | `pfad` (string, ✅), `max_chars` (integer) | Read a file; directories return an `ls -la` listing |
| `mcp__pycode__list_dir` | `pfad` (string) | List a directory (`ls -la`) |
| `mcp__pycode__create_file` | `path` (string, ✅), `context` (string, ✅) | Create/overwrite a file, creates parent dirs |
| `mcp__pycode__append_file` | `path` (string, ✅), `context` (string, ✅) | Append to a file |
| `mcp__pycode__search` | `pattern` (string, ✅), `pfad` (string), `case_insensitive` (boolean) | grep search under a directory |

    ./test_tool.sh call mcp__pycode__execute '{"bashprompt": "uname -a"}'
    ./test_tool.sh call mcp__pycode__read_file '{"pfad": "config/config.json"}'

→ See [MCP.md](MCP.md) for server setup and adding external MCP servers.

## Tool Testing Cheat-Sheet

    # List all tools with status
    ./test_tool.sh list

    # Show description + parameter table
    ./test_tool.sh info web_read

    # Execute a tool
    ./test_tool.sh call web_read '{"url": "https://example.com"}'

    # With debug output
    DEBUG=1 ./test_tool.sh call web_read '{"url": "https://example.com"}'

    # Enable a disabled tool (add its name to config/activetools.txt)
    echo "speak" >> config/activetools.txt

    # Full automated smoke test over all tools
    ./test/tools.sh
    ./test/tools.sh --dangerous          # include sys_update/sys_clean/turnoff_screen
    ./test/tools.sh --only bash,web_read # test a subset
