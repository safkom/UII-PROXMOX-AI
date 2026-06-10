import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from backend.config.settings import Settings
from backend.ollama.embeddings import OllamaEmbeddings

logger = logging.getLogger(__name__)


class SnapshotStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
        )
        self.embeddings = OllamaEmbeddings(settings)

    def _ensure_collection(self, name: str) -> None:
        """Create a collection, recreating it if the embedding dimension changed."""
        expected_size = self.embeddings.dimension()
        try:
            info = self.client.get_collection(name)
            if info.config.params.vectors.size == expected_size:
                return
            logger.warning(f"Recreating '{name}': embedding dimension changed")
            self.client.delete_collection(name)
        except Exception:
            pass
        self.client.create_collection(
            collection_name=name,
            vectors_config=qdrant_models.VectorParams(
                size=expected_size, distance=qdrant_models.Distance.COSINE
            ),
        )

    def store_scan_snapshot(self, scan_data: dict[str, Any]) -> str:
        self._ensure_collection(self.settings.qdrant_current_collection_name)
        self._ensure_collection(self.settings.qdrant_history_collection_name)

        scan_id = self._snapshot_id(scan_data)
        timestamp = scan_data.get("timestamp") or datetime.now(timezone.utc).isoformat()
        diagnostics = scan_data.get("diagnostics", [])

        container_payloads = [
            container.model_dump() if hasattr(container, "model_dump") else dict(container)
            for container in scan_data.get("containers", [])
        ]

        current_points = []
        if container_payloads:
            vectors = self.embeddings.embed(
                [self._container_description(payload) for payload in container_payloads]
            )
            for container_payload, vector in zip(container_payloads, vectors):
                current_points.append(
                    qdrant_models.PointStruct(
                        # Stable per-container id so each scan updates points in
                        # place instead of accumulating duplicates.
                        id=self._container_point_id(container_payload),
                        vector=vector,
                        payload={
                            "kind": "infrastructure_container",
                            "scan_id": scan_id,
                            "timestamp": timestamp,
                            "scanned_nodes": scan_data.get("scanned_nodes", 0),
                            "diagnostics": diagnostics,
                            **container_payload,
                        },
                    )
                )

        if current_points:
            self.client.upsert(
                collection_name=self.settings.qdrant_current_collection_name,
                points=current_points,
            )
            # Drop containers that no longer exist on the cluster.
            self.client.delete(
                collection_name=self.settings.qdrant_current_collection_name,
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must_not=[
                            qdrant_models.FieldCondition(
                                key="scan_id", match=qdrant_models.MatchValue(value=scan_id)
                            )
                        ]
                    )
                ),
            )

        history_payload = {
            "kind": "infrastructure_scan",
            "scan_id": scan_id,
            "timestamp": timestamp,
            "container_count": scan_data.get("container_count", 0),
            "scanned_nodes": scan_data.get("scanned_nodes", 0),
            "diagnostics": diagnostics,
            "containers": container_payloads,
        }

        history_point = qdrant_models.PointStruct(
            id=scan_id,
            vector=self.embeddings.embed_one(self._history_description(history_payload)),
            payload=history_payload,
        )
        self.client.upsert(
            collection_name=self.settings.qdrant_history_collection_name,
            points=[history_point],
        )

        return scan_id

    def list_current_infrastructure(self) -> list[dict[str, Any]]:
        try:
            records, _ = self.client.scroll(
                collection_name=self.settings.qdrant_current_collection_name,
                with_payload=True,
                with_vectors=False,
                limit=1000,
            )
        except Exception as exc:
            logger.warning(f"Failed to read current infrastructure: {exc}")
            return []
        return [record.payload or {} for record in records]

    def list_history_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            records, _ = self.client.scroll(
                collection_name=self.settings.qdrant_history_collection_name,
                with_payload=True,
                with_vectors=False,
                # Scroll returns points in id order, so over-fetch and sort by
                # timestamp before applying the limit.
                limit=max(limit, 256),
            )
        except Exception as exc:
            logger.warning(f"Failed to read scan history: {exc}")
            return []
        payloads = [record.payload or {} for record in records]
        payloads.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return payloads[:limit]

    def get_history_scan(self, scan_id: str) -> dict[str, Any] | None:
        try:
            records = self.client.retrieve(
                collection_name=self.settings.qdrant_history_collection_name,
                ids=[scan_id],
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.warning(f"Failed to read scan {scan_id}: {exc}")
            return None
        if not records:
            return None
        return records[0].payload or {}

    @staticmethod
    def _container_description(container_payload: dict[str, Any]) -> str:
        """Natural-language description so infrastructure is semantically searchable."""
        parts = [
            f"{container_payload.get('name', 'unknown')} is a {container_payload.get('type', 'container')}",
            f"on node {container_payload.get('node', 'unknown')}",
            f"status {container_payload.get('status', 'unknown')}",
        ]
        if container_payload.get("ip"):
            parts.append(f"with IP {container_payload['ip']}")
        if container_payload.get("hostname"):
            parts.append(f"hostname {container_payload['hostname']}")
        return ", ".join(parts)

    @staticmethod
    def _history_description(history_payload: dict[str, Any]) -> str:
        names = ", ".join(
            str(container.get("name", "unknown")) for container in history_payload.get("containers", [])
        )
        return (
            f"Infrastructure scan of {history_payload.get('container_count', 0)} containers "
            f"across {history_payload.get('scanned_nodes', 0)} nodes: {names}"
        )

    @staticmethod
    def _snapshot_id(scan_data: dict[str, Any]) -> str:
        normalized = json.dumps(scan_data, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()
        return str(uuid.UUID(hex=digest[:32]))

    @staticmethod
    def _container_point_id(container_payload: dict[str, Any]) -> str:
        stable_key = {
            "vmid": container_payload.get("vmid"),
            "type": container_payload.get("type"),
            "node": container_payload.get("node"),
        }
        normalized = json.dumps(stable_key, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()
        return str(uuid.UUID(hex=digest[:32]))
