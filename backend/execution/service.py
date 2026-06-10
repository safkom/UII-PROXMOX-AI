"""SSH-backed execution service for diagnostic commands.

Runs approved commands on the Proxmox node via SSH using password or
key-based authentication.
"""
from __future__ import annotations

import logging
import re
import shlex
from typing import Optional

import paramiko

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)


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
        "echo",
        "pct",
        "qm",
        "pvesh",
    }

    BLOCKED = {"rm", "sudo", "shutdown", "reboot", "chmod", "chown", "apt"}

    # Binaries whose subcommands range from diagnostics to destruction
    # (e.g. `pct destroy`, `systemctl poweroff`) get a subcommand allowlist.
    ALLOWED_SUBCOMMANDS = {
        "pct": {"list", "config", "status", "start", "stop"},
        "qm": {"list", "config", "status", "start", "stop"},
        "pvesh": {"get", "ls"},
        "systemctl": {
            "status", "show", "cat", "is-active", "is-enabled", "is-failed",
            "list-units", "list-unit-files", "start", "stop", "restart",
        },
    }

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

        allowed_subcommands = self.ALLOWED_SUBCOMMANDS.get(head)
        if allowed_subcommands is not None:
            subcommand = parts[1] if len(parts) > 1 else ""
            if subcommand not in allowed_subcommands:
                raise ValueError(
                    f"'{head} {subcommand}'".strip()
                    + f" is not an allowed subcommand (allowed: {', '.join(sorted(allowed_subcommands))})"
                )

        return parts

    def _normalize_command(self, command: str) -> str:
        parts = self.validate(command)
        if hasattr(shlex, "join"):
            return shlex.join(parts)
        return " ".join(shlex.quote(part) for part in parts)

    @staticmethod
    def _get_ssh_client(settings) -> paramiko.SSHClient:
        """Create an authenticated SSH client to the Proxmox node."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        host = settings.proxmox_host_ip or settings.proxmox_ip or "192.168.1.147"
        user = settings.proxmox_user.split("@")[0]  # strip realm (e.g. "root@pam" -> "root")

        connect_kwargs: dict = {
            "hostname": host,
            "username": user,
            "timeout": 10,
            "look_for_keys": False,
            "allow_agent": False,
        }

        # Use password authentication
        if settings.proxmox_password:
            connect_kwargs["password"] = settings.proxmox_password
        else:
            raise RuntimeError(
                "No SSH credentials available. Set PROXMOX_PASSWORD in .env."
            )

        client.connect(**connect_kwargs)
        return client

    def execute(self, command: str, target: Optional[str] = None, timeout: int = 30) -> dict:
        """Execute a validated command on the Proxmox node via SSH.

        Args:
            command: shell-like command string (validated)
            target: ignored for SSH (node is determined by settings)
            timeout: seconds before timing out

        Returns:
            dict with returncode, stdout, stderr
        """
        normalized_command = self._normalize_command(command)
        settings = get_settings()
        node = settings.proxmox_node or "proxmox"

        client = self._get_ssh_client(settings)
        try:
            logger.info(f"Executing on {node}: {normalized_command}")
            stdin, stdout, stderr = client.exec_command(
                normalized_command, timeout=timeout
            )
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")

            return {
                "returncode": exit_code,
                "stdout": out.rstrip(),
                "stderr": err.rstrip(),
                "node": node,
            }
        except Exception as exc:
            logger.error(f"SSH execution failed: {exc}")
            raise
        finally:
            try:
                client.close()
            except Exception:
                logger.debug("Failed to close SSH client", exc_info=True)