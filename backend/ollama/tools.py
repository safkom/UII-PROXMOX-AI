"""Tool definitions for the Ollama chat backend.

Each function decorated with @register_tool becomes available to the LLM
via the OpenAI function-calling protocol.
"""

from backend.ollama.client import register_tool
from backend.config.settings import get_settings
from backend.proxmox.client import ProxmoxClient
from backend.loki.client import LokiClient
from backend.qdrant.logs import LogStore


@register_tool("scan_containers")
def scan_containers() -> dict:
    """List all containers and VMs on the Proxmox cluster with their status, IP, and type."""
    settings = get_settings()
    client = ProxmoxClient(settings)
    containers = client.list_all_containers()
    return {
        "count": len(containers),
        "containers": containers,
    }


@register_tool("get_logs")
def get_logs(container: str = "", limit: int = 20) -> dict:
    """Fetch recent logs from Loki for a specific container or all containers."""
    settings = get_settings()
    loki_client = LokiClient(settings)
    if container:
        logs = loki_client.get_logs_for_container(container, since_minutes=60)
    else:
        logs = loki_client.get_logs_by_label('{job=~".*"}', since_minutes=60)
    return {
        "container": container or "all",
        "count": len(logs),
        "logs": logs[:limit],
    }


@register_tool("search_logs")
def search_logs(query: str, container: str = "", limit: int = 10) -> dict:
    """Semantic search over previously ingested logs in Qdrant."""
    settings = get_settings()
    log_store = LogStore(settings)
    results = log_store.search_logs(query, container=container, limit=limit)
    return {
        "query": query,
        "results": results,
    }


