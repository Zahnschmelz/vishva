# Configuration

All settings live in `config/config.json`. Copy `config/config.example.json`
to `config/config.json` and adjust. Keys not listed here fall back to sane
code defaults.

## Core / LLM

    {
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "your-model.gguf",
      "api_key": "ollama",
      "api_timeout": 600,
      "api_retry_delay": 1.5,
      "temperature": 0.9,
      "top_p": 0.95,
      "top_k": 64,
      "seed": null,
      "context_size": 32768,
      "compression_threshold": 24576,
      "token_counting": "server",
      "language": "en",
      "debug": 0
    }

| Key | Description | Default |
|---|---|---|
| `base_url` | LLM endpoint (OpenAI-compatible) | `http://127.0.0.1:8080/v1` |
| `model` | Model name or GGUF path | `llama3.1` |
| `api_key` | API key (usually irrelevant for local servers) | `ollama` |
| `api_timeout` | Request timeout in seconds | 1200 |
| `api_retry_delay` | Delay before retry after a failed request | 1.5 |
| `temperature` / `top_p` / `top_k` / `seed` | Sampling parameters (only sent when set) | — |
| `context_size` | Max context size in tokens | 16384 |
| `compression_threshold` | Compress history at X tokens (`null` = auto: ctx − reserve) | auto |
| `token_counting` | `server` (via `/tokenize`) or `tiktoken` | `tiktoken` |
| `language` | Interface/agent language | `en` |
| `debug` | Debug level (0 = off, 1 = minimal, 2 = verbose) | 0 |

## Meta model

Small local model for RAG injection/extraction, dedup checks and loop rescue.
See [BEST_PRACTICES.md](BEST_PRACTICES.md) for the two-server pattern.

    {
      "meta_model_url": "http://127.0.0.1:8090/v1",
      "meta_model_name": "meta",
      "meta_model_timeout": 60,
      "meta_model_max_tokens": 1024,
      "meta_context_size": 4096,
      "meta_summary_max_tokens": 300,
      "meta_fallback_enabled": true,
      "meta_send_thinking_kwargs": false
    }

| Key | Description | Default |
|---|---|---|
| `meta_model_url` | Meta model endpoint (empty = meta features off) | `""` |
| `meta_model_name` | Model name on that server | `meta` |
| `meta_model_timeout` | Request timeout in seconds | 60 |
| `meta_model_max_tokens` | Raise max_tokens for reasoning meta models | 0 (off) |
| `meta_context_size` | Context size used for meta budget calculations | 4096 |
| `meta_summary_max_tokens` | Output cap for summaries | 300–500 |
| `meta_fallback_enabled` | Fall back to the main model when meta fails | true |
| `meta_send_thinking_kwargs` | Send `enable_thinking: false` for thinking models | false |

Disable all meta-driven features when running a single server:

    {
      "meta_model_url": "",
      "rag_meta_enrichment_enabled": false,
      "rag_meta_extraction_enabled": false
    }

## Context protection

    {
      "loop_rescue_enabled": true,
      "loop_rescue_mode": "hybrid",
      "loop_rescue_reserve": 4000,
      "loop_rescue_keep_last": 1,
      "loop_rescue_max_attempts": 2,
      "loop_adaptive_cap": true,
      "budget_hint_enabled": true,
      "offloading_enabled": true,
      "read_cache_chunk_size": 2500,
      "max_tool_result_length": 4000,
      "max_tool_args_length": 4000,
      "max_tool_turns": 15,
      "tool_ttl_turns": 6,
      "history_keep_n": 5
    }

| Key | Description | Default |
|---|---|---|
| `loop_rescue_enabled` | Rescue the session when context is about to overflow mid-task | true |
| `loop_rescue_mode` | `llm` / `hybrid` / `none` — how the tool trace is condensed | `hybrid` |
| `loop_rescue_reserve` | Token reserve that triggers rescue | 4000 |
| `loop_rescue_keep_last` | Keep the last N tool pairs verbatim | 1 |
| `loop_rescue_max_attempts` | Max rescues per task | 2 |
| `loop_adaptive_cap` | Halve tool-result cap when context > 60% | true |
| `budget_hint_enabled` | Inject context/tool budget hints into the prompt | true |
| `offloading_enabled` | Offload large tool args/results to `data/agent_cache/` | true |
| `read_cache_chunk_size` | Chunk size for offloaded results | 2500 |
| `max_tool_result_length` | Truncate tool results to N chars (cached in full) | 3000 |
| `max_tool_args_length` | Truncate stored tool arguments to N chars | 2000 |
| `max_tool_turns` | Max tool-call rounds per user message | 15 |
| `tool_ttl_turns` | Compress tool pairs older than N turns into one-liners | 5 |
| `history_keep_n` | Messages kept verbatim during compression | 5 |

