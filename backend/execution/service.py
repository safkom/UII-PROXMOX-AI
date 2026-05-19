"""Safe execution service for diagnostic commands.

This module provides a minimal, conservative implementation that:
- Validates commands against an allowlist and blocked tokens
- Rejects shell metacharacters and compound commands
- Executes commands locally with `shell=False` using `subprocess.run`

Remote/SSH execution is intentionally NOT implemented here and should be
added later with explicit SSH key handling and per-target validation.
"""
from __future__ import annotations

import shlex
import subprocess
import re
from typing import Optional


class ExecutionService:
    ALLOWED = {
        "ip",
        "ss",
        "find",
        "ls",
        "grep",
        "cat",
        "tail",
        "df",
        "free",
        "ps",
        "journalctl",
        "systemctl",
    }

    BLOCKED = {"rm", "sudo", "shutdown", "reboot", "chmod", "chown", "apt"}

    SHELL_DANGERS = re.compile(r"[;&|`$<>]")

    def validate(self, command: str) -> list[str]:
        if not command or not command.strip():
            raise ValueError("Empty command")

        if self.SHELL_DANGERS.search(command):
            raise ValueError("Shell metacharacters are not allowed")

        try:
            parts = shlex.split(command)
        except ValueError:
            raise ValueError("Failed to parse command")

        if not parts:
            raise ValueError("Empty command after parsing")

        head = parts[0]
        # disallow absolute binary paths to avoid bypassing allowlist
        if "/" in head:
            raise ValueError("Absolute binary paths are not allowed")

        if head in self.BLOCKED:
            raise ValueError(f"Command '{head}' is blocked")

        if head not in self.ALLOWED:
            raise ValueError(f"Command '{head}' is not in the allowed commands")

        return parts

    def execute(self, command: str, target: Optional[str] = None, timeout: int = 30) -> dict:
        """Execute a validated command locally.

        Args:
            command: shell-like command string (validated)
            target: remote target (not supported in this MVP)
            timeout: seconds before timing out

        Returns:
            dict with returncode, stdout, stderr
        """
        if target:
            raise ValueError("Remote execution (target) is not implemented in this service")

        parts = self.validate(command)

        try:
            proc = subprocess.run(parts, capture_output=True, text=True, timeout=timeout, check=False)
            return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except subprocess.TimeoutExpired as exc:
            return {"returncode": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}