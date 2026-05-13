from datetime import datetime
from typing import Optional

from pydantic import Field

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ServiceHealth(BaseModel):
    name: str
    url: str
    ok: bool
    status_code: Optional[int] = None
    detail: Optional[str] = None


class HealthReport(BaseModel):
    status: str
    services: list[ServiceHealth]


class Container(BaseModel):
    vmid: int
    name: str
    type: str  # "lxc" or "qemu"
    node: str
    status: str
    ip: Optional[str] = None
    hostname: Optional[str] = None


class ScanResult(BaseModel):
    timestamp: datetime
    container_count: int
    containers: list[Container]
    success: bool
    scanned_nodes: int = 0
    diagnostics: list[str] = Field(default_factory=list)
    history_snapshot_id: str | None = None


class InfrastructureHistoryItem(BaseModel):
    scan_id: str
    timestamp: datetime
    container_count: int
    scanned_nodes: int = 0
    diagnostics: list[str] = Field(default_factory=list)
    containers: list[Container] = Field(default_factory=list)


class InfrastructureSummary(BaseModel):
    current: list[Container] = Field(default_factory=list)
    latest_scan: InfrastructureHistoryItem | None = None
    history: list[InfrastructureHistoryItem] = Field(default_factory=list)


class LogEntry(BaseModel):
    timestamp: str
    container: str
    message: str
    labels: dict = Field(default_factory=dict)


class LogIngestionRequest(BaseModel):
    containers: list[str] | None = None  # If None, ingest all
    label_query: str | None = None  # LogQL label query, e.g. '{job="prometheus"}'
    since_minutes: int = Field(60, description="How far back to fetch logs")


class LogIngestionResult(BaseModel):
    batch_id: str
    timestamp: datetime
    total_logs_ingested: int
    containers_processed: list[str]
    success: bool


class LogSearchRequest(BaseModel):
    query: str
    container: str | None = None
    limit: int = Field(10, ge=1, le=100)


class LogSearchResult(BaseModel):
    query: str
    timestamp: datetime
    results: list[LogEntry]
    total_results: int


class ChatRequest(BaseModel):
    query: str
    include_logs: bool = True
    log_limit: int = Field(20, ge=1, le=200)


class SuggestedAction(BaseModel):
    action: str
    command: str | None = None
    target: str | None = None
    risk: str = "medium"


class ChatResponse(BaseModel):
    timestamp: datetime
    query: str
    summary: str
    reasoning: str
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)


class ApprovalCreateRequest(BaseModel):
    action: str
    command: str | None = None
    target: str | None = None
    risk: str = "medium"
    source_query: str | None = None
    requested_by: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str  # approved | rejected
    reviewer: str | None = None
    note: str | None = None


class ApprovalItem(BaseModel):
    id: str
    status: str
    action: str
    command: str | None = None
    target: str | None = None
    risk: str = "medium"
    source_query: str | None = None
    requested_by: str | None = None
    reviewer: str | None = None
    review_note: str | None = None
    created_at: datetime
    updated_at: datetime