## Tools — execution & confirmation

    {
      "agent_workdir": "working_dir",
      "auto_venv": true,
      "venv_path": "working_dir/.venv",
      "backup_dir": "backups",
      "max_backups": 20,
      "max_bash_output_chars": 5000,
      "max_bash_output_lines": 250,
      "tool_confirm_enabled": true,
      "tool_confirm_tools": ["bash", "write_file", "edit_file", "sys_update",
                             "sys_clean", "mcp__pycode__execute", "bg_task"],
      "tool_confirm_fallback": "allow",
      "tool_confirm_frame": true,
      "tool_confirm_preview_max": 100,
      "bg_task_initial_delay": 4,
      "bg_task_log_tail_lines": 15,
      "web_read_max_chars": 15000,
      "web_read_max_chars_limit": 50000,
      "tool_log_args_length": 120
    }

| Key | Description | Default |
|---|---|---|
| `agent_workdir` | Persistent working directory for `bash`/relative paths | `working_dir` |
| `auto_venv` | Auto-activate a venv for `bash` commands | true |
| `venv_path` | Explicit venv location | `working_dir/.venv` |
| `backup_dir` / `max_backups` | Automatic file backups on write/edit | `backups` / 20 |
| `max_bash_output_chars` / `max_bash_output_lines` | bash output truncation | 5000 / 100 |
| `tool_confirm_enabled` | Master switch for the confirmation layer | true |
| `tool_confirm_tools` | Tool names/patterns (fnmatch) that require confirmation | see above |
| `tool_confirm_fallback` | Verdict when no interactive UI is available (daemon, subagent, bot): `allow` / `deny` | `allow` |
| `tool_confirm_frame` | Render confirmation as a panel in the CLI | true |
| `tool_confirm_preview_max` | Argument preview length in the prompt | 100 |
| `bg_task_initial_delay` | Seconds to wait before the first status report | 4 |
| `bg_task_log_tail_lines` | Log tail lines included in `bg_task status` | 15 |
| `web_read_max_chars` / `web_read_max_chars_limit` | Default/hard cap for web_read output | 15000 / 50000 |

> **Note:** scheduled tasks, the daemon and subagents run unattended — they use
> `tool_confirm_fallback` instead of an interactive prompt.

## Subagent

    {
      "subagent_forbidden": ["speak", "rag_save", "send_image", "send_file",
                             "sched_task", "ls_tasks", "cancel_task", "subagent"],
      "subagent_max_turns_limit": 15
    }

## Vision / image analysis

    {
      "vision_enabled": true,
      "vision_backend": "auto",
      "vision_base_url": "",
      "vision_model": "",
      "vision_api_key": "",
      "vision_disable_tools": true,
      "image_max_size": 256
    }

