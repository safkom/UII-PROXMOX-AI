"""ProxVNC-backed execution service for diagnostic commands.

This module keeps the conservative command validation layer, but runs approved
commands through a ProxVNC shell on the selected Proxmox node.
"""
from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Optional
from urllib.parse import urlparse

from backend.config.settings import get_settings
try:
    from ProxVNC import ProxVNC as ProxVNCClient
    from ProxVNC.proxmoxer import ProxmoxAPI as ProxmoxApiClient
except Exception:  # pragma: no cover - optional dependency in tests
    ProxVNCClient = None
    ProxmoxApiClient = None


logger = logging.getLogger(__name__)

# PVE tickets are valid for 2 hours; refresh after 1 hour to be safe
_TICKET_REFRESH_INTERVAL = 3600


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

    def _normalize_command(self, command: str) -> str:
        parts = self.validate(command)
        if hasattr(shlex, "join"):
            return shlex.join(parts)
        return " ".join(shlex.quote(part) for part in parts)

    @staticmethod
    def _proxmox_host(settings) -> str:
        host_ip = settings.proxmox_host_ip or settings.proxmox_ip
        if host_ip:
            if host_ip.startswith(("http://", "https://")):
                parsed = urlparse(host_ip)
                host_ip = parsed.netloc or parsed.path
            host_ip = host_ip.rstrip("/")
            return f"{host_ip}:{settings.proxmox_port}"

        if settings.proxmox_url:
            parsed = urlparse(settings.proxmox_url)
            if parsed.netloc:
                netloc = parsed.netloc
                if ":" not in netloc.rsplit("]", 1)[-1]:
                    netloc = f"{netloc}:{settings.proxmox_port}"
                return netloc
            return settings.proxmox_url.rstrip("/")

        raise ValueError("Either proxmox_host_ip, proxmox_ip, or proxmox_url must be configured")

    @staticmethod
    def _require_proxvnc() -> None:
        if ProxVNCClient is None or ProxmoxApiClient is None:
            raise RuntimeError("ProxVNC is not installed. Add the ProxVNC package to enable remote execution.")

    @staticmethod
    def _clean_proxvnc_output(raw: str) -> str:
        """Strip Proxmox VNC shell session noise from command output.

        The ProxVNC terminal captures the entire shell session including:
        - Initial banner/motd lines
        - The base64-encoded command echo
        - Shell prompts like 'root@proxmox:~#'
        - Terminal escape sequences
        """
        text = raw

        # Remove ANSI escape sequences
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
        text = re.sub(r"\x1b\].*?\x07", "", text)

        # Remove carriage returns
        text = text.replace("\r", "")

        # Remove the base64 command echo line (echo '...' | base64 -d | bash)
        text = re.sub(r"echo '[^']+' \| base64 -d \| bash\n?", "", text)

        # Remove shell prompt lines (e.g. "root@proxmox:~#")
        text = re.sub(r"root@\S+:~\#.*\n?", "", text)

        # Remove common Proxmox banner/motd lines
        lines = text.split("\n")
        cleaned_lines = []
        skip_patterns = [
            r"^Linux proxmox",
            r"^The programs included",
            r"^Debian GNU/Linux comes",
            r"^the exact distribution",
            r"^individual files",
            r"^permitted by applicable",
            r"^\s*$",
        ]
        for line in lines:
            if not any(re.match(p, line) for p in skip_patterns):
                cleaned_lines.append(line)

        # Remove leading blank lines
        result = "\n".join(cleaned_lines).lstrip("\n")
        return result

    def _refresh_ticket(self, settings) -> None:
        """Fetch a fresh PVE auth ticket using stored password credentials.

        Updates settings in-place so subsequent calls use the new ticket.
        Proxmox tickets are valid for 2 hours; this should be called before
        each execution to ensure the cookie is fresh.
        """
        import requests as req

        if not settings.proxmox_password:
            logger.debug("No password configured; skipping ticket refresh")
            return

        host = self._proxmox_host(settings)
        base = f"https://{host}"
        url = f"{base}/api2/json/access/ticket"

        try:
            r = req.post(
                url,
                data={"username": settings.proxmox_user, "password": settings.proxmox_password},
                verify=settings.proxmox_verify_ssl,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("data")
            if data and data.get("ticket"):
                raw_ticket = data["ticket"]
                csrf = data.get("CSRFPreventionToken", "")
                settings.proxmox_pve_auth_cookie = f"PVEAuthCookie={raw_ticket}"
                settings.proxmox_pve_csrf_token = csrf
                logger.debug("Refreshed PVE auth ticket for %s", settings.proxmox_user)
            else:
                logger.warning("Ticket refresh returned no ticket data")
        except Exception as exc:
            logger.warning("Failed to refresh PVE auth ticket: %s", exc)

    def _build_api(self, settings):
        host = self._proxmox_host(settings)
        common_kwargs = {
            "host": host,
            "verify_ssl": settings.proxmox_verify_ssl,
        }

        if settings.proxmox_password:
            return ProxmoxApiClient(
                **common_kwargs,
                user=settings.proxmox_user,
                password=settings.proxmox_password,
                otp=getattr(settings, "proxmox_otp", None),
            )

        return ProxmoxApiClient(
            **common_kwargs,
            user=settings.proxmox_user,
            token_name=settings.proxmox_token_id,
            token_value=settings.proxmox_token_secret,
        )

    def _resolve_node(self, settings, api, target: Optional[str]) -> str:
        configured_node = getattr(settings, "proxmox_node", None)
        if configured_node:
            return str(configured_node)

        try:
            nodes = api.nodes.get()
        except Exception:
            nodes = []

        node_names = [str(node.get("node")) for node in nodes if node.get("node")]
        if target:
            target_value = str(target).strip().lower()
            for node_name in node_names:
                if node_name.lower() == target_value:
                    return node_name

        if node_names:
            return node_names[0]

        raise RuntimeError("No Proxmox nodes are available")

    def _connect_client(self, settings, api, node: str):
        if settings.proxmox_pve_auth_cookie:
            # When using a raw PVE auth cookie, request the termproxy using that cookie
            # so the returned shell_ticket is tied to the same session/cookie.
            import requests

            base = getattr(settings, "proxmox_api_base_url", None) or f"https://{self._proxmox_host(settings)}"
            if base.startswith("http://"):
                base = "https://" + base[len("http://"):]

            termproxy_url = f"{base}/api2/json/nodes/{node}/termproxy"
            headers = {"Cookie": settings.proxmox_pve_auth_cookie}
            if getattr(settings, "proxmox_pve_csrf_token", None):
                headers["CSRFPreventionToken"] = settings.proxmox_pve_csrf_token
            r = requests.post(termproxy_url, headers=headers, verify=settings.proxmox_verify_ssl, timeout=10)
            r.raise_for_status()
            termproxy = r.json().get("data")
            shell_ticket = termproxy["ticket"]
            shell_port = termproxy["port"]

            # Strip the "PVEAuthCookie=" prefix if present — ProxVNC adds it own prefix
            raw_cookie = settings.proxmox_pve_auth_cookie
            if raw_cookie and raw_cookie.startswith("PVEAuthCookie="):
                raw_cookie = raw_cookie[len("PVEAuthCookie="):]

            client = ProxVNCClient(
                url=base,
                node=node,
                user=settings.proxmox_user,
                shell_port=shell_port,
                shell_ticket=shell_ticket,
                pve_auth_cookie=raw_cookie,
            )
            client.connect()
            return client

        client = ProxVNCClient(api=api, node=node)
        client.connect()
        return client

    def execute(self, command: str, target: Optional[str] = None, timeout: int = 30) -> dict:
        """Execute a validated command through ProxVNC.

        Args:
            command: shell-like command string (validated)
            target: remote target metadata used to resolve the Proxmox node
            timeout: seconds before timing out

        Returns:
            dict with returncode, stdout, stderr
        """
        self._require_proxvnc()
        normalized_command = self._normalize_command(command)
        settings = get_settings()

        # Refresh PVE auth ticket if using password-based cookie auth
        if settings.proxmox_password and settings.proxmox_pve_auth_cookie:
            self._refresh_ticket(settings)

        api = self._build_api(settings)
        node = self._resolve_node(settings, api, target)
        client = None

        exit_marker = "__PROXVNC_EXIT__"
        wrapped_command = (
            f"set -o pipefail; {normalized_command}; "
            f"rc=$?; printf '\n{exit_marker}:%s\n' \"$rc\""
        )

        try:
            client = self._connect_client(settings, api, node)
            client.execCommandAsB64(wrapped_command)
            output = client.readTerm(waitTime=max(0.5, min(float(timeout), 2.0)))

            match = re.search(rf"{re.escape(exit_marker)}:(-?\d+)", output)
            returncode = int(match.group(1)) if match else -1
            cleaned_output = re.sub(rf"\n?{re.escape(exit_marker)}:-?\d+\n?", "\n", output)

            # Strip Proxmox shell session noise for cleaner output
            cleaned_output = self._clean_proxvnc_output(cleaned_output)

            return {
                "returncode": returncode,
                "stdout": cleaned_output.rstrip(),
                "stderr": "",
                "node": node,
            }
        except Exception as exc:
            logger.error(f"ProxVNC execution failed: {exc}")
            raise
        finally:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    logger.debug("Failed to disconnect ProxVNC client", exc_info=True)