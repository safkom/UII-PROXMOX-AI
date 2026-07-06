import os
from datetime import datetime, timezone

import pytest
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
from backend.execution.service import ExecutionService


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

    def claim_for_execution(self, approval_id):
        item = self._store.get(approval_id)
        if not item or item["status"] != "approved":
            return None
        item["updated_at"] = datetime.now(timezone.utc)
        item["status"] = "executing"
        return item

    def release_claim(self, approval_id, note=None):
        item = self._store.get(approval_id)
        if not item or item["status"] != "executing":
            return item
        item["updated_at"] = datetime.now(timezone.utc)
        item["status"] = "approved"
        if note:
            item["review_note"] = note
        return item

    def mark_executed(self, approval_id, note=None):
        item = self._store.get(approval_id)
        if not item or item["status"] != "executing":
            return item
        item["updated_at"] = datetime.now(timezone.utc)
        item["status"] = "executed"
        if note:
            item["review_note"] = note
        return item

    def delete(self, approval_id):
        return self._store.pop(approval_id, None) is not None

    def cleanup(self, remove_empty=True, action=None, statuses=None):
        return 0


def test_approval_to_execute_flow(monkeypatch):
    client = TestClient(app)

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

    # Executed approvals must not be runnable again
    assert fake_store.get(approval_id)["status"] == "executed"


def test_execute_requires_approval():
    client = TestClient(app)

    resp = client.post("/execute", json={"command": "tail -n 1 /etc/hosts"})
    assert resp.status_code == 400

    resp = client.post("/execute", json={})
    assert resp.status_code == 400


def test_execute_rejects_unapproved(monkeypatch):
    client = TestClient(app)

    fake_store = FakeApprovalStore()
    monkeypatch.setattr(routes_mod, "approval_store", fake_store)

    created = fake_store.create("tail_logs", "tail -n 1 /etc/hosts", None, "low", None, "tester")
    resp = client.post("/execute", json={"approval_id": created["id"]})
    assert resp.status_code == 400


def test_command_validation_allowlist():
    service = ExecutionService()

    # Diagnostic commands pass
    assert service.validate("df -h") == ["df", "-h"]
    assert service.validate("pct list") == ["pct", "list"]
    assert service.validate("systemctl status nginx")[0] == "systemctl"

    # Blocked binaries
    with pytest.raises(ValueError):
        service.validate("rm -rf /")
    with pytest.raises(ValueError):
        service.validate("shutdown now")

    # Destructive subcommands of allowed binaries are rejected
    with pytest.raises(ValueError):
        service.validate("pct destroy 101")
    with pytest.raises(ValueError):
        service.validate("qm destroy 101")
    with pytest.raises(ValueError):
        service.validate("systemctl poweroff")
    with pytest.raises(ValueError):
        service.validate("pvesh delete /nodes/pve/lxc/101")

    # Shell metacharacters and path tricks are rejected
    with pytest.raises(ValueError):
        service.validate("df -h; rm -rf /")
    with pytest.raises(ValueError):
        service.validate("cat /etc/passwd | grep root")
    with pytest.raises(ValueError):
        service.validate("/usr/bin/df -h")


def test_find_dangerous_flags_blocked():
    service = ExecutionService()

    # `find` is allowed for read-only searches...
    assert service.validate("find /var/log -name '*.log'")[0] == "find"
    assert service.validate("find /etc -type f") == ["find", "/etc", "-type", "f"]

    # ...but its command-execution / destructive flags must be rejected, even
    # though they contain no shell metacharacters and `rm` is only nested.
    for cmd in [
        "find / -delete",
        "find / -name x -exec rm -rf {} +",
        "find . -execdir touch pwned +",
        "find / -type f -fprintf /tmp/out %p",
        "find / -fls /tmp/list",
    ]:
        with pytest.raises(ValueError):
            service.validate(cmd)


def test_claim_prevents_double_execution(tmp_path):
    from backend.approvals.store import ApprovalStore

    store = ApprovalStore(db_path=str(tmp_path / "approvals.sqlite3"))
    item = store.create("tail_logs", "tail -n 1 /etc/hosts", None, "low", None, "tester")
    store.decide(item["id"], "approved", "admin", "ok")

    # First claim wins and flips approved -> executing.
    first = store.claim_for_execution(item["id"])
    assert first is not None
    assert first["status"] == "executing"

    # A concurrent second claim on the same approval must fail (TOCTOU closed).
    second = store.claim_for_execution(item["id"])
    assert second is None

    # Finalize and confirm it cannot be claimed again.
    store.mark_executed(item["id"], note="done")
    assert store.get(item["id"])["status"] == "executed"
    assert store.claim_for_execution(item["id"]) is None