| Key | Description | Default |
|---|---|---|
| `vision_enabled` | Enable image analysis | true |
| `vision_backend` | `auto` / `ollama` / `none` | `auto` |
| `vision_base_url` / `vision_model` | Dedicated vision endpoint (empty = main model) | `""` |
| `vision_disable_tools` | Disable tools during vision turns (most vision models can't tool-call) | true |
| `image_max_size` | Downscale images to N×N before encoding | 256 |

## Audio — TTS / STT / sounds

    {
      "tts_enabled": false,
      "tts_server_url": "http://127.0.0.1:8091",
      "tts_model": "kokoro",
      "tts_voice": "kerstin",
      "tts_speed": 1.0,
      "tts_response_format": "wav",
      "tts_timeout": 300,
      "tts_max_chars": 2000,
      "tts_play": false,
      "stt_command": "whisper {audio} --model tiny --language German --output_format txt --output_dir {dir}",
      "stt_seconds": 5,
      "sound_enabled": false
    }

| Key | Description | Default |
|---|---|---|
| `tts_enabled` | Enable text-to-speech | false |
| `tts_server_url` | OpenAI-compatible TTS server | `http://127.0.0.1:8091` |
| `tts_voice` | Voice name — depends on your TTS server (Kokoro examples: `af_heart`, `gf_siebens`) | — |
| `tts_response_format` | `wav` / `mp3` / `ogg` | `wav` |
| `tts_play` | Also play audio locally (not just send it) | false |
| `stt_command` | Speech-to-text command; `{audio}` and `{dir}` are substituted | whisper tiny |
| `stt_seconds` | Default recording length for `/stt` | 5 |
| `sound_enabled` | Play notification sounds | false |

## RAG — long-term memory

    {
      "rag_enabled": true,
      "rag_db_dir": "data/rag_db",
      "rag_matrix_mode": true,
      "rag_embedding_model": "multilingual-e5-large-instruct",
      "rag_top_k": 2,
      "rag_index_startup": true,
      "rag_preload_model": true,
      "rag_preload_delay_seconds": 0,
      "rag_meta_enrichment_enabled": true,
      "rag_meta_extraction_enabled": true,
      "rag_meta_max_search_queries": 3,
      "rag_meta_max_extraction_entries": 3,
      "rag_dedup_similarity_threshold": 0.90,
      "rag_max_context_chars": 3000,
      "rag_inject_max_chars": 2500,
      "rag_injection_access_min_score": 0.8,
      "rag_debug_injection": false
    }

| Key | Description | Default |
|---|---|---|
| `rag_enabled` | Master switch | true |
| `rag_db_dir` | RAG database location | `data/rag_db` |
| `rag_matrix_mode` | NumPy matrix scoring (fast) | true |
| `rag_embedding_model` | Local model dir or HF name — see INSTALL.md | `all-MiniLM-L6-v2` |
| `rag_top_k` | Results considered per enrichment query | 5 |
| `rag_index_startup` | Index `knowledge/` at startup | true |
| `rag_preload_model` | Warm up the embedding model in a background thread | true |
| `rag_meta_enrichment_enabled` | Meta model decides per message whether memory is injected | true |
| `rag_meta_extraction_enabled` | Auto-extract new facts after each turn | true |
| `rag_dedup_similarity_threshold` | Cosine threshold for duplicate detection | 0.90 |
| `rag_max_context_chars` | Max chars for the `[ESSENTIAL_CONTEXT]` block | 2500 |
| `rag_inject_max_chars` | Max chars for enrichment injection | 2500 |
| `rag_debug_injection` | Verbose injection logs | false |

### Access tracking, aging & rotation

    {
      "rag_access_tracking_enabled": true,
      "rag_access_save_interval": 5,
      "rag_cycle_aging_enabled": true,
      "rag_max_entries": 10000,
      "rag_unused_max_cycles": 50,
      "rag_unused_access_threshold": 1,
      "rag_low_access_max_cycles": 100,
      "rag_low_access_threshold": 5,
      "rag_rotation_protected_categories": ["manual", "important", "user_info", "essential"],
      "rag_rotation_protected_sources": [],
      "rag_rotation_hard_max": false
    }

Entries that are never accessed age out; protected categories/sources never do.

### Chunking & knowledge index

    {
      "rag_chunk_max_chars": 1000,
      "rag_chunk_overlap_chars": 150,
      "rag_knowledge_max_chars": 1200,
      "rag_knowledge_overlap_chars": 120,
      "rag_knowledge_meta_refine": true,
      "rag_knowledge_expand_neighbors": 1
    }

### Categories

    {
      "rag_categories": ["user_info", "preference", "system", "agent",
                         "knowhow", "project", "miscellaneous"],
      "rag_reserved_categories": ["essential", "knowledge"],
      "rag_category_fallback": "miscellaneous",
      "rag_exclude_maintenance_categories": ["essential", "knowledge"]
    }

### Maintenance (daemon)

    {
      "rag_maintenance_enabled": true,
      "rag_maintenance_interval_hours": 24,
      "rag_maint_idle_minutes": 5,
      "rag_maint_cpu_max": 25,
      "rag_maint_mem_min_free_gb": 4,
      "rag_maint_mem_min_free_percent": 50,
      "rag_maint_batch_size": 20,
      "rag_maint_dedup_threshold": 0.87,
      "rag_maint_dedup_meta_confirm": true,
      "rag_maint_merge_enabled": true,
      "rag_maint_max_merges_per_tick": 3,
      "rag_maint_max_merge_chars": 1000,
      "rag_maint_dry_run": false,
      "rag_maint_retry_minutes": 5
    }

Maintenance runs in the scheduler daemon when the system is idle: dedup/merge,
priority review, priority assignment, category unification — all gated by
interval, session idle time, CPU and RAM.

## Scheduler

    {
      "scheduler_enabled": true,
      "scheduler_check_interval": 30,
      "scheduler_claim_ttl": 900,
      "scheduler_default_target": "auto",
      "scheduler_default_chat_id": "",
      "scheduler_fallback_delay": 300,
      "scheduler_default_fallbacks": {
        "cli": "telegram",
        "gui": "telegram",
        "telegram": "none",
        "auto": "telegram"
      },
      "scheduler_daemon_enabled": true,
      "scheduler_daemon_targets": ["cli", "gui"],
      "scheduler_reload_tools": true,
      "scheduler_required_tools": ["bash", "read_file", "write_file", "list_dir"],
      "scheduler_refresh_system_prompt": true,
      "scheduler_clear_session_per_task": true
    }

| Key | Description | Default |
|---|---|---|
| `scheduler_enabled` | Master switch | true |
| `scheduler_check_interval` | Poll interval in seconds | 30 |
| `scheduler_claim_ttl` | Stale claims are released after N seconds | 900 |
| `scheduler_default_target` | `cli` / `gui` / `telegram` / `auto` | `auto` |
| `scheduler_fallback_delay` | Seconds before a fallback target may deliver | 300 |
| `scheduler_default_fallbacks` | Per-target fallback map (`none` disables) | see above |
| `scheduler_daemon_enabled` | Standalone daemon delivers `cli`/`gui` tasks | false |
| `scheduler_daemon_targets` | Targets handled by the daemon | `["cli", "gui"]` |
| `scheduler_required_tools` | Temporarily activated for scheduled tasks | fs basics |
| `scheduler_clear_session_per_task` | Daemon starts each task with a clean session | true |

### Notifications, sounds & terminal

    {
      "scheduler_notifications_enabled": true,
      "scheduler_notification_command": "notify-send",
      "scheduler_notification_app_name": "Vishva",
      "scheduler_notification_icon": "",
      "scheduler_notification_urgency": "normal",
      "scheduler_notification_error_urgency": "critical",
      "scheduler_notification_timeout_ms": 10000,
      "scheduler_notification_max_chars": 500,
      "scheduler_notification_title_prefix": "Vishva",
      "scheduler_notify_log_dir": "data/scheduler_notifications",
      "scheduler_play_sound": true,
      "scheduler_play_sound_on_error": true,
      "scheduler_sound_files": ["assets/sounds/alarm.wav"],
      "scheduler_sound_player": "auto",
      "scheduler_sound_command": "",
      "scheduler_open_terminal": true,
      "scheduler_terminal_title": "Vishva Scheduler Response",
      "scheduler_script_cleanup_max_age_hours": 24
    }

## Weather

    {
      "weather_latitude": 52.5200,
      "weather_longitude": 13.4050,
      "weather_location_name": "Berlin",
      "weather_timezone": "Europe/Berlin"
    }

Replace with your own location. The `weather` tool uses the Open-Meteo API.

## Telegram

    {
      "bot_token": "YOUR_BOT_TOKEN",
      "chat_id": "YOUR_CHAT_ID",
      "tg_files_dir": "data/tg_files",
      "tg_max_file_size_mb": 80
    }

The bot only responds to `chat_id`. Received documents are stored under
`tg_files_dir/<chat_id>/`.

## Spotify

    {
      "spotify_client_id": "YOUR_CLIENT_ID",
      "spotify_client_secret": "YOUR_CLIENT_SECRET",
      "spotify_redirect_uri": "http://127.0.0.1:8888/callback",
      "spotify_market": "DE"
    }

Run `python scripts/spotify_auth_server.py` once to create the token cache
(`data/.spotify_cache`).

## MCP

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

Two equivalent places to define servers — they are merged at startup:

- `mcp_servers` inside `config/config.json`
- `config/mcp.json` using the standard `mcpServers` format

→ See [MCP.md](MCP.md) for details.

## CLI / GUI

    {
      "cli_markdown_enabled": true,
      "cli_code_block_extraction": true,
      "cli_frame_enabled": true,
      "cli_frame_style": "cyan",
      "cli_show_intermediate_content": true,
      "gui_history_limit": 50,
      "gui_tool_result_preview": 800,
      "show_thinking": false
    }

## Miscellaneous

    {
      "debug": 0,
      "system_prompt": "You are a helpful assistant with tool access.",
      "rag_session_inherit_enabled": true,
      "rag_session_inherit_max_entries": 10
    }
