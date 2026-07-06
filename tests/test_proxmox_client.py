"""Tests for the Proxmox client inventory scan and API power execution."""
import os

import pytest

os.environ.setdefault("PROXMOX_URL", "http://proxmox.local")
os.environ.setdefault("PROXMOX_TOKEN_SECRET", "test-secret")
os.environ.setdefault("QDRANT_URL", "http://qdrant.local")
os.environ.setdefault("OLLAMA_URL", "http://ollama.local")
os.environ.setdefault("LOKI_URL", "http://loki.local")
os.environ.setdefault("PROMETHEUS_URL", "http://prometheus.local")

from backend.config.settings import get_settings
from backend.execution.service import ExecutionService
from backend.proxmox.client import ProxmoxClient


CLUSTER_RESOURCES = [
    {"type": "node", "node": "pve1"},
    {"type": "node", "node": "pve2"},
    {"type": "lxc", "vmid": 101, "name": "web", "node": "pve1", "status": "running"},
    {"type": "qemu", "vmid": 200, "name": "win", "node": "pve2", "status": "stopped"},
    # Templates must be skipped.
    {"type": "lxc", "vmid": 900, "name": "tmpl", "node": "pve1", "status": "stopped", "template": 1},
    # Storage entries must be ignored.
    {"type": "storage", "storage": "local", "node": "pve1"},
]


def make_client() -> ProxmoxClient:
    return ProxmoxClient(get_settings())


def test_scan_inventory_uses_cluster_resources(monkeypatch):
    client = make_client()
    requests_made = []

    def fake_request(method, path, **kwargs):
        requests_made.append((method, path))
        if path == "/api2/json/cluster/resources":
            return {"data": CLUSTER_RESOURCES}
        if path.endswith("/lxc/101/config"):
            return {"data": {"hostname": "web-host", "net0": "name=eth0,ip=10.0.0.5/24"}}
        if path.endswith("/qemu/200/config"):
            return {"data": {"name": "win-vm", "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"}}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.scan_inventory()

    assert result["scanned_nodes"] == 2
    guests = {c["vmid"]: c for c in result["containers"]}
    # Template (900) and storage entries are excluded.
    assert set(guests) == {101, 200}
    assert guests[101]["ip"] == "10.0.0.5"
    assert guests[101]["hostname"] == "web-host"
    assert guests[101]["status"] == "running"
    # QEMU hostname comes from the `name` config key (there is no `hostname`).
    assert guests[200]["hostname"] == "win-vm"
    # Exactly one inventory call + one config call per guest, no status calls.
    inventory_calls = [p for _, p in requests_made if p == "/api2/json/cluster/resources"]
    assert len(inventory_calls) == 1
    assert not any("status/current" in p for _, p in requests_made)


def test_scan_inventory_runtime_ip_fallback(monkeypatch):
    client = make_client()

    def fake_request(method, path, **kwargs):
        if path == "/api2/json/cluster/resources":
            return {"data": [
                {"type": "node", "node": "pve1"},
                {"type": "lxc", "vmid": 101, "name": "dhcp-ct", "node": "pve1", "status": "running"},
            ]}
        if path.endswith("/lxc/101/config"):
            return {"data": {"hostname": "dhcp-ct", "net0": "name=eth0,ip=dhcp"}}
        if path.endswith("/lxc/101/interfaces"):
            return {"data": [
                {"name": "lo", "inet": "127.0.0.1/8"},
                {"name": "eth0", "inet": "192.168.1.66/24"},
            ]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.scan_inventory()
    assert result["containers"][0]["ip"] == "192.168.1.66"


def test_runtime_ip_is_best_effort(monkeypatch):
    """Older PVE / missing guest agent must not break the scan."""
    client = make_client()

    def fake_request(method, path, **kwargs):
        raise RuntimeError("501 not implemented")

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.get_runtime_ip("pve1", 101, "lxc") is None
    assert client.get_runtime_ip("pve1", 200, "qemu") is None


def test_qemu_runtime_ip_via_guest_agent(monkeypatch):
    client = make_client()

    def fake_request(method, path, **kwargs):
        assert path.endswith("/qemu/200/agent/network-get-interfaces")
        return {"data": {"result": [
            {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
            {"name": "ens18", "ip-addresses": [
                {"ip-address-type": "ipv6", "ip-address": "fe80::1"},
                {"ip-address-type": "ipv4", "ip-address": "192.168.1.77"},
            ]},
        ]}}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.get_runtime_ip("pve1", 200, "qemu") == "192.168.1.77"


class FakePowerClient:
    """Stub of ProxmoxClient for power-routing tests."""

    def __init__(self, settings):
        self.calls = []

    def find_guest(self, vmid):
        return {"type": "lxc", "vmid": 101, "node": "pve1"} if vmid == 101 else None

    def start_guest(self, node, vmid, guest_type):
        self.calls.append(("start", node, vmid, guest_type))
        return "UPID:pve1:0001:test:"

    def stop_guest(self, node, vmid, guest_type):
        self.calls.append(("stop", node, vmid, guest_type))
        return "UPID:pve1:0002:test:"

    def wait_for_task(self, node, upid, timeout=60, poll_interval=1.0):
        return {"status": "stopped", "exitstatus": "OK"}


def test_pct_start_routed_to_api(monkeypatch):
    import backend.execution.service as service_mod

    monkeypatch.setattr(service_mod, "ProxmoxClient", FakePowerClient)

    def fail_ssh(*args, **kwargs):
        raise AssertionError("power commands must not use SSH")

    service = ExecutionService()
    monkeypatch.setattr(service, "_execute_via_ssh", fail_ssh)

    result = service.execute("pct start 101")
    assert result["returncode"] == 0
    assert result["node"] == "pve1"
    assert "UPID:pve1:0001:test:" in result["stdout"]


def test_power_command_rejects_wrong_tool_or_unknown_vmid(monkeypatch):
    import backend.execution.service as service_mod

    monkeypatch.setattr(service_mod, "ProxmoxClient", FakePowerClient)
    service = ExecutionService()

    # vmid 101 is an LXC guest, so `qm start` must be rejected.
    with pytest.raises(ValueError):
        service.execute("qm start 101")
    # Unknown vmid.
    with pytest.raises(ValueError):
        service.execute("pct start 999")
    # Missing / non-numeric vmid.
    with pytest.raises(ValueError):
        service.execute("pct start")
    with pytest.raises(ValueError):
        service.execute("pct start web")


def test_non_power_commands_still_use_ssh(monkeypatch):
    service = ExecutionService()
    seen = {}

    def fake_ssh(parts, settings, timeout):
        seen["parts"] = parts
        return {"returncode": 0, "stdout": "ok", "stderr": "", "node": "pve1"}

    monkeypatch.setattr(service, "_execute_via_ssh", fake_ssh)

    result = service.execute("pct config 101")
    assert result["stdout"] == "ok"
    assert seen["parts"] == ["pct", "config", "101"]
