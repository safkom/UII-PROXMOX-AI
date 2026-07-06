"""Execution service for approved diagnostic commands.

Guest power actions (pct/qm start/stop) run through the Proxmox API as
async tasks; all other allow-listed commands run on the Proxmox node via
SSH using password authentication.
"""
from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Optional
from urllib.parse import urlparse

import paramiko

from backend.config.settings import get_settings
from backend.proxmox.client import ProxmoxClient

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

    # Some allow-listed binaries have flags that turn a read-only diagnostic
    # tool into an arbitrary-command or destructive one. `find -exec rm ...`
    # would otherwise run `rm` even though it is in BLOCKED, because BLOCKED
    # only inspects argv[0]. These flags are rejected regardless of position.
    DANGEROUS_FLAGS = {
        "find": {
            "-exec", "-execdir", "-ok", "-okdir", "-delete",
            "-fprint", "-fprintf", "-fprint0", "-fls",
        },
    }

    SHELL_DANGERS = re.compile(r"[;&|`$<>]")

    # Guest power actions are routed to the Proxmox API instead of SSH:
    # POST /nodes/{node}/{lxc|qemu}/{vmid}/status/{start|stop} returns a task
    # UPID we poll. This works for guests on any cluster node (SSH always
    # lands on one host, but pct/qm must run on the node hosting the guest)
    # and needs only the VM.PowerMgmt privilege instead of a root shell.
    API_POWER_COMMANDS = {
        ("pct", "start"): "lxc",
        ("pct", "stop"): "lxc",
        ("qm", "start"): "qemu",
        ("qm", "stop"): "qemu",
    }

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

        dangerous_flags = self.DANGEROUS_FLAGS.get(head)
        if dangerous_flags:
            for token in parts[1:]:
                if token in dangerous_flags:
                    raise ValueError(
                        f"Flag '{token}' is not allowed for '{head}'"
                    )

        return parts

    @staticmethod
    def _get_ssh_client(settings) -> paramiko.SSHClient:
        """Create an authenticated SSH client to the Proxmox node."""
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if getattr(settings, "proxmox_ssh_strict_host_key", False):
            # Reject unknown hosts; operator must pre-populate known_hosts.
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            logger.warning(
                "SSH host-key verification is disabled (AutoAddPolicy). "
                "Set PROXMOX_SSH_STRICT_HOST_KEY=true and populate known_hosts "
                "to protect against man-in-the-middle attacks."
            )
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        host = settings.proxmox_host_ip or settings.proxmox_ip
        if not host:
            # Fall back to the host part of the configured API URL.
            try:
                host = urlparse(settings.proxmox_api_base_url).hostname
            except ValueError:
                host = None
        if not host:
            raise RuntimeError(
                "No SSH host configured. Set PROXMOX_HOST_IP (or PROXMOX_IP / PROXMOX_URL) in .env."
            )

        user = settings.proxmox_ssh_user
        if not user:
            # The API identity (PROXMOX_USER, e.g. "ai-stack") is usually not a
            # Unix account on the node; SSH needs its own login.
            user = settings.proxmox_user.split("@")[0]  # strip realm ("root@pam" -> "root")
            logger.warning(
                "PROXMOX_SSH_USER is not set; falling back to API user '%s' for SSH login. "
                "Set PROXMOX_SSH_USER (e.g. 'root') if this is not a Unix account on the node.",
                user,
            )

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
        """Execute a validated command: guest power actions go through the
        Proxmox API, everything else runs on the Proxmox node via SSH.

        Args:
            command: shell-like command string (validated)
            target: ignored (the node is resolved from settings / the cluster)
            timeout: seconds before timing out

        Returns:
            dict with returncode, stdout, stderr, node
        """
        parts = self.validate(command)
        settings = get_settings()

        subcommand = parts[1] if len(parts) > 1 else ""
        guest_type = self.API_POWER_COMMANDS.get((parts[0], subcommand))
        if guest_type is not None:
            return self._execute_power_via_api(parts, guest_type, settings, timeout)

        return self._execute_via_ssh(parts, settings, timeout)

    def _execute_power_via_api(self, parts: list[str], guest_type: str, settings, timeout: int) -> dict:
        """Run pct/qm start/stop as a Proxmox API power action and wait for the task."""
        tool, action, *rest = parts
        if len(rest) != 1 or not rest[0].isdigit():
            raise ValueError(f"'{tool} {action}' requires exactly one numeric vmid")
        vmid = int(rest[0])

        client = ProxmoxClient(settings)
        guest = client.find_guest(vmid)
        if guest is None:
            raise ValueError(f"No guest with vmid {vmid} found on the cluster")
        if guest.get("type") != guest_type:
            other_tool = "qm" if tool == "pct" else "pct"
            raise ValueError(
                f"vmid {vmid} is a {guest.get('type')} guest; use '{other_tool} {action} {vmid}'"
            )

        node = guest["node"]
        logger.info(f"Executing via Proxmox API on {node}: {tool} {action} {vmid}")
        if action == "start":
            upid = client.start_guest(node, vmid, guest_type)
        else:
            upid = client.stop_guest(node, vmid, guest_type)
        if not upid:
            raise RuntimeError(f"Proxmox did not return a task UPID for {action} of vmid {vmid}")

        task = client.wait_for_task(node, upid, timeout=max(timeout, 30))
        exitstatus = task.get("exitstatus", "")
        ok = exitstatus == "OK"
        return {
            "returncode": 0 if ok else 1,
            "stdout": f"Proxmox API task {upid}: {exitstatus or task.get('status', 'unknown')}",
            "stderr": "" if ok else f"Task finished with exitstatus: {exitstatus or 'unknown'}",
            "node": node,
        }

    def _execute_via_ssh(self, parts: list[str], settings, timeout: int) -> dict:
        if hasattr(shlex, "join"):
            normalized_command = shlex.join(parts)
        else:
            normalized_command = " ".join(shlex.quote(part) for part in parts)
        node = settings.proxmox_node or "proxmox"

        client = self._get_ssh_client(settings)
        try:
            logger.info(f"Executing on {node}: {normalized_command}")
            stdin, stdout, stderr = client.exec_command(
                normalized_command, timeout=timeout
            )
            channel = stdout.channel
            out_buf = bytearray()
            err_buf = bytearray()

            # Drain both streams while waiting for the command to exit. Calling
            # recv_exit_status() first would deadlock on large output: once the
            # SSH window fills, the remote blocks writing and never exits.
            deadline = time.monotonic() + timeout
            while not channel.exit_status_ready():
                drained = False
                while channel.recv_ready():
                    out_buf.extend(channel.recv(32768))
                    drained = True
                while channel.recv_stderr_ready():
                    err_buf.extend(channel.recv_stderr(32768))
                    drained = True
                if time.monotonic() > deadline:
                    channel.close()
                    raise RuntimeError(f"Command timed out after {timeout}s")
                if not drained:
                    time.sleep(0.05)

            exit_code = channel.recv_exit_status()
            # The exit status arrives with EOF, so these reads cannot block.
            out_buf.extend(stdout.read())
            err_buf.extend(stderr.read())

            return {
                "returncode": exit_code,
                "stdout": out_buf.decode("utf-8", errors="replace").rstrip(),
                "stderr": err_buf.decode("utf-8", errors="replace").rstrip(),
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