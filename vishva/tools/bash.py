"""Bash tool — execute shell commands in the agent's persistent working directory.

Security model:
  1. _validate_bash_command() blocks the most common destructive patterns and
     flags risky ones (warnings are returned to the model).
  2. The real gate for critical commands is the ToolManager confirmation layer
     (tool_confirm_tools in config.json) — see docs/TOOLS.md.
"""
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Tuple

TIMEOUT_SECONDS = 120


def _bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
    command = args.get("command", "")
    if not command:
        return {"error": "command is required."}

    ok, warnings = self._validate_bash_command(command)
    if not ok:
        return {"error": " | ".join(warnings), "blocked": True}

    # Run inside the agent's persistent working directory
    prefix = f"cd {shlex.quote(self.agent_cwd)} && "

    # Optionally activate a project venv (working_dir/.venv etc.)
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
            timeout=TIMEOUT_SECONDS,
        )
        output = result.stdout + result.stderr
        if not output:
            output = "(no output)"

        max_chars = int(self.config.get("max_bash_output_chars", 5_000))
        max_lines = int(self.config.get("max_bash_output_lines", 100))

        # Line truncation — keep first and last half of max_lines
        lines = output.splitlines()
        if len(lines) > max_lines:
            keep = max(max_lines, 2)
            head_n = keep // 2
            tail_n = keep - head_n
            head = lines[:head_n]
            tail = lines[-tail_n:]
            removed = len(lines) - len(head) - len(tail)
            output = (
                "\n".join(head)
                + f"\n\n... [{len(lines)} lines total, {removed} removed] ...\n\n"
                + "\n".join(tail)
            )

        # Character truncation — keep first and last half of max_chars
        if len(output) > max_chars:
            total = len(output)
            half = max_chars // 2
            output = (
                output[:half]
                + f"\n\n... [output truncated: {total} chars total] ...\n\n"
                + output[-half:]
            )

        resp = {
            "success": True,
            "returncode": result.returncode,
            "output": output,
        }
        if warnings:
            resp["warnings"] = warnings
        return resp

    except subprocess.TimeoutExpired:
        return {"error": f"Timeout after {TIMEOUT_SECONDS} seconds."}
    except Exception as e:
        return {"error": str(e)}


def _is_root_wipe(command: str) -> bool:
    """True if the command runs rm -rf against / itself (root wipe).
    Subpaths like 'rm -rf /tmp/task_x' are allowed."""
    m = re.search(r"rm\s+-[rf]+\s+/", command)
    if not m:
        return False
    rest = command[m.end():].lstrip()
    return rest == "" or rest[0] in " *;|&"


def _validate_bash_command(self, command: str) -> Tuple[bool, List[str]]:
    """Best-effort validation: blocks the most common destructive patterns
    and returns warnings for risky ones. Not a sandbox — the confirmation
    layer is the real protection."""
    warnings: List[str] = []
    cmd_lower = command.lower().strip()

    if _is_root_wipe(cmd_lower):
        return False, ["BLOCKED: rm -rf / - system destruction"]

    blocked_patterns = [
        (r"\bmkfs\b", "mkfs - filesystem formatting"),
        (r"dd\s+if=.+of=/dev/[sh]d[a-z]", "dd to block device - destructive"),
        (r">\s*/dev/[sh]d[a-z]", "redirect to block device - destructive"),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb detected"),
        (r"chmod\s+-R\s+777\s+/", "chmod 777 / - system-wide permission change"),
    ]
    for pattern, reason in blocked_patterns:
        if re.search(pattern, cmd_lower):
            return False, [f"BLOCKED: {reason}"]

    risky_patterns = [
        (r"sudo\s+rm\s+-rf", "sudo rm -rf - very destructive"),
        (r"rm\s+-rf\s+\*", "rm -rf * - deletes all files in directory"),
        (r"rm\s+-rf\s+~/", "rm -rf ~/ - deletes home directory"),
        (r"curl\s+.+\s*\|\s*bash", "curl | bash - executes remote code"),
        (r"wget\s+.+\s*\|\s*bash", "wget | bash - executes remote code"),
        (r"eval\s*\(", "eval() - executes arbitrary code"),
        (r"system\s*\(", "system() call"),
    ]
    for pattern, reason in risky_patterns:
        if re.search(pattern, cmd_lower):
            warnings.append(f"WARNING: {reason}")

    return True, warnings
