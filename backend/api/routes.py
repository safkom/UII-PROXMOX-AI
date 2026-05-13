import logging
import uuid
from datetime import datetime
from typing import Any, List

from fastapi import APIRouter, HTTPException

from backend.approvals.store import ApprovalStore
from backend.config.settings import get_settings
from backend.ollama.client import OllamaClient
from backend.proxmox.client import ProxmoxClient
from backend.qdrant.snapshots import SnapshotStore
from backend.loki.client import LokiClient
from backend.qdrant.logs import LogStore

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
)

logger = logging.getLogger(__name__)

router = APIRouter()
approval_store = ApprovalStore()


def collect_service_health() -> list[ServiceHealth]:
    settings = get_settings()
    probes = [
        probe_http_service(
            "proxmox",
            settings.proxmox_url,
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
        raise HTTPException(status_code=500, detail="Failed to fetch containers from Proxmox")
    return []


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
                timestamp=point.get("timestamp", datetime.utcnow()),
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
            timestamp=point.get("timestamp", datetime.utcnow()),
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
                timestamp=point.get("timestamp", datetime.utcnow()),
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
                "timestamp": datetime.utcnow().isoformat(),
                "container_count": len(container_models),
                "containers": container_models,
                "scanned_nodes": scan_data["scanned_nodes"],
                "diagnostics": scan_data["diagnostics"],
            }
        )
        return ScanResult(
            timestamp=datetime.utcnow(),
            container_count=len(container_models),
            containers=container_models,
            success=True,
            scanned_nodes=scan_data["scanned_nodes"],
            diagnostics=scan_data["diagnostics"],
            history_snapshot_id=snapshot_id,
        )
    except Exception as exc:
        logger.error(f"Failed to scan infrastructure: {exc}")
        raise HTTPException(status_code=500, detail="Failed to scan infrastructure")


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    settings = get_settings()

    try:
        snapshot_store = SnapshotStore(settings)
        current_infra = snapshot_store.list_current_infrastructure()
    except Exception as exc:
        logger.warning(f"Failed to read infrastructure context for chat: {exc}")
        current_infra = []

    logs_context: list[dict[str, Any]] = []
    if payload.include_logs:
        try:
            log_store = LogStore(settings)
            logs_context = log_store.get_recent_logs(limit=payload.log_limit)
        except Exception as exc:
            logger.warning(f"Failed to read log context for chat: {exc}")

    # Keep context compact to avoid very large prompts.
    infra_brief = [
        {
            "name": c.get("name"),
            "type": c.get("type"),
            "node": c.get("node"),
            "status": c.get("status"),
            "ip": c.get("ip"),
        }
        for c in current_infra
    ]

    system_prompt = (
        "You are an on-prem Proxmox homelab DevOps assistant. "
        "Answer using ONLY valid JSON with this schema: "
        '{"summary": string, "reasoning": string, "confidence": number between 0 and 1, '
        '"suggested_actions": [{"action": string, "command": string|null, "target": string|null, "risk": "low|medium|high"}]}. '
        "Do not include markdown. If not enough data, say so and keep confidence low. "
        "Never claim an action was executed."
    )

    prompt = (
        f"User query:\n{payload.query}\n\n"
        f"Current infrastructure ({len(infra_brief)} items):\n{infra_brief}\n\n"
        f"Recent logs ({len(logs_context)} items):\n{logs_context}\n"
    )

    try:
        ollama_client = OllamaClient(settings)
        model_result = ollama_client.generate_json(prompt=prompt, system_prompt=system_prompt)
    except Exception as exc:
        logger.error(f"Failed to query Ollama: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate chat response")

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
        timestamp=datetime.utcnow(),
        query=payload.query,
        summary=str(model_result.get("summary", "No summary available.")),
        reasoning=str(model_result.get("reasoning", "No reasoning provided.")),
        confidence=confidence,
        suggested_actions=normalized_actions,
        context={
            "infrastructure_count": len(infra_brief),
            "logs_count": len(logs_context),
            "model": settings.ollama_model,
        },
    )


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
            timestamp=datetime.utcnow(),
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
            timestamp=datetime.utcnow(),
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
        raise HTTPException(status_code=500, detail="Failed to get recent logs")


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
    allowed = {"pending", "approved", "rejected"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status must be pending, approved, or rejected")

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
