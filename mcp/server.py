#!/usr/bin/env python3
"""
Vishva local MCP server (stdio, FastMCP).
Env:
  VISHVA_MCP_WORKDIR  Basis für relative Pfade (default: cwd)
  VISHVA_MCP_MAX_OUT  Max. Zeichen pro Tool-Output (default: 20000)
  VISHVA_MCP_TIMEOUT  Default-Timeout für Befehle in s (default: 120)
"""
import os
import shlex
import subprocess
from datetime import datetime
from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("vishva-local")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKDIR = os.environ.get("VISHVA_MCP_WORKDIR") or os.path.join(_REPO_ROOT, "working_dir")
MAX_OUT = int(os.environ.get("VISHVA_MCP_MAX_OUT", "20000"))
DEF_TIMEOUT = int(os.environ.get("VISHVA_MCP_TIMEOUT", "120"))

def _cap(text: str) -> str:
    if len(text) > MAX_OUT:
        return text[:MAX_OUT] + f"\n...[output truncated at {MAX_OUT} chars]"
    return text

def _abs(path: str) -> str:
    path = os.path.expanduser(path or ".")
    return path if os.path.isabs(path) else os.path.join(WORKDIR, path)

def _run(cmd: str, timeout: int) -> str:
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True,
                           text=True, timeout=timeout, cwd=WORKDIR)
        out = (r.stdout or "")
        if r.stderr:
            out += ("\n" if out else "") + r.stderr
        if not out.strip():
            return f"(exit code {r.returncode}, no output)"
        return _cap(out)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

@mcp.tool()
async def show_current_path() -> str:
    """Return the server's base working directory."""
    return WORKDIR

@mcp.tool()
async def get_time() -> str:
    """Current local time as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")

@mcp.tool()
async def get_date() -> str:
    """Current date as YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")

@mcp.tool()
async def execute(bashprompt: str, timeout: int = DEF_TIMEOUT) -> str:
    """Run a Linux shell command via bash. Chain commands with &&/;. Returns stdout+stderr."""
    if not bashprompt or not bashprompt.strip():
        return "ERROR: empty command"
    return _run(bashprompt, min(int(timeout), 600))

@mcp.tool()
async def read_file(pfad: str, max_chars: int = MAX_OUT) -> str:
    """Read a file's content. If the path is a directory, returns an ls -la listing."""
    p = _abs(pfad)
    try:
        if os.path.isdir(p):
            r = subprocess.run(["ls", "-la", p], capture_output=True,
                               text=True, timeout=10)
            return _cap(r.stdout or r.stderr)
        if not os.path.isfile(p):
            return f"ERROR: file or directory does not exist: {pfad}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return _cap(f.read(int(max_chars)))
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

@mcp.tool()
async def list_dir(pfad: str = ".") -> str:
    """List a directory (ls -la). Relative paths resolve against the server workdir."""
    return _run("ls -la " + shlex.quote(_abs(pfad)), 30)

@mcp.tool()
async def create_file(path: str, content: str) -> str:
    """Create or overwrite a file with content. Creates parent directories."""
    p = _abs(path)
    try:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File {path} created ({len(content)} chars)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

@mcp.tool()
async def append_file(path: str, content: str) -> str:
    """Append content to a file. Creates the file/parent dirs if missing."""
    p = _abs(path)
    try:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

@mcp.tool()
async def search(pattern: str, pfad: str = ".", case_insensitive: bool = False) -> str:
    """grep-search for a text pattern in files under a directory."""
    flags = "-rn" + ("i" if case_insensitive else "")
    return _run(f"grep {flags} -- {shlex.quote(pattern)} {shlex.quote(_abs(pfad))}", 60)

if __name__ == "__main__":
    mcp.run(transport="stdio")
