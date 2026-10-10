"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
#import json
#import time
import shlex
#import shutil
import subprocess
#from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, List, Tuple
try:
    import requests
except ImportError:
    requests = None

def _bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
    command = args.get("command", "")
    if not command:
        return {"error": "command is required."}
    ok, warnings = self._validate_bash_command(command)
    if not ok:
        return {"error": " | ".join(warnings), "blocked": True}
    prefix = f"cd {shlex.quote(self.agent_cwd)} && "
    if self.config.get("auto_venv", False):
        venv = self._resolve_venv()
        if venv:
            prefix += f"source {shlex.quote(os.path.join(venv, 'bin', 'activate'))} && "
    full_command = prefix + command
    try:
        result = subprocess.run(
            full_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120)
        output = result.stdout + result.stderr
        if not output:
            output = "(no output)"
        MAX_BASH_OUTPUT = self.config.get("max_bash_output_chars", 5_000)
        MAX_BASH_LINES = self.config.get("max_bash_output_lines", 100)
        lines = output.splitlines()
        if len(lines) > MAX_BASH_LINES:
            head = lines[:50]
            tail = lines[-50:]
            output = "\n".join(head) + f"\n\n... [{len(lines)} Zeilen total, {len(lines)-100} entfernt] ...\n\n" + "\n".join(tail)
        if len(output) > MAX_BASH_OUTPUT:
            half = MAX_BASH_OUTPUT // 2
            output = output[:half] + \
                     f"\n\n... [Output gekürzt: {len(output)} Zeichen total] ...\n\n" + \
                     output[-half:]
        resp = {
            "success": True,
            "returncode": result.returncode,
            "output": output}
        if warnings:
            resp["warnings"] = warnings
        return resp
    except subprocess.TimeoutExpired:
        return {"error": "Timeout after 120 seconds."}
    except Exception as e:
        return {"error": str(e)}



def _validate_bash_command(self, command: str) -> Tuple[bool, List[str]]:
    warnings = []
    cmd_lower = command.lower().strip()
    blocked_exact = [
        "rm -rf /",
        "rm -rf / ",
        "rm -rf /*",]
    for exact in blocked_exact:
        if exact in cmd_lower:
            return False, ["BLOCKED: rm -rf / - system destruction"]
    blocked_patterns = [
        (r"mkfs\.", "mkfs - filesystem formatting"),
        (r"dd\s+if=.+of=/dev/[sh]d[a-z]", "dd to block device - destructive"),
        (r">\s*/dev/[sh]d[a-z]", "redirect to block device - destructive"),
        (r":\(\)\{\s*:\|:\&\s*\};:", "fork bomb detected"),
        (r"chmod\s+-R\s+777\s+/", "chmod 777 / - system-wide permission change"),]
    for pattern, reason in blocked_patterns:
        if re.search(pattern, cmd_lower):
            return False, [f"BLOCKED: {reason}"]
    risky_patterns = [
        (r"sudo\s+rm\s+-rf", "sudo rm -rf - very destructive"),
        (r"rm\s+-rf\s+\*", "rm -rf * - deletes all files in directory"),
        (r"rm\s+-rf\s+~/", "rm -rf ~/ - deletes home directory"),
        (r"curl\s+.*\s*\|\s*bash", "curl | bash - executes remote code"),
        (r"wget\s+.*\s*\|\s*bash", "wget | bash - executes remote code"),
        (r"eval\s*\(", "eval() - executes arbitrary code"),
        (r"system\s*\(", "system() call"),]
    for pattern, reason in risky_patterns:
        if re.search(pattern, cmd_lower):
            warnings.append(f"WARNING: {reason}")
    return True, warnings
