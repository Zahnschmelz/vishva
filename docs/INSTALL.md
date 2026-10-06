# Installation

Vishva runs fully locally on Linux. The installation is split into two stages:

1. **`installer.py`** — system scan, interactive configuration, venv + dependencies
2. **`onboarding.sh`** — first-run interview, RAG entries, personal persona generation

## Prerequisites

**Arch / CachyOS / Manjaro**

    sudo pacman -S --needed python python-pip git python-rich
    # optional but recommended:
    sudo pacman -S --needed ffmpeg jq rsync htop wl-clipboard   # xclip on X11
    # AUR helper (optional):
    yay -S --needed llama.cpp-cuda   # or the Vulkan build

**Ubuntu / Debian**

    sudo apt update
    sudo apt install python3 python3-pip git python3-rich
    sudo apt install ffmpeg jq rsync htop

**Fedora**

    sudo dnf install python3 python3-pip git python-rich ffmpeg jq rsync htop

`rich` powers the installer UI and must come from the system package manager
(system `pip` is blocked by PEP 668 on modern distros). Everything else is
installed into a project venv by the installer.

Make the scripts executable (no sudo needed for your own files):

    chmod +x *.sh *.py

## Stage 1 — System scan & setup

    ./installer.py

What it does:

- **System scan** — user/sudo, distro, kernel, CPU/RAM/disk, GPU
  (nvidia-smi / rocm-smi / lspci), display server, desktop, timezone/locale
  (language auto-detection), clipboard, notifications, audio
  (PipeWire/PulseAudio), package manager (+ AUR helper), git/docker,
  common tools, network/proxy/local IP
- **AI server detection** — probes `:8080–8082` (llama.cpp), `:11434` (Ollama),
  `:1234/1235` (LM Studio), `:5000/5001` (tg-webui) and shows detected models
- **Interactive setup** — language, main/meta model endpoints (accept detected
  servers or enter manually), context size, tool categories, RAG options, TTS,
  persona, debug level
- **Writes** — `config/config.json`, `config/activetools.txt`,
  `personas/_active/SOUL.md` + `ENGINE.md`, `data/installer_scan.json`
- **Creates** `./venv` and installs `requirements.txt`
- **Generates** systemd user units in `services/` and offers to install them
  into `~/.config/systemd/user/`

The installer is idempotent — re-running it merges with the existing config.

## Stage 2 — First-run onboarding

    ./onboarding.sh                      # wrapper
    # or directly:
    python3 vishva/onboarding.py

What it does:

- loads `data/installer_scan.json` — the agent already knows your system
- runs a ~10-question interview (name, language, style, job, projects,
  habits, taboos)
- saves every relevant answer to long-term memory (`rag_save`)
- creates the mandatory essentials:
  `user: …; system: …; gpu: …` and `agent_home: …`
- merges selected persona snippets + your tool selection into a personal
  persona file in `personas/custom/` (gitignored — your answers stay private)

Flags:

    python3 vishva/onboarding.py --force      # re-run after completion

## Stage 3 — Start

    ./run.sh          # CLI
    ./run_gui.sh      # GUI (installs requirements-gui.txt automatically)
    ./run_bot.sh      # Telegram bot (needs bot token in config.json)

## LLM backend (required before the first chat)

Vishva needs at least one OpenAI-compatible server. Recommended: two
servers — main model on GPU, small meta model on CPU.

→ Full guide with systemd units, Ollama/LM Studio variants and the author's
reference hardware: [BEST_PRACTICES.md](BEST_PRACTICES.md)

Minimal llama.cpp example:

    # main model (full GPU)
    llama-server -m /models/main.gguf -c 32768 -ngl 99 --port 8080

    # meta model (CPU only, small)
    llama-server -m /models/meta.gguf -ngl 0 -c 4096 --port 8090

If you run no separate meta model, disable the meta helpers in
`config/config.json` (pointing them at a large main model is slow and wasteful):

    {
      "meta_model_url": "",
      "rag_meta_enrichment_enabled": false,
      "rag_meta_extraction_enabled": false
    }

## Embedding models (RAG)

Vishva runs fully offline — embedding models are **not downloaded automatically**
and must be placed in `models/` before the first start. Without a model, RAG
silently returns no results.

Recommended (multilingual, best retrieval quality, ~2.2 GB):

    huggingface-cli download intfloat/multilingual-e5-large-instruct \
        --local-dir models/multilingual-e5-large-instruct

Lightweight alternative (~90 MB):

    huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 \
        --local-dir models/all-MiniLM-L6-v2

Set the model in `config/config.json`:

    "rag_embedding_model": "multilingual-e5-large-instruct"

The value is resolved as: absolute path → project-relative path →
`models/<name>/` → HuggingFace name.

## Systemd services (optional, auto-start)

The installer generates `services/telegram-bot.service` and
`services/vishva-daemon.service` with the correct paths for your machine and
offers to install them. To do it manually:

    cp services/telegram-bot.service services/vishva-daemon.service ~/.config/systemd/user/
    # plus your own llama-server units — see BEST_PRACTICES.md
    systemctl --user daemon-reload
    systemctl --user enable --now llama-server llama-server-meta \
        telegram-bot vishva-daemon

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: rich` in installer | `sudo pacman -S python-rich` / `sudo apt install python3-rich` |
| `Permission denied: ./installer.py` | `chmod +x *.sh *.py` |
| RAG returns no results | Embedding model missing — see [Embedding models](#embedding-models-rag) |
| Playwright: "BEWARE: OS not officially supported" | ignore — Ubuntu fallback binaries work fine |
| Scrapling: `apt-get: command not found` | install browser libs via pacman (see below) |
| MCP: `No module named 'mcp.server.fastmcp'` | `./venv/bin/pip install 'mcp'` |
| Onboarding: "no installer data" | run `./installer.py` first |
| Re-run onboarding | `python3 vishva/onboarding.py --force` |
| RAM shows `?` in installer summary | locale issue — installer reads `/proc/meminfo` |

Arch browser libraries for Playwright/Scrapling:

    sudo pacman -S --needed nss nspr atk at-spi2-core cups libdrm libxkbcommon \
        libxcomposite libxdamage libxfixes libxrandr mesa pango cairo alsa-lib

## Manual installation (without installer)

    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    pip install -r requirements-gui.txt   # optional
    playwright install chromium
    cp config/config.example.json config/config.json
    # edit config.json: base_url, model, meta_model_url, context_size, …
