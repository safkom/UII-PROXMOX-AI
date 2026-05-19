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