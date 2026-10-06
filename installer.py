#!/usr/bin/env python3
"""
Vishva Installer — system scan + interactive setup
Writes config/config.json, config/activetools.txt and activates a persona.
Usage:  python3 installer.py
"""
import os
import sys
import json
import shutil
import getpass
import datetime
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE = Path(__file__).parent.resolve()


# ------------------------------------------------------------ helpers
def _ensure_rich():
    try:
        import rich  # noqa
    except ImportError:
        print("[installer] installing rich …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "rich", "-q"])


_ensure_rich()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm, IntPrompt

console = Console()


def run(cmd: str, timeout: int = 10) -> Tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def which_first(*names: str) -> Optional[str]:
    for n in names:
        if shutil.which(n):
            return n
    return None


# ------------------------------------------------------------ system scan
def scan_system() -> Dict[str, Any]:
    info: Dict[str, Any] = {}

    console.print(Panel("[bold cyan]System Scan[/bold cyan]", expand=False))

    console.print("[yellow]• user & groups[/yellow]")
    ok, out = run("whoami");        info["user"]   = out if ok else "?"
    ok, out = run("id -Gn");        info["groups"] = out.split() if ok else []
    ok, out = run("id -u");         info["is_root"]= out == "0"
    ok, out = run("sudo -n true 2>&1 && echo SUDO_OK || echo SUDO_NEEDS_PW")
    info["sudo_passwordless"] = "SUDO_OK" in out

    console.print("[yellow]• distro / kernel[/yellow]")
    ok, out = run("cat /etc/*release 2>/dev/null | grep -E '^(ID|VERSION_ID|PRETTY_NAME)='")
    info["os"] = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info["os"][k] = v.strip('"')
    ok, out = run("uname -sr"); info["kernel"] = out if ok else "?"

    console.print("[yellow]• hardware[/yellow]")
    ok, out = run("nproc");  info["cpu_cores"] = int(out) if ok and out.isdigit() else 1
    # RAM: sprachunabhängig via /proc/meminfo
    ok, out = run("awk '/^MemTotal:/{printf \"%.1f GiB\", $2/1024/1024}' /proc/meminfo")
    if ok and out:
        info["ram"] = out
    else:
        ok2, out2 = run("free -h | awk 'NR==2{print $2}'")  # Fallback: 2. Zeile
        info["ram"] = out2 if ok2 and out2 else "?"
    ok, out = run("df -h / | awk 'NR==2{print $4}'");   info["disk_free"] = out or "?"

    console.print("[yellow]• GPU[/yellow]")

    # --- GPU: nvidia -> rocm-smi (JSON, dann Text) -> lspci ---
    gpu = ("none", "")
    ok, out = run("nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1")
    if ok and out:
        gpu = ("nvidia", out.strip())
    else:
        series = ""
        # 1) rocm-smi JSON (robusteste Quelle)
        ok, out = run("rocm-smi --showproductname --json 2>/dev/null")
        if ok and out:
            try:
                for card in json.loads(out).values():
                    if isinstance(card, dict):
                        for k, v in card.items():
                            if k.lower() == "card series" and v:
                                series = str(v)
                                break
                    if series:
                        break
            except Exception:
                series = ""
        # 2) rocm-smi Text: Wert hinter "Card series:" extrahieren
        if not series:
            ok, out = run("rocm-smi --showproductname 2>/dev/null | "
                          "sed -n 's/.*[Cc]ard series:[[:space:]]*//p' | head -1")
            series = out.strip() if ok else ""
        if series:
            gpu = ("amd", series)
        else:
            # 3) generischer lspci-Fallback
            ok, out = run("lspci | grep -E 'VGA|3D' | head -1")
            if ok and out:
                name = out.split(":", 2)[-1].strip() if out.count(":") >= 2 else out.strip()
                low = name.lower()
                vendor = ("nvidia" if "nvidia" in low
                          else "amd" if ("radeon" in low or "amd" in low)
                          else "other")
                gpu = (vendor, name)
    info["gpu"] = gpu

    console.print("[yellow]• display / desktop[/yellow]")
    info["session"] = os.environ.get("XDG_SESSION_TYPE", "?")
    info["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    ok, out = run("hostname");         info["hostname"] = out if ok else "?"
    ok, out = run("timedatectl show -p Timezone --value 2>/dev/null || echo $TZ")
    info["timezone"] = out if ok and out else "?"

    console.print("[yellow]• locale / language[/yellow]")
    ok, out = run("locale | awk -F= '/^LANG=/{print $2}'")
    info["locale"] = out if ok and out else "?"
    detected = "en"
    for key in (info["locale"], info["timezone"]):
        if key:
            low = key.lower()
            if low.startswith("de") or "berlin" in low or "vienna" in low:
                detected = "de"; break
            if low.startswith("fr") or "paris" in low:   detected = "fr"; break
            if low.startswith("es") or "madrid" in low:  detected = "es"; break
            if low.startswith("it") or "rome" in low:    detected = "it"; break
    info["detected_language"] = detected

    console.print("[yellow]• tools[/yellow]")
    info["clipboard"] = which_first("wl-copy", "xclip", "xsel")
    info["notify_send"] = shutil.which("notify-send") is not None
    ok, out = run("python3 --version"); info["python"] = out if ok else "?"
    ok, out = run("pip --version 2>/dev/null || pip3 --version 2>/dev/null")
    info["pip"] = out if ok else "?"
    info["venv_exists"] = (BASE / "venv").exists()

    info["package_manager"] = which_first("apt", "nala", "dnf", "pacman", "zypper", "pkg")
    if info["package_manager"] == "pacman":
        info["aur_helper"] = which_first("yay", "paru")
    info["editor"] = which_first("nano", "vim", "vi")
    ok, out = run("git --version"); info["git"] = out if ok else None
    ok1, _ = run("git config user.name"); ok2, _ = run("git config user.email")
    info["git_configured"] = ok1 and ok2
    info["docker"] = shutil.which("docker") is not None
    info["tools"] = {
        t: shutil.which(t) is not None
        for t in ["curl", "wget", "jq", "ffmpeg", "rsync", "htop"]
    }

    console.print("[yellow]• audio / network[/yellow]")
    ok1, _ = run("pw-cli info 2>/dev/null | head -1")
    ok2, _ = run("pactl info 2>/dev/null | head -1")
    info["audio"] = "pipewire" if ok1 else ("pulseaudio" if ok2 else "unknown")
    ok, out = run("curl -sI https://example.com | head -1")
    info["internet"] = ok and "200" in out
    ok, _ = run("env | grep -i proxy"); info["proxy"] = ok
    ok, out = run("ip a | awk '/inet / && !/127.0.0.1/{print $2; exit}' | cut -d/ -f1")
    if not ok or not out:
        ok, out = run("ifconfig | awk '/inet / && !/127.0.0.1/{print $2; exit}'")
    info["local_ip"] = out if ok else "?"

    return info


# ------------------------------------------------------------ AI server detection
def detect_ai_servers() -> Dict[str, Dict[str, Any]]:
    console.print(Panel("[bold cyan]AI Server Detection[/bold cyan]", expand=False))
    found: Dict[str, Dict[str, Any]] = {}
    probes = [
        ("llama.cpp", [8080, 8081, 8082]),
        ("ollama",    [11434]),
        ("lmstudio",  [1234, 1235]),
        ("tg-webui",  [5000, 5001]),
    ]
    for name, ports in probes:
        for port in ports:
            ok, out = run(f"curl -s http://localhost:{port}/v1/models")
            if not ok or not out:
                continue
            model = "unknown"
            # naive: erster "id"-Wert
            import re
            m = re.search(r'"id"\s*:\s*"([^"]+)"', out)
            if m:
                model = m.group(1)
            found[name] = {"port": port,
                           "url": f"http://localhost:{port}/v1",
                           "model": model}
            console.print(f"  [green]✓[/green] {name} on :{port}  model={model}")
            break
    if not found:
        console.print("  [yellow]no AI servers detected[/yellow]")
    return found


# ------------------------------------------------------------ summary
def show_summary(info: Dict[str, Any], servers: Dict[str, Dict[str, Any]]):
    t = Table(title="System Summary", show_header=True, header_style="bold cyan")
    t.add_column("Component", style="cyan")
    t.add_column("Value", style="green")
    t.add_row("User", info["user"])
    t.add_row("Root", "yes" if info["is_root"] else "no")
    t.add_row("Sudo (no pw)", "yes" if info["sudo_passwordless"] else "no")
    t.add_row("OS", info["os"].get("PRETTY_NAME", "?"))
    t.add_row("Kernel", info["kernel"])
    t.add_row("CPU", str(info["cpu_cores"]))
    t.add_row("RAM", info["ram"])
    t.add_row("Disk free", info["disk_free"])
    t.add_row("GPU", f"{info['gpu'][0]}: {info['gpu'][1]}")
    t.add_row("Session", info["session"])
    t.add_row("Desktop", info["desktop"])
    t.add_row("Timezone", info["timezone"])
    t.add_row("Detected lang", info["detected_language"].upper())
    t.add_row("Clipboard", info["clipboard"] or "—")
    t.add_row("notify-send", "yes" if info["notify_send"] else "no")
    t.add_row("Python", info["python"])
    t.add_row("Venv", "exists" if info["venv_exists"] else "missing")
    t.add_row("Package mgr", info["package_manager"] or "—")
    if info["package_manager"] == "pacman":
        t.add_row("AUR helper", info.get("aur_helper") or "—")
    t.add_row("Internet", "yes" if info["internet"] else "no")
    t.add_row("Local IP", info["local_ip"])
    t.add_row("Audio", info["audio"])
    console.print(t)
    if servers:
        console.print("\n[bold cyan]Detected AI servers:[/bold cyan]")
        for name, s in servers.items():
            console.print(f"  [green]✓[/green] {name}: {s['url']} (model: {s['model']})")


# ------------------------------------------------------------ interactive setup
def interactive_setup(info: Dict[str, Any], servers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    console.print(Panel("[bold cyan]Interactive Setup[/bold cyan]", expand=False))
    cfg: Dict[str, Any] = {}

    # --- language ---
    console.print("\n[yellow]Language[/yellow]")
    cfg["language"] = Prompt.ask(
        "Language for Vishva",
        choices=["de", "en", "fr", "es", "it"],
        default=info["detected_language"])

    # --- Working Directory & Project Root (absolut) ---
    console.print("\n[yellow]Working Directory[/yellow]")
    default_workdir = str(BASE / "working_dir")
    wd = Prompt.ask("Agent working directory", default=default_workdir)
    if wd:
        wd = os.path.abspath(os.path.expanduser(wd))
        os.makedirs(wd, exist_ok=True)
        cfg["agent_workdir"] = wd
        cfg["project_root"] = str(BASE)
        console.print(f"[green]✓[/green] agent_workdir: {wd}")

    # --- AI servers ---
    console.print("\n[yellow]AI Servers[/yellow]")
    if servers:
        names = list(servers.keys())
        use = Confirm.ask("Use detected server(s)?", default=True)
        if use:
            main = servers[names[0]]
            cfg["base_url"]  = main["url"]
            cfg["model"]     = main["model"]
            cfg["api_key"]   = "ollama"
            if len(names) > 1 and Confirm.ask(
                    f"Use '{names[1]}' as meta model?", default=True):
                meta = servers[names[1]]
                cfg["meta_model_url"]   = meta["url"]
                cfg["meta_model_name"]  = meta["model"]
                cfg["meta_model_timeout"] = 60
        else:
            cfg["base_url"] = Prompt.ask("Main model URL", default="http://localhost:8080/v1")
            cfg["model"]    = Prompt.ask("Main model name", default="llama3.1")
            cfg["api_key"]  = Prompt.ask("API key", default="ollama")
    else:
        cfg["base_url"] = Prompt.ask("Main model URL", default="http://localhost:8080/v1")
        cfg["model"]    = Prompt.ask("Main model name", default="llama3.1")
        cfg["api_key"]  = Prompt.ask("API key", default="ollama")

    if Confirm.ask("Configure a separate meta model?",
                   default="meta_model_url" not in cfg):
        cfg["meta_model_url"]   = Prompt.ask("Meta model URL", default="http://localhost:8090/v1")
        cfg["meta_model_name"]  = Prompt.ask("Meta model name", default="meta")
        cfg["meta_model_timeout"] = 60

    # --- context ---
    console.print("\n[yellow]Context[/yellow]")
    cfg["context_size"] = IntPrompt.ask(
        "Context size (tokens)",
        default=32768,
        choices=["4096", "8192", "16384", "32768", "65536"])

    # --- tool selection ---
    console.print("\n[yellow]Tool Categories[/yellow]")
    cats = {
        "filesystem": ["bash","read_file","write_file","edit_file","list_dir","cd","lint_code","read_cache"],
        "web":        ["web_read","web_search","product_search"],
        "system":     ["sys_update","sys_clean","vol_ctl","brightness_ctl","turnoff_screen"],
        "scheduler":  ["sched_task","ls_tasks","cancel_task"],
        "media":      ["spotify","speak","send_image","send_file","news_digest"],
        "rag":        ["rag_search","rag_save","rag_update","rag_delete","rag_reindex","rag_essential"],
        "agent":      ["subagent","bg_task","weather"],
    }
    enabled: List[str] = []
    for cat, tools in cats.items():
        if Confirm.ask(f"Enable [cyan]{cat}[/cyan] tools?", default=True):
            enabled.extend(tools)
    cfg["enabled_tools"] = enabled

    # --- RAG ---
    console.print("\n[yellow]RAG[/yellow]")
    cfg["rag_enabled"] = Confirm.ask("Enable RAG?", default=True)
    if cfg["rag_enabled"]:
        cfg["rag_meta_enrichment_enabled"] = Confirm.ask("Auto-inject relevant memory?", default=True)
        cfg["rag_meta_extraction_enabled"] = Confirm.ask("Auto-extract from turns?", default=True)
    cfg["rag_top_k"] = IntPrompt.ask(
        "RAG results to inject", default=3, choices=["1", "2", "3", "5"])

    # --- TTS ---
    console.print("\n[yellow]TTS[/yellow]")
    cfg["tts_enabled"] = Confirm.ask("Enable TTS?", default=False)
    if cfg["tts_enabled"]:
        cfg["tts_server_url"] = Prompt.ask("TTS server URL", default="http://localhost:8091")
        cfg["tts_voice"]      = Prompt.ask("TTS voice", default="kerstin")

    # --- Integration: Telegram ---
    console.print("\n[yellow]Telegram Setup[/yellow]")
    if Confirm.ask("Configure Telegram bot?", default=False):
        token = getpass.getpass("Bot token (hidden): ").strip()
        chat_id = Prompt.ask("Telegram chat ID", default="").strip()
        if token:
            cfg["bot_token"] = token
        if chat_id:
            cfg["chat_id"] = chat_id
        if not token and not chat_id:
            console.print("[yellow]Nothing entered — skipping Telegram.[/yellow]")

    # --- Integration: Spotify ---
    console.print("\n[yellow]Spotify Setup[/yellow]")
    if Confirm.ask("Configure Spotify?", default=False):
        sp_id = Prompt.ask("Spotify client ID", default="").strip()
        sp_secret = getpass.getpass("Spotify client secret (hidden): ").strip()
        if sp_id:
            cfg["spotify_client_id"] = sp_id
        if sp_secret:
            cfg["spotify_client_secret"] = sp_secret
        if sp_id or sp_secret:
            cfg["_spotify_setup"] = True      # internes Flag, wird vor dem Schreiben entfernt
        else:
            console.print("[yellow]Nothing entered — skipping Spotify.[/yellow]")

    # --- persona ---
    console.print("\n[yellow]Persona[/yellow]")
    personas_dir = BASE / "personas"
    templates = [f.stem for f in personas_dir.glob("*.md")
                 if f.is_file() and f.stem not in ("default", "default_bak")]
    if templates:
        for i, p in enumerate(templates, 1):
            console.print(f"  {i}. {p}")
        sel = IntPrompt.ask("Select (0 = default)",
                            default=0,
                            choices=[str(i) for i in range(len(templates) + 1)])
        cfg["persona"] = templates[sel - 1] if sel > 0 else "default"
    else:
        cfg["persona"] = "default"

    # --- debug ---
    cfg["debug"] = IntPrompt.ask(
        "Debug level (0=off, 1=minimal, 2=verbose)",
        default=0, choices=["0", "1", "2"])

    return cfg


# ------------------------------------------------------------ venv
def ensure_venv():
    venv_path = BASE / "venv"
    if venv_path.exists():
        console.print("[green]✓[/green] venv already exists")
        return
    if not Confirm.ask("Create virtual environment now?", default=True):
        return
    console.print(f"[yellow]creating {venv_path} …[/yellow]")
    r = subprocess.run([sys.executable, "-m", "venv", str(venv_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"[red]✗[/red] venv creation failed: {r.stderr}")
        return
    console.print("[green]✓[/green] venv created")
    req = BASE / "requirements.txt"
    if req.exists():
        console.print("[yellow]installing requirements …[/yellow]")
        pip = venv_path / "bin" / "pip"
        subprocess.run([str(pip), "install", "-r", str(req), "-q"])
        console.print("[green]✓[/green] requirements installed")


# ------------------------------------------------------------ write config
def write_config(cfg: Dict[str, Any], info: Dict[str, Any],
                 servers: Dict[str, Any]):
    console.print(Panel("[bold cyan]Writing Configuration[/bold cyan]", expand=False))
    cfg_dir = BASE / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"

    # Basis: vorhandene config.json, sonst config.example.json als Vorlage
    existing: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    else:
        example = cfg_dir / "config.example.json"
        if example.exists():
            try:
                with open(example, encoding="utf-8") as f:
                    existing = json.load(f)
                console.print("[cyan]config.json aus config.example.json erstellt[/cyan]")
            except Exception:
                existing = {}

    # Diese Keys dürfen neu angelegt werden (Setup-Essentials + Credentials).
    # Alles andere wird NUR gesetzt, wenn der Key bereits existiert.
    ALLOW_NEW = {
        "base_url", "model", "api_key",
        "meta_model_url", "meta_model_name", "meta_model_timeout",
        "context_size", "compression_threshold", "language", "debug",
        "rag_enabled", "rag_top_k",
        "rag_meta_enrichment_enabled", "rag_meta_extraction_enabled",
        "tts_enabled", "tts_server_url", "tts_voice",
        "bot_token", "chat_id",
        "spotify_client_id", "spotify_client_secret", "spotify_redirect_uri",
    }

    updated, added, skipped = [], [], []
    for key, value in cfg.items():
        if key in existing:
            existing[key] = value
            updated.append(key)
        elif key in ALLOW_NEW:
            existing[key] = value
            added.append(key)
        else:
            skipped.append(key)

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    console.print(f"[green]✓[/green] wrote {cfg_path}")
    console.print(f"  [green]updated:[/green] {', '.join(sorted(updated)) or '—'}")
    if added:
        console.print(f"  [cyan]added:[/cyan] {', '.join(sorted(added))}")
    if skipped:
        console.print(f"  [yellow]nicht angehängt (Key nicht vorhanden):[/yellow] "
                      f"{', '.join(sorted(skipped))}")

    # activetools
    if "enabled_tools" in cfg:
        at_path = cfg_dir / "activetools.txt"
        with open(at_path, "w") as f:
            for tool in sorted(cfg["enabled_tools"]):
                f.write(tool + "\n")
        console.print(f"[green]✓[/green] wrote {at_path} "
                      f"({len(cfg['enabled_tools'])} tools)")

    # persona
    if "persona" in cfg:
        persona = cfg["persona"]
        src = BASE / "personas" / f"{persona}.md"
        if src.exists():
            import re as _re
            text = src.read_text(encoding="utf-8")

            def sect(name: str) -> str:
                m = _re.search(rf"^---{name}---\s*\n(.*?)(?=^---|\Z)",
                               text, flags=_re.MULTILINE | _re.DOTALL)
                return m.group(1).strip() if m else ""

            active = BASE / "personas" / "_active"
            active.mkdir(parents=True, exist_ok=True)
            (active / "SOUL.md").write_text(sect("SOUL") or f"# {persona}",
                                            encoding="utf-8")
            (active / "ENGINE.md").write_text(sect("ENGINE") or "# engine",
                                              encoding="utf-8")
            console.print(f"[green]✓[/green] persona '{persona}' activated")

    # Installer-Scan für Onboarding persistieren
    scan_path = BASE / "data" / "installer_scan.json"
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(scan_path, "w", encoding="utf-8") as f:
            json.dump({
                "system": info,
                "ai_servers": servers,
                "config": cfg,
                "timestamp": datetime.datetime.now().isoformat(),
            }, f, indent=2, ensure_ascii=False)
        console.print(f"[green]✓[/green] scan data written to {scan_path}")
    except Exception as e:
        console.print(f"[yellow]scan persistence skipped: {e}[/yellow]")

def write_service_files(info: Dict[str, Any]):
    """Erzeugt systemd-User-Units mit korrekten Pfaden + UID im services/ dir."""
    console.print(Panel("[bold cyan]Systemd Service Files[/bold cyan]", expand=False))
    svc_dir = BASE / "services"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (BASE / "tmp").mkdir(exist_ok=True)   # für bot.log / bot_error.log

    uid = os.getuid()
    base = str(BASE)
    session = (info.get("session") or "").lower()

    # --- Session-Env für den Daemon (notify-send / Terminal-Öffnung) ---
    env_lines: List[str] = []
    wayland = os.environ.get("WAYLAND_DISPLAY") or ("wayland-0" if session == "wayland" else "")
    display = os.environ.get("DISPLAY") or (":0" if session == "x11" else "")

    if session == "wayland" or (wayland and session != "x11"):
        env_lines.append(f"Environment=WAYLAND_DISPLAY={wayland or 'wayland-0'}")
        if display:                                   # XWayland-Fallback
            env_lines.append(f"Environment=DISPLAY={display}")
    elif session == "x11" or display:
        env_lines.append(f"Environment=DISPLAY={display or ':0'}")
    else:                                             # headless/unbekannt
        env_lines.append("Environment=DISPLAY=:0")
        env_lines.append("Environment=WAYLAND_DISPLAY=wayland-0")
    env_lines.append(f"Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus")
    session_env = "\n".join(env_lines)

    # --- Units ---
    bot_unit = f"""[Unit]
Description=Vishva Telegram Bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={base}
ExecStart={base}/run_bot.sh
Restart=on-failure
RestartSec=60
StandardOutput=append:{base}/tmp/bot.log
StandardError=append:{base}/tmp/bot_error.log

[Install]
WantedBy=default.target
"""

    daemon_unit = f"""[Unit]
Description=Vishva Scheduler Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={base}
ExecStart={base}/venv/bin/python -m vishva.scheduler_daemon
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
{session_env}

[Install]
WantedBy=default.target
"""

    for fname, content in (("telegram-bot.service", bot_unit),
                           ("vishva-daemon.service", daemon_unit)):
        path = svc_dir / fname
        path.write_text(content, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {path}")

    # ExecStart braucht das Executable-Bit auf run_bot.sh
    for sh in ("run_bot.sh", "run.sh", "run_gui.sh"):
        f = BASE / sh
        if f.exists():
            try:
                f.chmod(0o755)
            except Exception:
                pass

    # Optional: direkt installieren + daemon-reload
    try:
        install_now = Confirm.ask(
            "Install services to ~/.config/systemd/user now?", default=True)
    except (EOFError, KeyboardInterrupt):
        install_now = False

    if install_now:
        user_dir = Path.home() / ".config" / "systemd" / "user"
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            for fname in ("telegram-bot.service", "vishva-daemon.service"):
                shutil.copy(svc_dir / fname, user_dir / fname)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            console.print(f"[green]✓[/green] installed to {user_dir} + daemon-reload")
            console.print("[yellow]Enable/start:[/yellow] "
                          "systemctl --user enable --now telegram-bot vishva-daemon")
        except Exception as e:
            console.print(f"[red]✗[/red] install failed: {e}")


def run_spotify_auth():
    script = BASE / "scripts" / "spotify_auth_server.py"
    if not script.exists():
        console.print(f"[yellow]{script} not found — skipping Spotify auth.[/yellow]")
        return
    py = BASE / "venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    console.print("\n[magenta]Starting Spotify authentication …[/magenta]")
    console.print("Open the printed URL in your browser and authorize.")
    try:
        subprocess.run([str(py), str(script)], cwd=str(BASE))
    except KeyboardInterrupt:
        console.print("[yellow]Spotify auth cancelled.[/yellow]")

# ------------------------------------------------------------ main
def main():
    console.print(Panel(
        "[bold cyan]Vishva Installer[/bold cyan]\n"
        "system scan + interactive setup", expand=False))

    info = scan_system()
    servers = detect_ai_servers()
    show_summary(info, servers)

    if not Confirm.ask("\nProceed with interactive setup?", default=True):
        console.print("[yellow]cancelled.[/yellow]")
        return 0

    cfg = interactive_setup(info, servers)
    spotify_setup = cfg.pop("_spotify_setup", False)   # internes Flag entfernen
    ensure_venv()
    write_config(cfg, info, servers)
    write_service_files(info)

    # Spotify-Auth direkt im Anschluss anbieten (Config-Keys sind jetzt geschrieben)
    if spotify_setup and cfg.get("spotify_client_id"):
        try:
            run_now = Confirm.ask("\nRun Spotify authentication now?", default=True)
        except (EOFError, KeyboardInterrupt):
            run_now = False
        if run_now:
            run_spotify_auth()

    console.print(Panel(
        "[bold green]Installation complete![/bold green]\n"
        "Continue with first run interview [cyan]./onboarding.sh[/cyan]\n"
        "Or start Vishva with [cyan]./run.sh[/cyan]",
        expand=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
