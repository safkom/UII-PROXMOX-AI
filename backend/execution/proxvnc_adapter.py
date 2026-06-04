from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from backend.config.settings import get_settings
from backend.proxmox.client import ProxmoxClient

logger_available = True
try:
    import logging
    logger = logging.getLogger(__name__)
except Exception:
    logger_available = False


class ProxVNCAdapter:
    """Adapter to execute commands on Proxmox containers via ProxVNC.

    This is optional and will raise informative errors if ProxVNC is not
    installed. It resolves a `target` (name or vmid) to a node+vmid using
    the existing ProxmoxClient and then uses ProxVNC to connect and run
    the command in the container's shell.
    """

    def __init__(self):
        self.settings = get_settings()

    def _ensure_deps(self):
        try:
            # ProxVNC exposes ProxVNC and proxmoxer.ProxmoxAPI
            from ProxVNC.proxmoxer import ProxmoxAPI  # type: ignore
            from ProxVNC import ProxVNC  # type: ignore
        except Exception as exc:  # pragma: no cover - environment specific
            raise ImportError(
                "ProxVNC is required for remote execution. Install with: "
                "pip install 'git+https://github.com/xgiralt64/ProxVNC.git@main#egg=ProxVNC'"
            ) from exc
        return ProxmoxAPI, ProxVNC

    def _resolve_target(self, target: str) -> tuple[int, str]:
        """Resolve a target string to (vmid, node).

        `target` may be a numeric vmid or a name. Uses ProxmoxClient scan to find match.
        """
        client = ProxmoxClient(self.settings)
        containers = client.list_all_containers()
        # if numeric vmid
        try:
            vmid = int(target)
            for c in containers:
                if int(c.get("vmid", -1)) == vmid:
                    return vmid, c.get("node")
        except Exception:
            pass

        # try name match (exact)
        for c in containers:
            if str(c.get("name", "")).lower() == str(target).lower():
                return int(c.get("vmid")), c.get("node")

        raise ValueError(f"Target '{target}' not found in Proxmox inventory")

    def execute(self, command: str, target: str, timeout: int = 30) -> dict:
        ProxmoxAPI, ProxVNC = self._ensure_deps()

        # Build host:port for ProxmoxAPI
        host_ip = self.settings.proxmox_host_ip or self.settings.proxmox_ip
        if host_ip and host_ip.startswith(("http://", "https://")):
            parsed = urlparse(host_ip)
            host_ip = parsed.netloc or parsed.path
        if not host_ip and self.settings.proxmox_url:
            parsed = urlparse(self.settings.proxmox_url)
            host_ip = parsed.netloc or parsed.path
        if host_ip and ":" not in host_ip.rsplit("]", 1)[-1]:
            host_ip = f"{host_ip}:{self.settings.proxmox_port}"
        host = f"{host_ip}:{self.settings.proxmox_port}"

        node_name = self.settings.proxmox_node
        if not node_name:
            raise ValueError("PROXMOX_NODE is required for ProxVNC execution")

        # Create proxmox API object using token auth if available
        try:
            prox = ProxmoxAPI(
                host=host,
                user=self.settings.proxmox_user,
                token_name=self.settings.proxmox_token_id,
                token_value=self.settings.proxmox_token_secret,
                verify_ssl=self.settings.proxmox_verify_ssl,
            )
        except TypeError:
            # fallback to older signature where token args may differ
            prox = ProxmoxAPI(host=host, user=self.settings.proxmox_user, verify_ssl=self.settings.proxmox_verify_ssl)

        vmid, node = self._resolve_target(target)
        node = node or node_name

        client = ProxVNC(api=prox, node=node)
        try:
            client.connect(lxc=vmid)
            # Use base64 exec to avoid quoting issues
            client.execCommandAsB64(command)
            out = client.readTerm()
            client.disconnect()
            return {"returncode": 0, "stdout": out, "stderr": ""}
        except Exception as exc:  # pragma: no cover - integration
            try:
                client.disconnect()
            except Exception:
                pass
            logger.error(f"ProxVNC execution failed: {exc}") if logger_available else None
            raise
