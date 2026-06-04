import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient


os.environ.setdefault("PROXMOX_URL", "http://proxmox.local")
os.environ.setdefault("PROXMOX_TOKEN_SECRET", "test-secret")
os.environ.setdefault("QDRANT_URL", "http://qdrant.local")
os.environ.setdefault("OLLAMA_URL", "http://ollama.local")
os.environ.setdefault("LOKI_URL", "http://loki.local")
os.environ.setdefault("PROMETHEUS_URL", "http://prometheus.local")

import backend.api.routes as routes_mod
from backend.api.main import app
from backend.config.settings import Settings


class FakeApprovalStore:
    def __init__(self):
        self._store = {}

    def create(self, action, command, target, risk, source_query, requested_by):
        now = datetime.now(timezone.utc)
        _id = "test-approval-1"
        item = {
            "id": _id,
            "status": "pending",
            "action": action,
            "command": command,
            "target": target,
            "risk": risk,
            "source_query": source_query,
            "requested_by": requested_by,
            "reviewer": None,
            "review_note": None,
            "created_at": now,
            "updated_at": now,
        }
        self._store[_id] = item
        return item

    def get(self, approval_id):
        return self._store.get(approval_id)

    def list(self, status=None):
        return list(self._store.values())

    def decide(self, approval_id, decision, reviewer, note):
        item = self._store.get(approval_id)
        if not item:
            return None
        item["updated_at"] = datetime.now(timezone.utc)
        item["status"] = decision
        item["reviewer"] = reviewer
        item["review_note"] = note
        return item


