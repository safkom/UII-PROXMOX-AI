import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from backend.config.settings import Settings


class SnapshotStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
        )

    def ensure_collection(self) -> None:
        try:
            self.client.get_collection(self.settings.qdrant_current_collection_name)
        except Exception:
            self.client.recreate_collection(
                collection_name=self.settings.qdrant_current_collection_name,
                vectors_config=qdrant_models.VectorParams(size=4, distance=qdrant_models.Distance.COSINE),
            )

        try:
            self.client.get_collection(self.settings.qdrant_history_collection_name)
        except Exception:
            self.client.recreate_collection(
                collection_name=self.settings.qdrant_history_collection_name,
                vectors_config=qdrant_models.VectorParams(size=4, distance=qdrant_models.Distance.COSINE),
            )

    def store_scan_snapshot(self, scan_data: dict[str, Any]) -> str:
        self.ensure_collection()
        scan_id = self._snapshot_id(scan_data)
        timestamp = scan_data.get("timestamp") or datetime.now(timezone.utc).isoformat()
        diagnostics = scan_data.get("diagnostics", [])

        current_points = []
        for container in scan_data.get("containers", []):
            container_payload = container.model_dump() if hasattr(container, "model_dump") else container.dict()
            point_id = self._container_point_id(scan_id, container_payload)
            current_points.append(
                qdrant_models.PointStruct(
                    id=point_id,
                    vector=self._vector_from_container(container_payload),
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

        history_payload = {
            "kind": "infrastructure_scan",
            "scan_id": scan_id,
            "timestamp": timestamp,
            "container_count": scan_data.get("container_count", 0),
            "scanned_nodes": scan_data.get("scanned_nodes", 0),
            "diagnostics": diagnostics,
            "containers": [
                container.model_dump() if hasattr(container, "model_dump") else container.dict()
                for container in scan_data.get("containers", [])
            ],
        }

        history_point = qdrant_models.PointStruct(
            id=scan_id,
            vector=self._vector_from_history(history_payload),
            payload=history_payload,
        )
        self.client.upsert(
            collection_name=self.settings.qdrant_history_collection_name,
            points=[history_point],
        )

        return scan_id

    def list_current_infrastructure(self) -> list[dict[str, Any]]:
        self.ensure_collection()
        records, _ = self.client.scroll(
            collection_name=self.settings.qdrant_current_collection_name,
            with_payload=True,
            with_vectors=False,
            limit=1000,
        )
        return [record.payload or {} for record in records]

    def list_history_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_collection()
        records, _ = self.client.scroll(
            collection_name=self.settings.qdrant_history_collection_name,
            with_payload=True,
            with_vectors=False,
            limit=max(limit, 1),
        )
        payloads = [record.payload or {} for record in records]
        payloads.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return payloads[:limit]

    def get_history_scan(self, scan_id: str) -> dict[str, Any] | None:
        self.ensure_collection()
        records = self.client.retrieve(
            collection_name=self.settings.qdrant_history_collection_name,
            ids=[scan_id],
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            return None
        return records[0].payload or {}

    @staticmethod
    def _snapshot_id(scan_data: dict[str, Any]) -> str:
        normalized = json.dumps(scan_data, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(normalized).hexdigest()
        return digest[:32]

    @staticmethod
    def _container_point_id(scan_id: str, container_payload: dict[str, Any]) -> str:
        stable_key = {
            "scan_id": scan_id,
            "vmid": container_payload.get("vmid"),
            "type": container_payload.get("type"),
            "node": container_payload.get("node"),
            "name": container_payload.get("name"),
        }
        normalized = json.dumps(stable_key, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()[:32]

    @staticmethod
    def _vector_from_container(container_payload: dict[str, Any]) -> list[float]:
        normalized = json.dumps(container_payload, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(normalized).digest()
        values = []
        for index in range(4):
            chunk = digest[index * 8 : (index + 1) * 8]
            integer_value = int.from_bytes(chunk, "big", signed=False)
            values.append((integer_value % 1000000) / 1000000.0)
        return values

    @staticmethod
    def _vector_from_history(history_payload: dict[str, Any]) -> list[float]:
        normalized = json.dumps(history_payload, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(normalized).digest()
        values = []
        for index in range(4):
            chunk = digest[index * 8 : (index + 1) * 8]
            integer_value = int.from_bytes(chunk, "big", signed=False)
            values.append((integer_value % 1000000) / 1000000.0)
        return values
