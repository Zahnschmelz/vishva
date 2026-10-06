#!/usr/bin/env python3
"""
mcp_client.py — minimaler synchroner MCP-Client für Vishva.
Supports stdio servers (subprocess) and Streamable-HTTP servers.
No extra deps besides requests (HTTP only).
"""
import json
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional
from .paths import BASE_DIR

try:
    import requests
except ImportError:
    requests = None

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "vishva", "version": "1.0"}

def load_mcp_servers(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge mcp_servers (config.json) + config/mcp.json (Standard-Format)."""
    servers = dict(config.get("mcp_servers", {}) or {})
    path = os.path.join(BASE_DIR, "config", "mcp.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, cfg in (data.get("mcpServers", {}) or {}).items():
                servers.setdefault(name, cfg)
        except Exception as e:
            print(f"[MCP] config/mcp.json nicht lesbar: {e}")
    return servers

class MCPError(Exception):
    pass


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


class StdioConnection:
    """JSON-RPC über stdin/stdout eines Subprozesses."""

    def __init__(self, name: str, cfg: Dict[str, Any], timeout: int):
        self.name = name
        self.cfg = cfg
        self.timeout = timeout
        self.proc = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._events: Dict[int, threading.Event] = {}
        self._dead = False

    def start(self):
        cmd = self.cfg.get("command", "")
        if not cmd:
            raise MCPError(f"{self.name}: 'command' fehlt")
        # which() nur für nackte Befehle; Pfade (mit /) lässt Popen über cwd lösen
        if os.sep not in cmd and not shutil.which(cmd):
            raise MCPError(f"{self.name}: command '{cmd}' not found in PATH")
        env = os.environ.copy()
        env.update(self.cfg.get("env", {}) or {})
        self.proc = subprocess.Popen(
            [cmd] + list(self.cfg.get("args", []) or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
            cwd=self.cfg.get("cwd") or BASE_DIR)
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._handshake()

    def stop(self):
        self._dead = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def _read_loop(self):
        while not self._dead and self.proc and self.proc.stdout:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._events:
                self._pending[mid] = msg
                self._events[mid].set()

    def _send(self, payload: Dict[str, Any]):
        with self._lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict] = None,
                timeout: Optional[int] = None) -> Any:
        if not self.alive():
            raise MCPError(f"{self.name}: server process dead")
        mid = self._next_id
        self._next_id += 1
        ev = threading.Event()
        self._events[mid] = ev
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params or {}})
        if not ev.wait(timeout or self.timeout):
            self._events.pop(mid, None)
            raise MCPError(f"{self.name}: timeout on '{method}'")
        self._events.pop(mid, None)
        msg = self._pending.pop(mid, {})
        if "error" in msg:
            raise MCPError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def notify(self, method: str, params: Optional[Dict] = None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _handshake(self):
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO})
        self.notify("notifications/initialized")


class HttpConnection:
    """Streamable HTTP (POST JSON-RPC, optional SSE-Responses)."""

    def __init__(self, name: str, cfg: Dict[str, Any], timeout: int):
        if requests is None:
            raise MCPError("requests not installed (HTTP-MCP needs it)")
        self.name = name
        self.url = cfg.get("url", "")
        self.headers = dict(cfg.get("headers", {}) or {})
        self.timeout = timeout
        self.session_id = None
        self._lock = threading.Lock()
        self._next_id = 1

    def start(self):
        if not self.url:
            raise MCPError(f"{self.name}: 'url' fehlt")
        self._handshake()

    def stop(self):
        pass

    def alive(self) -> bool:
        return True

    def _post(self, payload: Dict[str, Any], expect: bool = True) -> Any:
        hdr = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
        hdr.update(self.headers)
        if self.session_id:
            hdr["Mcp-Session-Id"] = self.session_id
        with self._lock:
            r = requests.post(self.url, json=payload, headers=hdr,
                              timeout=self.timeout)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if not expect:
            return None
        r.raise_for_status()
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            return self._parse_sse(r.text)
        return r.json()

    @staticmethod
    def _parse_sse(text: str) -> Any:
        last = None
        for line in text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    last = json.loads(data)
                except Exception:
                    continue
        return last if last is not None else {}

    def request(self, method: str, params: Optional[Dict] = None,
                timeout: Optional[int] = None) -> Any:
        mid = self._next_id
        self._next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": mid, "method": method,
                          "params": params or {}})
        if not isinstance(msg, dict):
            raise MCPError(f"{self.name}: empty/invalid response on '{method}'")
        if "error" in msg:
            raise MCPError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def notify(self, method: str, params: Optional[Dict] = None):
        try:
            self._post({"jsonrpc": "2.0", "method": method,
                        "params": params or {}}, expect=False)
        except Exception:
            pass

    def _handshake(self):
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO})
        self.notify("notifications/initialized")


class MCPManager:
    """Verwaltet konfigurierte MCP-Server und exponiert deren Tools."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.timeout = int(self.config.get("mcp_tool_timeout", 60) or 60)
        self.max_desc = int(self.config.get("mcp_max_desc_chars", 300) or 300)
        self.servers: Dict[str, Dict[str, Any]] = load_mcp_servers(self.config)
        self.conns: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []
        self.index: Dict[str, tuple] = {}
        self._debug = bool(self.config.get("debug", 0))

    def _connect(self, name: str):
        conn = self.conns.get(name)
        if conn is not None and conn.alive():
            return conn
        cfg = self.servers.get(name, {})
        conn = HttpConnection(name, cfg, self.timeout) if "url" in cfg \
            else StdioConnection(name, cfg, self.timeout)
        conn.start()
        self.conns[name] = conn
        if self._debug:
            print(f"[MCP] connected: {name}")
        return conn

    def _drop(self, name: str):
        conn = self.conns.pop(name, None)
        if conn:
            try:
                conn.stop()
            except Exception:
                pass

    def refresh_tools(self) -> List[Dict[str, Any]]:
        self.tools, self.index = [], {}
        for name, cfg in self.servers.items():
            if cfg.get("enabled", True) is False or cfg.get("disabled", False):
                continue
            try:
                conn = self._connect(name)
                res = conn.request("tools/list", {})
                for t in res.get("tools", []):
                    tname = t.get("name", "")
                    if not tname:
                        continue
                    prefixed = f"mcp__{_sanitize(name)}__{_sanitize(tname)}"
                    desc = (t.get("description", "") or "").strip()
                    if len(desc) > self.max_desc:
                        desc = desc[:self.max_desc] + "…"
                    schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                    self.tools.append({
                        "type": "function",
                        "function": {
                            "name": prefixed,
                            "description": desc or f"MCP tool {name}/{tname}",
                            "parameters": schema}})
                    self.index[prefixed] = (name, tname)
            except Exception as e:
                if self._debug:
                    print(f"[MCP] {name}: refresh failed: {type(e).__name__}: {e}")
                self._drop(name)
        return self.tools

    def call(self, prefixed: str, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self.index.get(prefixed)
        if not target:
            self.refresh_tools()
            target = self.index.get(prefixed)
        if not target:
            return {"error": f"MCP tool unknown: {prefixed}"}
        server, tool = target
        try:
            conn = self._connect(server)
            res = conn.request("tools/call",
                               {"name": tool, "arguments": args or {}})
        except Exception as e:
            self._drop(server)
            return {"error": f"MCP call failed ({server}/{tool}): "
                             f"{type(e).__name__}: {e}"}
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", [])
                            if isinstance(c, dict) and c.get("type") == "text")
            return {"success": False, "error": text or "MCP tool reported error"}
        parts = []
        for c in res.get("content", []):
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "resource":
                    r = c.get("resource", {}) or {}
                    parts.append(r.get("text", "") or str(r.get("blob", ""))[:2000])
        out: Dict[str, Any] = {
            "success": True, "server": server, "tool": tool,
            "content": "\n".join(p for p in parts if p)}
        if res.get("structuredContent") is not None:
            out["structured"] = res["structuredContent"]
        return out

    def shutdown(self):
        for name in list(self.conns):
            self._drop(name)