def test_approval_to_execute_flow(monkeypatch):
    client = TestClient(app)

    # Replace approval_store with fake
    fake_store = FakeApprovalStore()
    monkeypatch.setattr(routes_mod, "approval_store", fake_store)

    # Create approval
    payload = {
        "action": "tail_logs",
        "command": "tail -n 1 /etc/hosts",
        "target": None,
        "risk": "low",
        "source_query": "show hosts",
        "requested_by": "tester",
    }
    resp = client.post("/approvals", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    approval_id = data["id"]

    # Approve it
    resp = client.patch(f"/approvals/{approval_id}", json={"decision": "approved", "reviewer": "admin", "note": "ok"})
    assert resp.status_code == 200

    # Mock exec_service.execute
    def fake_execute(cmd, target, timeout=30):
        assert cmd == payload["command"]
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(routes_mod.exec_service, "execute", fake_execute)

    # Execute
    resp = client.post("/execute", json={"approval_id": approval_id})
    assert resp.status_code == 200
    result = resp.json()
    assert result["returncode"] == 0
    assert result["stdout"] == "ok"


def test_direct_execute_ignores_target_metadata(monkeypatch):
    client = TestClient(app)

    def fake_execute(cmd, target, timeout=30):
        assert cmd == "tail -n 1 /etc/hosts"
        assert target == "vm-100"
        assert timeout == 12
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(routes_mod.exec_service, "execute", fake_execute)

    resp = client.post(
        "/execute/direct",
        json={"command": "tail -n 1 /etc/hosts", "target": "vm-100", "timeout": 12},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["target"] == "vm-100"
    assert data["stdout"] == "ok"


def test_proxvnc_execution_uses_password_auth(monkeypatch):
    import backend.execution.service as exec_mod

    class FakeSettings:
        proxmox_url = ""
        proxmox_host_ip = "10.0.0.50"
        proxmox_ip = None
        proxmox_port = 8006
        proxmox_user = "ai-stack"
        proxmox_realm = "pve"
        proxmox_token_id = "assistant"
        proxmox_token_secret = "secret"
        proxmox_password = "password"
        proxmox_otp = None
        proxmox_pve_auth_cookie = None
        proxmox_verify_ssl = False

    class FakeNodes:
        def get(self):
            return [{"node": "pve"}]

    class FakeAPI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.nodes = FakeNodes()

    class FakeVNC:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.connected = False
            self.commands = []

        def connect(self):
            self.connected = True

        def execCommandAsB64(self, command, wait_time=0.5):
            self.commands.append((command, wait_time))

        def readTerm(self, waitTime=0.5):
            return "command output\n__PROXVNC_EXIT__:0\n"

        def disconnect(self):
            self.connected = False

    monkeypatch.setattr(exec_mod, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(exec_mod, "ProxmoxApiClient", FakeAPI)
    monkeypatch.setattr(exec_mod, "ProxVNCClient", FakeVNC)

    result = exec_mod.ExecutionService().execute("tail -n 1 /etc/hosts", timeout=4)

    assert result["returncode"] == 0
    assert "command output" in result["stdout"]


def test_proxvnc_execution_can_use_cookie_auth(monkeypatch):
    import backend.execution.service as exec_mod

    class FakeSettings:
        proxmox_url = ""
        proxmox_host_ip = "10.0.0.50"
        proxmox_ip = None
        proxmox_port = 8006
        proxmox_user = "ai-stack"
        proxmox_realm = "pve"
        proxmox_token_id = "assistant"
        proxmox_token_secret = "secret"
        proxmox_password = None
        proxmox_otp = None
        proxmox_pve_auth_cookie = "PVEAuthCookie=abc123"
        proxmox_verify_ssl = False

    class FakeTermproxy:
        def post(self):
            return {"ticket": "ticket-1", "port": 5900}

    class FakeNodeResource:
        def __init__(self, node):
            self.node = node
            self.termproxy = FakeTermproxy()

    class FakeNodes:
        def get(self):
            return [{"node": "pve"}]

        def __call__(self, node):
            return FakeNodeResource(node)

    class FakeAPI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.nodes = FakeNodes()

    class FakeVNC:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.commands = []

        def connect(self):
            return None

        def execCommandAsB64(self, command, wait_time=0.5):
            self.commands.append((command, wait_time))

        def readTerm(self, waitTime=0.5):
            return "ok\n__PROXVNC_EXIT__:0\n"

        def disconnect(self):
            return None

    class FakeResponse:
        def __init__(self):
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"ticket": "ticket-1", "port": 5900}}

    class FakeRequests:
        def post(self, url, headers=None, verify=True, timeout=10, **kwargs):
            return FakeResponse()

    fake_requests = FakeRequests()

    monkeypatch.setattr(exec_mod, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(exec_mod, "ProxmoxApiClient", FakeAPI)
    monkeypatch.setattr(exec_mod, "ProxVNCClient", FakeVNC)

    # The service imports `requests` inside _connect_client; patch it on the built-in module
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post", fake_requests.post)

    result = exec_mod.ExecutionService().execute("ls /", target="ct1", timeout=3)

    assert result["returncode"] == 0
    assert result["node"] == "pve"
    assert result["stdout"].startswith("ok")


def test_proxmox_ip_builds_api_base_url():
    settings = Settings.model_construct(
        proxmox_url="",
        proxmox_host_ip="10.0.0.50",
        proxmox_ip=None,
        proxmox_port=8006,
        proxmox_verify_ssl=False,
        proxmox_user="ai-stack",
        proxmox_realm="pve",
        proxmox_token_id="assistant",
        proxmox_token_secret="secret",
        qdrant_url="http://qdrant.local",
        ollama_url="http://ollama.local",
        loki_url="http://loki.local",
        prometheus_url="http://prometheus.local",
    )

    assert settings.proxmox_api_base_url == "http://10.0.0.50:8006"
    assert settings.proxmox_auth_header == "PVEAPIToken=ai-stack@pve!assistant=secret"


def test_chat_scans_containers_when_model_requests_it(monkeypatch):
    client = TestClient(app)

    class FakeSnapshotStore:
        def __init__(self, settings):
            self.settings = settings

        def list_current_infrastructure(self):
            return []

    class FakeProxmoxClient:
        def __init__(self, settings):
            self.settings = settings

        def scan_inventory(self):
            return {
                "containers": [
                    {
                        "vmid": 101,
                        "name": "ct1",
                        "type": "lxc",
                        "node": "pve",
                        "status": "running",
                        "ip": "10.0.0.10",
                    }
                ],
                "scanned_nodes": 1,
                "diagnostics": [],
            }

    class FakeOllamaClient:
        def __init__(self, settings):
            self.settings = settings
            self.prompts = []

        def list_models(self):
            return []

        def generate_json(self, prompt, system_prompt=""):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return {"tool_call": {"name": "scan_containers", "args": {}}}
            return {
                "summary": "scan complete",
                "reasoning": "used refreshed inventory",
                "confidence": 0.9,
                "suggested_actions": [
                    {
                        "action": "Check resource usage",
                        "command": "pct top 101",
                        "target": "ct1",
                        "risk": "low",
                    }
                ],
            }

    monkeypatch.setattr(routes_mod, "SnapshotStore", FakeSnapshotStore)
    monkeypatch.setattr(routes_mod, "ProxmoxClient", FakeProxmoxClient)
    monkeypatch.setattr(routes_mod, "OllamaClient", FakeOllamaClient)

    resp = client.post(
        "/chat",
        json={"query": "What containers are running?", "include_logs": False, "log_limit": 1, "model": None},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == "scan complete"
    assert data["context"]["infrastructure_count"] == 1
    assert data["suggested_actions"][0]["command"] == "pct top 101"