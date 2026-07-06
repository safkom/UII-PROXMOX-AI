import logging
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.approvals.store import ApprovalStore
from backend.config.settings import get_settings
from backend.ollama.client import OllamaClient, get_tool_definitions
from backend.proxmox.client import ProxmoxClient
from backend.qdrant.snapshots import SnapshotStore
from backend.loki.client import LokiClient
from backend.qdrant.logs import LogStore
from backend.execution.service import ExecutionService

from .health import probe_http_service
from .models import (
    Container,
    HealthReport,
    HealthResponse,
    InfrastructureHistoryItem,
    InfrastructureSummary,
    ScanResult,
    ServiceHealth,
    LogIngestionRequest,
    LogIngestionResult,
    LogSearchRequest,
    LogSearchResult,
    LogEntry,
    ChatRequest,
    ChatResponse,
    SuggestedAction,
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalItem,
    ExecuteRequest,
    ExecutionResult,
    SettingsResponse,
    SettingsUpdateRequest,
    SettingsSavedResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()
approval_store = ApprovalStore()
exec_service = ExecutionService()

# Cap how much conversation history is replayed to the model. The history is
# re-sent on every round of the tool loop, so this directly bounds prompt size
# on small local models.
MAX_HISTORY_MESSAGES = 10


def build_chat_messages(system_prompt: str, payload: ChatRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for msg in payload.history[-MAX_HISTORY_MESSAGES:]:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": payload.query})
    return messages


def collect_service_health() -> list[ServiceHealth]:
    settings = get_settings()
    probes = [
        probe_http_service(
            "proxmox",
            settings.proxmox_api_base_url,
            "/api2/json/version",
            verify_ssl=settings.proxmox_verify_ssl,
            headers={"Authorization": settings.proxmox_auth_header},
        ),
        probe_http_service("qdrant", settings.qdrant_url, "/healthz"),
        probe_http_service("ollama", settings.ollama_url, "/api/version"),
        probe_http_service("loki", settings.loki_url, "/ready"),
        probe_http_service("prometheus", settings.prometheus_url, "/-/ready"),
    ]
    return [ServiceHealth(**probe.__dict__) for probe in probes]


@router.get("/health/live", response_model=HealthResponse)
def health_live():
    return HealthResponse(status="ok")


@router.get("/health", response_model=HealthReport)
def health():
    services = collect_service_health()
    status = "ok" if all(service.ok for service in services) else "degraded"
    if status != "ok":
        raise HTTPException(status_code=503, detail="one or more services are unavailable")
    return HealthReport(status=status, services=services)


@router.get("/health/services", response_model=HealthReport)
def health_services():
    services = collect_service_health()
    status = "ok" if all(service.ok for service in services) else "degraded"
    return HealthReport(status=status, services=services)


@router.get("/containers", response_model=List[Container])
def list_containers():
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        containers = client.list_all_containers()
        return [Container(**c) for c in containers]
    except Exception as exc:
        logger.error(f"Failed to fetch containers: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch containers from Proxmox: {exc}")


@router.get("/infrastructure/current", response_model=List[Container])
def get_current_infrastructure():
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        current_points = snapshot_store.list_current_infrastructure()
        return [Container(**point) for point in current_points]
    except Exception as exc:
        logger.error(f"Failed to fetch current infrastructure: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch current infrastructure")


@router.get("/infrastructure/history", response_model=List[InfrastructureHistoryItem])
def get_infrastructure_history(limit: int = 20):
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        history_points = snapshot_store.list_history_scans(limit=limit)
        return [
            InfrastructureHistoryItem(
                scan_id=point.get("scan_id", ""),
                timestamp=point.get("timestamp", datetime.now(timezone.utc)),
                container_count=point.get("container_count", 0),
                scanned_nodes=point.get("scanned_nodes", 0),
                diagnostics=point.get("diagnostics", []),
                containers=[Container(**container) for container in point.get("containers", [])],
            )
            for point in history_points
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure history: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure history")


@router.get("/infrastructure/history/{scan_id}", response_model=InfrastructureHistoryItem)
def get_infrastructure_history_item(scan_id: str):
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        point = snapshot_store.get_history_scan(scan_id)
        if not point:
            raise HTTPException(status_code=404, detail="Scan not found")
        return InfrastructureHistoryItem(
            scan_id=point.get("scan_id", scan_id),
            timestamp=point.get("timestamp", datetime.now(timezone.utc)),
            container_count=point.get("container_count", 0),
            scanned_nodes=point.get("scanned_nodes", 0),
            diagnostics=point.get("diagnostics", []),
            containers=[Container(**container) for container in point.get("containers", [])],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure history item: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure history item")


@router.get("/infrastructure", response_model=InfrastructureSummary)
def get_infrastructure_summary():
    settings = get_settings()
    try:
        snapshot_store = SnapshotStore(settings)
        current_points = snapshot_store.list_current_infrastructure()
        history_points = snapshot_store.list_history_scans(limit=20)

        current_containers = [Container(**point) for point in current_points]
        history_items = [
            InfrastructureHistoryItem(
                scan_id=point.get("scan_id", ""),
                timestamp=point.get("timestamp", datetime.now(timezone.utc)),
                container_count=point.get("container_count", 0),
                scanned_nodes=point.get("scanned_nodes", 0),
                diagnostics=point.get("diagnostics", []),
                containers=[Container(**container) for container in point.get("containers", [])],
            )
            for point in history_points
        ]

        return InfrastructureSummary(
            current=current_containers,
            latest_scan=history_items[0] if history_items else None,
            history=history_items,
        )
    except Exception as exc:
        logger.error(f"Failed to fetch infrastructure summary: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch infrastructure summary")


@router.get("/debug/proxmox")
def debug_proxmox():
    """Debug endpoint to test Proxmox connectivity."""
    import traceback
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        base_url = client.base_url
        verify = client.session.verify
        # Show only the token name, never any part of the secret.
        auth = client.session.headers.get("Authorization", "").split("=")[0] + "=***"
        try:
            nodes = client.get_nodes()
            return {"ok": True, "base_url": base_url, "verify_ssl": verify, "auth_header": auth, "nodes": nodes}
        except Exception as e:
            return {"ok": False, "base_url": base_url, "verify_ssl": verify, "auth_header": auth, "error": str(e), "traceback": traceback.format_exc()}
    except Exception as e:
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


@router.post("/scan", response_model=ScanResult)
def scan_infrastructure():
    settings = get_settings()
    try:
        client = ProxmoxClient(settings)
        scan_data = client.scan_inventory()
        container_models = [Container(**c) for c in scan_data["containers"]]
        snapshot_store = SnapshotStore(settings)
        snapshot_id = snapshot_store.store_scan_snapshot(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "container_count": len(container_models),
                "containers": container_models,
                "scanned_nodes": scan_data["scanned_nodes"],
                "diagnostics": scan_data["diagnostics"],
            }
        )
        return ScanResult(
            timestamp=datetime.now(timezone.utc),
            container_count=len(container_models),
            containers=container_models,
            success=True,
            scanned_nodes=scan_data["scanned_nodes"],
            diagnostics=scan_data["diagnostics"],
            history_snapshot_id=snapshot_id,
        )
    except Exception as exc:
        logger.error(f"Failed to scan infrastructure: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to scan infrastructure: {exc}")


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    settings = get_settings()

    system_prompt = (
        "You are a Proxmox homelab DevOps assistant with tools to scan containers, fetch logs, and search logs. "
        "ALWAYS call the relevant tool before answering questions about containers, VMs, logs, or infrastructure. "
        "Once you have the tool results, respond with ONLY this JSON object: "
        '{"summary": "your answer to the user", "reasoning": "brief reasoning", "confidence": 0.0-1.0, '
        '"suggested_actions": [{"action": "description", "command": "shell command or null", "target": "container name or null", "risk": "low|medium|high"}]}. '
        "The command field must be a real shell command runnable on the Proxmox host "
        "(e.g. pct list, pct config <vmid>, qm list, journalctl -u <service>, systemctl status <service>, df -h, free -m). "
        "Use the numeric vmid with pct/qm, never the name. Never put tool names in the command field. "
        "If no real command applies, set command to null."
    )

    messages = build_chat_messages(system_prompt, payload)

    try:
        ollama_client = OllamaClient(settings)
        if payload.model:
            ollama_client.model = payload.model

        # chat() handles the full tool-calling loop internally:
        # call LLM -> check tool_calls -> execute tools -> append results -> repeat
        model_result = ollama_client.chat(messages, get_tool_definitions())
    except Exception as exc:
        logger.error(f"Failed to query Ollama: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate chat response")

    # Parse suggested_actions from the model result
    raw_actions = model_result.get("suggested_actions", [])
    normalized_actions: list[SuggestedAction] = []
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            try:
                normalized_actions.append(
                    SuggestedAction(
                        action=str(item.get("action", "Investigate issue")),
                        command=item.get("command"),
                        target=item.get("target"),
                        risk=str(item.get("risk", "medium")),
                    )
                )
            except Exception:
                continue

    try:
        confidence = float(model_result.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return ChatResponse(
        timestamp=datetime.now(timezone.utc),
        query=payload.query,
        summary=str(model_result.get("summary", "No summary available.")),
        reasoning=str(model_result.get("reasoning", "No reasoning provided.")),
        confidence=confidence,
        suggested_actions=normalized_actions,
        context={
            "model": settings.ollama_model,
        },
    )


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest):
    settings = get_settings()

    system_prompt = (
        "You are a Proxmox homelab DevOps assistant with tools to scan containers, fetch logs, and search logs. "
        "ALWAYS call the relevant tool before answering questions about containers, VMs, logs, or infrastructure. "
        "Once you have the tool results, answer the user in PLAIN TEXT. "
        "Then, on a new line, output this delimiter exactly: ###METADATA###\n"
        "Then output ONLY this JSON object: "
        '{"reasoning": "brief reasoning", "confidence": 0.0-1.0, '
        '"suggested_actions": [{"action": "description", "command": "shell command or null", "target": "container name or null", "risk": "low|medium|high"}]}. '
        "The command field must be a real shell command runnable on the Proxmox host "
        "(e.g. pct list, pct config <vmid>, pct start <vmid>, qm list, journalctl -u <service>, systemctl status <service>, df -h, free -m). "
        "Use the numeric vmid with pct/qm, never the name. Never put tool names in the command field. "
        "Suggest at most ONE action per response; propose the next step only after seeing the result. "
        "If no real command applies, set command to null."
    )

    messages = build_chat_messages(system_prompt, payload)

    def generator():
        import re
        try:
            ollama_client = OllamaClient(settings)
            if payload.model:
                ollama_client.model = payload.model

            # The frontend UI natively handles showing a "Thinking..." indicator
            # No need to send it as a text chunk here.

            buffer = ""
            metadata_started = False

            for event in ollama_client.chat_stream_events(messages, get_tool_definitions()):
                if event["type"] == "content_chunk":
                    chunk = event["text"]
                    if not metadata_started:
                        buffer += chunk
                        idx = buffer.find("###METADATA###")
                        if idx != -1:
                            metadata_started = True
                            valid_text = buffer[:idx]
                            if valid_text:
                                yield json.dumps({"type": "chunk", "text": valid_text}) + "\n"
                            buffer = buffer[idx + len("###METADATA###"):]
                        else:
                            # Safely yield if not containing partial ###METADATA###
                            if len(buffer) > 14:
                                valid_text = buffer[:-14]
                                yield json.dumps({"type": "chunk", "text": valid_text}) + "\n"
                                buffer = buffer[-14:]
                    else:
                        buffer += chunk

                elif event["type"] == "tool_call":
                    yield json.dumps({
                        "type": "tool_call",
                        "tool": event.get("name", "unknown"),
                        "args": event.get("args", {}),
                    }) + "\n"
                
                elif event["type"] == "tool_result":
                    yield json.dumps({
                        "type": "tool_call_result",
                        "result": event.get("result", ""),
                    }) + "\n"

                elif event["type"] == "final_answer":
                    # Yield any remaining text if metadata hasn't started
                    if not metadata_started and buffer:
                        yield json.dumps({"type": "chunk", "text": buffer}) + "\n"
                        buffer = ""
                    
                    # Try parsing the metadata if any
                    metadata = {}
                    if buffer.strip():
                        try:
                            cleaned = buffer.strip()
                            fence_match = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", cleaned, re.DOTALL)
                            if fence_match:
                                cleaned = fence_match.group(1).strip()
                            metadata = json.loads(cleaned)
                        except Exception:
                            pass

                    # Extract suggested actions safely
                    raw_actions = metadata.get("suggested_actions", [])
                    normalized_actions = []
                    if isinstance(raw_actions, list):
                        for item in raw_actions:
                            if not isinstance(item, dict): continue
                            normalized_actions.append({
                                "action": str(item.get("action", "Investigate issue")),
                                "command": item.get("command"),
                                "target": item.get("target"),
                                "risk": str(item.get("risk", "medium")),
                            })

                    full_content = event.get("content", "")
                    idx = full_content.find("###METADATA###")
                    summary_text = full_content[:idx].strip() if idx != -1 else full_content.strip()

                    payload_out = {
                        "summary": summary_text,
                        "reasoning": str(metadata.get("reasoning", "")),
                        "confidence": float(metadata.get("confidence", 0.0) if isinstance(metadata.get("confidence"), (int, float)) else 0.0),
                        "suggested_actions": normalized_actions,
                    }

                    # Surface actionable suggestions so the UI can create
                    # approval requests for them.
                    for action in normalized_actions:
                        if action.get("command"):
                            yield json.dumps({
                                "type": "suggested_action",
                                "action": action,
                            }) + "\n"

                    yield json.dumps({"type": "final", "payload": payload_out}) + "\n"

        except Exception as exc:
            logger.error(f"Streaming failed: {exc}")
            yield json.dumps({"type": "error", "error": str(exc)}) + "\n"

    return StreamingResponse(generator(), media_type="application/x-ndjson")


@router.get("/models", response_model=List[str])
def get_models():
    settings = get_settings()
    try:
        client = OllamaClient(settings)
        models = client.list_models()
        return models
    except Exception as exc:
        logger.error(f"Failed to list models: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list models")


@router.post("/ingest/logs", response_model=LogIngestionResult)
def ingest_logs(request: LogIngestionRequest):
    """Fetch logs from Loki and persist to Qdrant."""
    settings = get_settings()
    batch_id = str(uuid.uuid4())
    
    try:
        loki_client = LokiClient(settings)
        all_logs = []

        # If a LogQL label_query is provided, use it (e.g. '{job="prometheus"}')
        if request.label_query:
            try:
                all_logs = loki_client.get_logs_by_label(
                    request.label_query, since_minutes=request.since_minutes
                )
                containers = [f"label_query:{request.label_query}"]
            except Exception as e:
                logger.warning(f"Failed to fetch logs for label_query {request.label_query}: {e}")
                containers = []
        else:
            # Get list of containers to ingest
            if request.containers:
                containers = request.containers
            else:
                # Get all containers from Proxmox
                client = ProxmoxClient(settings)
                container_list = client.list_all_containers()
                containers = [c["name"] for c in container_list]

            # Fetch logs from Loki for each container
            for container_name in containers:
                try:
                    logs = loki_client.get_logs_for_container(
                        container_name, since_minutes=request.since_minutes
                    )
                    all_logs.extend(logs)
                except Exception as e:
                    logger.warning(f"Failed to fetch logs for {container_name}: {e}")
        
        # Attempt to map host-level logs to known containers (simple heuristic)
        try:
            client = ProxmoxClient(settings)
            container_infos = client.list_all_containers()
            container_names = [c.get("name", "").lower() for c in container_infos]
            container_hostnames = [c.get("hostname", "") for c in container_infos if c.get("hostname")]
        except Exception:
            container_infos = []
            container_names = []
            container_hostnames = []

        for log in all_logs:
            # prefer existing container label if present
            if log.get("container") and not str(log.get("container")).startswith("label_query:"):
                continue
            msg = str(log.get("message", "")).lower()
            assigned = None
            for name in container_names:
                if name and name in msg:
                    assigned = name
                    break
            if not assigned:
                for hn in container_hostnames:
                    if hn and hn.lower() in msg:
                        # find container with this hostname
                        for c in container_infos:
                            if c.get("hostname") and c.get("hostname").lower() == hn.lower():
                                assigned = c.get("name")
                                break
                        if assigned:
                            break
            if assigned:
                log["container"] = assigned
            else:
                # fallback: keep host label or mark as host
                if request.label_query:
                    log["container"] = f"label_query:{request.label_query}"
                else:
                    log.setdefault("container", "host")

        # Store in Qdrant
        log_store = LogStore(settings)
        total_ingested = log_store.store_logs(all_logs, batch_id)
        
        return LogIngestionResult(
            batch_id=batch_id,
            timestamp=datetime.now(timezone.utc),
            total_logs_ingested=total_ingested,
            containers_processed=containers,
            success=True,
        )
    except Exception as exc:
        logger.error(f"Failed to ingest logs: {exc}")
        raise HTTPException(status_code=500, detail="Failed to ingest logs")


@router.post("/logs/search", response_model=LogSearchResult)
def search_logs(request: LogSearchRequest):
    """Semantic search over ingested logs."""
    settings = get_settings()
    
    try:
        log_store = LogStore(settings)
        results = log_store.search_logs(
            query_text=request.query,
            container=request.container,
            limit=request.limit,
        )
        
        log_entries = [
            LogEntry(
                timestamp=r["timestamp"],
                container=r["container"],
                message=r["message"],
                labels=r.get("labels", {}),
            )
            for r in results
        ]
        
        return LogSearchResult(
            query=request.query,
            timestamp=datetime.now(timezone.utc),
            results=log_entries,
            total_results=len(log_entries),
        )
    except Exception as exc:
        logger.error(f"Failed to search logs: {exc}")
        raise HTTPException(status_code=500, detail="Failed to search logs")


@router.get("/logs/recent", response_model=List[LogEntry])
def get_recent_logs(container: str | None = None, limit: int = 100):
    """Get most recent logs."""
    settings = get_settings()
    
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    
    try:
        log_store = LogStore(settings)
        results = log_store.get_recent_logs(container=container, limit=limit)
        
        logger.info(f"Returning {len(results)} recent logs (container={container}, limit={limit})")
        
        return [
            LogEntry(
                timestamp=r["timestamp"],
                container=r["container"],
                message=r["message"],
                labels=r.get("labels", {}),
            )
            for r in results
        ]
    except Exception as exc:
        logger.error(f"Failed to get recent logs: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to get recent logs: {exc}")


@router.post("/approvals", response_model=ApprovalItem)
def create_approval(request: ApprovalCreateRequest):
    try:
        item = approval_store.create(
            action=request.action,
            command=request.command,
            target=request.target,
            risk=request.risk,
            source_query=request.source_query,
            requested_by=request.requested_by,
        )
        return ApprovalItem(**item)
    except Exception as exc:
        logger.error(f"Failed to create approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create approval")


@router.get("/approvals", response_model=List[ApprovalItem])
def list_approvals(status: str | None = None):
    allowed = {"pending", "approved", "rejected", "executed"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status must be pending, approved, rejected, or executed")

    try:
        items = approval_store.list(status=status)
        return [ApprovalItem(**item) for item in items]
    except Exception as exc:
        logger.error(f"Failed to list approvals: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list approvals")


@router.get("/approvals/{approval_id}", response_model=ApprovalItem)
def get_approval(approval_id: str):
    try:
        item = approval_store.get(approval_id)
        if not item:
            raise HTTPException(status_code=404, detail="Approval not found")
        return ApprovalItem(**item)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to get approval")


@router.delete("/approvals/{approval_id}")
def delete_approval(approval_id: str):
    try:
        if not approval_store.delete(approval_id):
            raise HTTPException(status_code=404, detail="Approval not found")
        return {"deleted": approval_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to delete approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to delete approval")


@router.post("/approvals/cleanup")
def cleanup_approvals(remove_empty: bool = True, action: str | None = None, finished: bool = False):
    """Remove approvals matching any of the given filters (OR semantics).

    - `remove_empty`: delete approvals where `command` is NULL or empty
    - `action`: delete approvals with this action value
    - `finished`: delete executed and rejected approvals
    """
    try:
        statuses = ["executed", "rejected"] if finished else None
        deleted = approval_store.cleanup(remove_empty=remove_empty, action=action, statuses=statuses)
        return {"deleted": deleted}
    except Exception as exc:
        logger.error(f"Failed to cleanup approvals: {exc}")
        raise HTTPException(status_code=500, detail="Failed to cleanup approvals")


@router.patch("/approvals/{approval_id}", response_model=ApprovalItem)
def decide_approval(approval_id: str, request: ApprovalDecisionRequest):
    if request.decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")

    try:
        existing = approval_store.get(approval_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Approval not found")

        updated = approval_store.decide(
            approval_id=approval_id,
            decision=request.decision,
            reviewer=request.reviewer,
            note=request.note,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update approval")
        return ApprovalItem(**updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to decide approval: {exc}")
        raise HTTPException(status_code=500, detail="Failed to decide approval")


@router.post("/execute", response_model=ExecutionResult)
def execute_command(request: ExecuteRequest):
    """Execute an approved, validated diagnostic command over SSH.

    Only executions tied to an approval (status == 'approved') are allowed.
    Successful executions mark the approval as 'executed' so it cannot run again.
    """
    # Resolve command and approval
    cmd = request.command
    target = request.target
    approval_id = request.approval_id

    if not approval_id:
        # For safety, disallow executions without an approval record in this MVP
        raise HTTPException(status_code=400, detail="Execution requires an approved approval_id")

    existing = approval_store.get(approval_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Approval not found")

    status = existing.get("status")
    if status in ("executing", "executed"):
        raise HTTPException(status_code=409, detail="Approval has already been executed")
    if status != "approved":
        raise HTTPException(status_code=400, detail="Approval is not approved for execution")

    # Atomically claim the approval so two concurrent /execute calls cannot both
    # run the same approved command (TOCTOU). Only the caller that flips
    # approved -> executing proceeds; the loser gets a 409.
    claimed = approval_store.claim_for_execution(approval_id)
    if not claimed:
        raise HTTPException(status_code=409, detail="Approval is already being executed")

    # Only the command stored on the approval may run — a client-supplied
    # command must match it exactly, otherwise the approval would no longer
    # bind what actually executes.
    approved_cmd = (claimed.get("command") or "").strip()
    if cmd and approved_cmd and cmd.strip() != approved_cmd:
        approval_store.release_claim(approval_id, note="client command did not match approved command")
        raise HTTPException(status_code=400, detail="Command does not match the approved command")

    cmd = approved_cmd
    target = claimed.get("target") or target

    if not cmd:
        approval_store.release_claim(approval_id, note="no command available to execute")
        raise HTTPException(status_code=400, detail="No command available to execute")

    try:
        result = exec_service.execute(cmd, target, timeout=request.timeout)
    except ValueError as ve:
        approval_store.release_claim(approval_id, note=f"validation failed: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.error(f"Execution failed: {exc}")
        approval_store.release_claim(approval_id, note=f"execution error: {exc}")
        raise HTTPException(status_code=500, detail=f"Execution failed: {exc}")

    returncode = result.get("returncode", -1)
    try:
        approval_store.mark_executed(approval_id, note=f"executed with exit code {returncode}")
    except Exception as exc:
        logger.warning(f"Failed to mark approval {approval_id} as executed: {exc}")

    return ExecutionResult(
        approval_id=approval_id,
        command=cmd,
        target=target,
        returncode=returncode,
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        executed_at=datetime.now(timezone.utc),
    )


@router.get("/settings", response_model=SettingsResponse)
def get_current_settings():
    """Return current non-sensitive configuration. Secrets are write-only."""
    s = get_settings()
    return SettingsResponse(
        app_env=s.app_env,
        app_host=s.app_host,
        app_port=s.app_port,
        proxmox_url=s.proxmox_url,
        proxmox_host_ip=s.proxmox_host_ip,
        proxmox_ip=s.proxmox_ip,
        proxmox_node=s.proxmox_node,
        proxmox_port=s.proxmox_port,
        proxmox_realm=s.proxmox_realm,
        proxmox_user=s.proxmox_user,
        proxmox_token_id=s.proxmox_token_id,
        proxmox_ssh_user=s.proxmox_ssh_user,
        proxmox_verify_ssl=s.proxmox_verify_ssl,
        qdrant_url=s.qdrant_url,
        qdrant_current_collection_name=s.qdrant_current_collection_name,
        qdrant_history_collection_name=s.qdrant_history_collection_name,
        qdrant_logs_collection_name=s.qdrant_logs_collection_name,
        ollama_url=s.ollama_url,
        ollama_model=s.ollama_model,
        ollama_embed_model=s.ollama_embed_model,
        ollama_num_ctx=s.ollama_num_ctx,
        loki_url=s.loki_url,
        prometheus_url=s.prometheus_url,
        approval_db_path=s.approval_db_path,
    )


@router.patch("/settings", response_model=SettingsSavedResponse)
def update_settings(payload: SettingsUpdateRequest):
    """Update .env file with provided values. Server restart required for changes to take effect."""
    env_path = Path(__file__).resolve().parents[2] / ".env"

    # Read current .env
    env = _read_env_file(env_path)

    updated_fields: list[str] = []
    for field_name, value in payload.model_dump(exclude_none=True).items():
        env_key = _ENV_VAR_MAP.get(field_name)
        if env_key is None:
            continue
        str_value = str(value) if not isinstance(value, bool) else str(value).lower()
        # Strip newlines so a crafted value cannot inject extra .env lines.
        str_value = str_value.replace("\n", " ").replace("\r", " ").strip()
        env[env_key] = str_value
        updated_fields.append(field_name)

    if not updated_fields:
        return SettingsSavedResponse(saved=False, message="No fields provided to update.")

    _write_env_file(env_path, env)

    return SettingsSavedResponse(
        saved=True,
        message=f"Updated {len(updated_fields)} field(s): {', '.join(updated_fields)}. Restart the server for changes to take effect.",
    )


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

_ENV_VAR_MAP: dict[str, str] = {
    "app_env": "APP_ENV",
    "app_host": "APP_HOST",
    "app_port": "APP_PORT",
    "proxmox_url": "PROXMOX_URL",
    "proxmox_host_ip": "PROXMOX_HOST_IP",
    "proxmox_ip": "PROXMOX_IP",
    "proxmox_node": "PROXMOX_NODE",
    "proxmox_port": "PROXMOX_PORT",
    "proxmox_realm": "PROXMOX_REALM",
    "proxmox_user": "PROXMOX_USER",
    "proxmox_token_id": "PROXMOX_TOKEN_ID",
    "proxmox_token_secret": "PROXMOX_TOKEN_SECRET",
    "proxmox_password": "PROXMOX_PASSWORD",
    "proxmox_ssh_user": "PROXMOX_SSH_USER",
    "proxmox_verify_ssl": "PROXMOX_VERIFY_SSL",
    "qdrant_url": "QDRANT_URL",
    "qdrant_api_key": "QDRANT_API_KEY",
    "qdrant_current_collection_name": "QDRANT_CURRENT_COLLECTION_NAME",
    "qdrant_history_collection_name": "QDRANT_HISTORY_COLLECTION_NAME",
    "qdrant_logs_collection_name": "QDRANT_LOGS_COLLECTION_NAME",
    "ollama_url": "OLLAMA_URL",
    "ollama_model": "OLLAMA_MODEL",
    "ollama_embed_model": "OLLAMA_EMBED_MODEL",
    "ollama_num_ctx": "OLLAMA_NUM_CTX",
    "loki_url": "LOKI_URL",
    "prometheus_url": "PROMETHEUS_URL",
    "approval_db_path": "APPROVAL_DB_PATH",
}


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a .env file into a flat {KEY: value} dict (ignores comments / blanks)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    """Write a flat {KEY: value} dict back to a .env file, preserving comments."""
    lines: list[str] = []
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(raw_line)
                continue
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in env:
                lines.append(f"{key}={env[key]}")
                del env[key]
            else:
                lines.append(raw_line)
    # Append any new keys that weren't in the original file
    for key, value in env.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")