def test_execute_conflict_on_double_run(monkeypatch):
    client = TestClient(app)

    fake_store = FakeApprovalStore()
    monkeypatch.setattr(routes_mod, "approval_store", fake_store)

    created = fake_store.create("tail_logs", "tail -n 1 /etc/hosts", None, "low", None, "tester")
    fake_store.decide(created["id"], "approved", "admin", "ok")

    monkeypatch.setattr(routes_mod.exec_service, "execute",
                        lambda cmd, target, timeout=30: {"returncode": 0, "stdout": "ok", "stderr": ""})

    # First execution succeeds and marks the approval executed.
    resp = client.post("/execute", json={"approval_id": created["id"]})
    assert resp.status_code == 200

    # Second execution of the same approval is refused with 409.
    resp = client.post("/execute", json={"approval_id": created["id"]})
    assert resp.status_code == 409


def test_execute_rejects_command_mismatch(monkeypatch):
    """A client-supplied command that differs from the approved one must not run."""
    client = TestClient(app)

    fake_store = FakeApprovalStore()
    monkeypatch.setattr(routes_mod, "approval_store", fake_store)

    created = fake_store.create("tail_logs", "tail -n 1 /etc/hosts", None, "low", None, "tester")
    fake_store.decide(created["id"], "approved", "admin", "ok")

    def fail_execute(cmd, target, timeout=30):
        raise AssertionError("mismatched command must not execute")

    monkeypatch.setattr(routes_mod.exec_service, "execute", fail_execute)

    resp = client.post("/execute", json={"approval_id": created["id"], "command": "cat /etc/shadow"})
    assert resp.status_code == 400

    # The claim is released so the approval stays runnable with its own command.
    assert fake_store.get(created["id"])["status"] == "approved"

    monkeypatch.setattr(
        routes_mod.exec_service, "execute",
        lambda cmd, target, timeout=30: {"returncode": 0, "stdout": "ok", "stderr": ""},
    )
    resp = client.post("/execute", json={"approval_id": created["id"], "command": "tail -n 1 /etc/hosts"})
    assert resp.status_code == 200


def test_api_auth_optional_token(monkeypatch):
    from backend.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "api_auth_token", "s3cret", raising=False)
    client = TestClient(app)

    # Missing token -> 401
    assert client.get("/settings").status_code == 401
    # Wrong token -> 401
    assert client.get("/settings", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Correct bearer token -> passes auth (200)
    assert client.get("/settings", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # X-API-Key header also accepted
    assert client.get("/settings", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_api_auth_disabled_by_default(monkeypatch):
    from backend.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "api_auth_token", None, raising=False)
    client = TestClient(app)

    # No token configured -> open (back-compat trusted-LAN mode)
    assert client.get("/settings").status_code == 200


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

    # Proxmox always serves the API over HTTPS; verify_ssl only controls validation.
    assert settings.proxmox_api_base_url == "https://10.0.0.50:8006"
    assert settings.proxmox_auth_header == "PVEAPIToken=ai-stack@pve!assistant=secret"


def test_chat_returns_model_answer(monkeypatch):
    client = TestClient(app)

    class FakeOllamaClient:
        def __init__(self, settings):
            self.settings = settings
            self.model = "fake-model"

        def chat(self, messages, tools=None):
            # The endpoint must send the system prompt, history, and query
            assert messages[0]["role"] == "system"
            assert messages[-1] == {"role": "user", "content": "What containers are running?"}
            return {
                "summary": "scan complete",
                "reasoning": "used refreshed inventory",
                "confidence": 0.9,
                "suggested_actions": [
                    {
                        "action": "Check container status",
                        "command": "pct status 101",
                        "target": "ct1",
                        "risk": "low",
                    }
                ],
            }

    monkeypatch.setattr(routes_mod, "OllamaClient", FakeOllamaClient)

    resp = client.post(
        "/chat",
        json={"query": "What containers are running?", "model": None, "history": []},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == "scan complete"
    assert data["confidence"] == 0.9
    assert data["suggested_actions"][0]["command"] == "pct status 101"


def test_chat_history_is_capped(monkeypatch):
    client = TestClient(app)
    seen = {}

    class FakeOllamaClient:
        def __init__(self, settings):
            self.model = "fake-model"

        def chat(self, messages, tools=None):
            seen["messages"] = messages
            return {"summary": "ok", "reasoning": "", "confidence": 1.0, "suggested_actions": []}

    monkeypatch.setattr(routes_mod, "OllamaClient", FakeOllamaClient)

    history = [{"role": "user", "content": f"msg {i}"} for i in range(30)]
    resp = client.post("/chat", json={"query": "latest", "history": history})
    assert resp.status_code == 200

    # system prompt + capped history + current query
    assert len(seen["messages"]) == 1 + routes_mod.MAX_HISTORY_MESSAGES + 1
    assert seen["messages"][1]["content"] == f"msg {30 - routes_mod.MAX_HISTORY_MESSAGES}"
