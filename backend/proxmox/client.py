"""Proxmox API client wrapper."""
import logging
from typing import Optional

import requests

from backend.config.settings import Settings

logger = logging.getLogger(__name__)


class ProxmoxClient:
    """Client for Proxmox API interaction."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.proxmox_api_base_url.rstrip("/")
        self.session = requests.Session()
        self.session.verify = settings.proxmox_verify_ssl
        self.session.headers.update({
            "Authorization": settings.proxmox_auth_header,
        })

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated request to Proxmox API."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error(f"Proxmox API error: {exc}")
            raise

    def get_nodes(self) -> list[dict]:
        """List all Proxmox nodes."""
        data = self._request("GET", "/api2/json/nodes")
        return data.get("data", [])

    def get_lxc_containers(self, node: str) -> list[dict]:
        """List LXC containers on a node."""
        data = self._request("GET", f"/api2/json/nodes/{node}/lxc")
        return data.get("data", [])

    def get_qemu_vms(self, node: str) -> list[dict]:
        """List QEMU VMs on a node."""
        data = self._request("GET", f"/api2/json/nodes/{node}/qemu")
        return data.get("data", [])

    def get_container_config(self, node: str, vmid: int, container_type: str = "lxc") -> dict:
        """Get config for a specific container or VM."""
        data = self._request("GET", f"/api2/json/nodes/{node}/{container_type}/{vmid}/config")
        return data.get("data", {})

    def get_container_status(self, node: str, vmid: int, container_type: str = "lxc") -> dict:
        """Get status for a specific container or VM."""
        data = self._request("GET", f"/api2/json/nodes/{node}/{container_type}/{vmid}/status/current")
        return data.get("data", {})

    def list_all_containers(self) -> list[dict]:
        """List all containers and VMs across all nodes with metadata."""
        result = self.scan_inventory()
        return result["containers"]

    def scan_inventory(self) -> dict:
        """Scan Proxmox inventory with diagnostics for troubleshooting."""
        containers = []
        diagnostics: list[str] = []

        try:
            nodes = self.get_nodes()
        except Exception as exc:
            logger.error(f"Failed to fetch nodes: {exc}")
            raise RuntimeError("Failed to fetch Proxmox nodes") from exc

        if not nodes:
            raise RuntimeError("No Proxmox nodes returned (check token permissions)")

        failed_inventory_queries = 0

        for node in nodes:
            node_name = node.get("node")
            if not node_name:
                continue

            node_query_failed = 0

            # Fetch LXC containers
            try:
                lxc_list = self.get_lxc_containers(node_name)
                for lxc in lxc_list:
                    try:
                        config = self.get_container_config(node_name, lxc["vmid"], "lxc")
                        status = self.get_container_status(node_name, lxc["vmid"], "lxc")
                        containers.append({
                            "type": "lxc",
                            "vmid": lxc["vmid"],
                            "name": lxc.get("name", "unknown"),
                            "node": node_name,
                            "status": status.get("status", "unknown"),
                            "hostname": config.get("hostname", ""),
                            "ip": self._extract_ip(config),
                        })
                    except Exception as e:
                        logger.warning(f"Failed to fetch config for LXC {lxc['vmid']}: {e}")
            except Exception as exc:
                logger.warning(f"Failed to fetch LXC containers on {node_name}: {exc}")
                diagnostics.append(f"{node_name}: LXC list failed ({exc})")
                node_query_failed += 1

            # Fetch QEMU VMs
            try:
                qemu_list = self.get_qemu_vms(node_name)
                for qemu in qemu_list:
                    try:
                        config = self.get_container_config(node_name, qemu["vmid"], "qemu")
                        status = self.get_container_status(node_name, qemu["vmid"], "qemu")
                        containers.append({
                            "type": "qemu",
                            "vmid": qemu["vmid"],
                            "name": qemu.get("name", "unknown"),
                            "node": node_name,
                            "status": status.get("status", "unknown"),
                            "hostname": config.get("hostname", ""),
                            "ip": self._extract_ip(config),
                        })
                    except Exception as e:
                        logger.warning(f"Failed to fetch config for VM {qemu['vmid']}: {e}")
            except Exception as exc:
                logger.warning(f"Failed to fetch QEMU VMs on {node_name}: {exc}")
                diagnostics.append(f"{node_name}: QEMU list failed ({exc})")
                node_query_failed += 1

            if node_query_failed == 2:
                failed_inventory_queries += 1

        if failed_inventory_queries == len(nodes):
            raise RuntimeError(
                "Failed to query inventory on all nodes. "
                "Check token ACL on / and nodes (Sys.Audit and VM.Audit are required)."
            )

        if not containers:
            diagnostics.append(
                "No containers or VMs returned. This usually means token ACL scope is too narrow for node inventory."
            )

        return {
            "containers": containers,
            "scanned_nodes": len(nodes),
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _extract_ip(config: dict) -> Optional[str]:
        """Extract the first IP address from Proxmox container/VM config."""
        # LXC commonly exposes net0/net1 fields with "ip=..." segments.
        for key, value in config.items():
            if key.startswith("net") and isinstance(value, str):
                parts = value.split(",")
                for part in parts:
                    if part.startswith("ip="):
                        ip_with_mask = part.replace("ip=", "")
                        if ip_with_mask and ip_with_mask.lower() != "dhcp":
                            return ip_with_mask.split("/")[0]

        # Cloud-init VMs commonly use ipconfig0/ipconfig1 fields.
        for key, value in config.items():
            if key.startswith("ipconfig") and isinstance(value, str):
                parts = value.split(",")
                for part in parts:
                    if part.startswith("ip="):
                        ip_with_mask = part.replace("ip=", "")
                        if ip_with_mask and ip_with_mask.lower() != "dhcp":
                            return ip_with_mask.split("/")[0]

        return None
