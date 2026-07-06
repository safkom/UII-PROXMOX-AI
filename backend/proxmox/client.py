"""Proxmox API client wrapper."""
import logging
import time
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
        # (connect, read) timeout so a hung/unreachable PVE host cannot block
        # the worker thread indefinitely. Callers may override via kwargs.
        self._timeout = (5, getattr(settings, "proxmox_request_timeout", 15))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated request to Proxmox API."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self._timeout)
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

    def get_cluster_resources(self, resource_type: Optional[str] = None) -> list[dict]:
        """List cluster resources in a single call (nodes, VMs, containers, ...).

        `GET /cluster/resources` is the canonical PVE endpoint for a full
        inventory: one request returns every guest with vmid, name, node,
        status and type ("qemu"/"lxc"), instead of one request pair per guest.
        """
        params = {"type": resource_type} if resource_type else None
        data = self._request("GET", "/api2/json/cluster/resources", params=params)
        return data.get("data", [])

    def find_guest(self, vmid: int) -> Optional[dict]:
        """Locate a guest cluster-wide by vmid. Returns its resource entry or None."""
        for resource in self.get_cluster_resources("vm"):
            if resource.get("vmid") == vmid and resource.get("type") in ("lxc", "qemu"):
                return resource
        return None

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
        """Scan Proxmox inventory with diagnostics for troubleshooting.

        Uses a single `GET /cluster/resources` call for the base inventory
        (node, vmid, name, status, type per guest), then fetches each guest's
        config only for hostname / statically configured IP. For running
        guests without a static IP, falls back to the runtime-IP endpoints.
        """
        containers = []
        diagnostics: list[str] = []

        try:
            resources = self.get_cluster_resources()
        except Exception as exc:
            logger.error(f"Failed to fetch cluster resources: {exc}")
            raise RuntimeError(
                "Failed to fetch Proxmox cluster resources. "
                "Check token ACL on / (Sys.Audit and VM.Audit are required)."
            ) from exc

        node_count = sum(1 for r in resources if r.get("type") == "node")

        for resource in resources:
            guest_type = resource.get("type")
            if guest_type not in ("lxc", "qemu"):
                continue
            if resource.get("template"):
                continue
            vmid = resource.get("vmid")
            node_name = resource.get("node")
            if vmid is None or not node_name:
                continue

            status = resource.get("status", "unknown")
            hostname = ""
            ip = None
            try:
                config = self.get_container_config(node_name, vmid, guest_type)
                # LXC config carries `hostname`; QEMU config only has `name`.
                hostname = config.get("hostname" if guest_type == "lxc" else "name", "")
                ip = self._extract_ip(config)
            except Exception as e:
                logger.warning(f"Failed to fetch config for {guest_type} {vmid}: {e}")
                diagnostics.append(f"{node_name}/{vmid}: config fetch failed ({e})")

            if ip is None and status == "running":
                ip = self.get_runtime_ip(node_name, vmid, guest_type)

            containers.append({
                "type": guest_type,
                "vmid": vmid,
                "name": resource.get("name", "unknown"),
                "node": node_name,
                "status": status,
                "hostname": hostname,
                "ip": ip,
            })

        if not containers:
            diagnostics.append(
                "No containers or VMs returned. This usually means token ACL scope is too narrow for guest inventory."
            )

        return {
            "containers": containers,
            "scanned_nodes": node_count,
            "diagnostics": diagnostics,
        }

    def get_runtime_ip(self, node: str, vmid: int, guest_type: str) -> Optional[str]:
        """Best-effort runtime IPv4 for a running guest (covers DHCP setups).

        LXC: `GET /nodes/{node}/lxc/{vmid}/interfaces`.
        QEMU: `GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`,
        which requires the QEMU guest agent inside the VM. Both endpoints may
        be unavailable (older PVE, no agent), so any failure returns None.
        """
        try:
            if guest_type == "lxc":
                data = self._request("GET", f"/api2/json/nodes/{node}/lxc/{vmid}/interfaces")
                for iface in data.get("data") or []:
                    if iface.get("name") == "lo":
                        continue
                    inet = iface.get("inet") or ""
                    ip = inet.split("/")[0]
                    if ip and not ip.startswith("127."):
                        return ip
            elif guest_type == "qemu":
                data = self._request(
                    "GET", f"/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
                )
                for iface in (data.get("data") or {}).get("result") or []:
                    if iface.get("name") == "lo":
                        continue
                    for addr in iface.get("ip-addresses") or []:
                        if addr.get("ip-address-type") != "ipv4":
                            continue
                        ip = addr.get("ip-address", "")
                        if ip and not ip.startswith("127."):
                            return ip
        except Exception as exc:
            logger.debug(f"Runtime IP lookup failed for {guest_type} {vmid} on {node}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Power management via the API (start/stop return an async task UPID)
    # ------------------------------------------------------------------

    def start_guest(self, node: str, vmid: int, guest_type: str) -> str:
        """Start a guest. Returns the UPID of the spawned PVE task.

        Requires the VM.PowerMgmt privilege on the API token.
        """
        data = self._request("POST", f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status/start")
        return data.get("data", "")

    def stop_guest(self, node: str, vmid: int, guest_type: str) -> str:
        """Stop a guest. Returns the UPID of the spawned PVE task."""
        data = self._request("POST", f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status/stop")
        return data.get("data", "")

    def get_task_status(self, node: str, upid: str) -> dict:
        data = self._request("GET", f"/api2/json/nodes/{node}/tasks/{upid}/status")
        return data.get("data", {})

    def wait_for_task(self, node: str, upid: str, timeout: float = 60, poll_interval: float = 1.0) -> dict:
        """Poll a PVE task until it finishes. Success means exitstatus == 'OK'."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_task_status(node, upid)
            if status.get("status") == "stopped":
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Proxmox task {upid} did not finish within {timeout}s")
            time.sleep(poll_interval)

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
