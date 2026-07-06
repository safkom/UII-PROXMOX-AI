import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from backend.config.settings import Settings
from backend.ollama.embeddings import OllamaEmbeddings

logger = logging.getLogger(__name__)

# Upper bound on points fetched when sorting client-side; homelab log volumes
# stay far below this between ingestion runs.
_SCROLL_WINDOW = 1024


class LogStore:
    """Qdrant storage for log entries with semantic search capability."""

    def __init__(self, settings: Settings):
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        self.collection_name = settings.qdrant_logs_collection_name
        self.embeddings = OllamaEmbeddings(settings)

    def ensure_collection(self):
        """Create the logs collection, recreating it if the vector size changed."""
        expected_size = self.embeddings.dimension()
        try:
            info = self.client.get_collection(self.collection_name)
            current_size = info.config.params.vectors.size
            if current_size == expected_size:
                return
            logger.warning(
                f"Recreating '{self.collection_name}': vector size {current_size} != {expected_size} "
                f"(embedding model changed)"
            )
            self.client.delete_collection(self.collection_name)
        except Exception:
            pass
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(size=expected_size, distance=Distance.COSINE),
        )

    def store_logs(self, logs: list[dict], batch_id: str) -> int:
        """Embed and store a batch of logs. Returns the number of logs stored."""
        if not logs:
            return 0

        self.ensure_collection()
        texts = [f"{log.get('container', '')} {log.get('message', '')}" for log in logs]
        vectors = self.embeddings.embed(texts)

        points = []
        for idx, (log, vector) in enumerate(zip(logs, vectors)):
            payload = {
                "batch_id": batch_id,
                "timestamp": log.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "container": log.get("container", "unknown"),
                "message": log.get("message", ""),
                "labels": log.get("labels", {}),
            }
            points.append(
                PointStruct(id=self._generate_point_id(batch_id, idx, log), vector=vector, payload=payload)
            )

        self.client.upsert(collection_name=self.collection_name, points=points)
        return len(points)

    def search_logs(
        self,
        query_text: str,
        container: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Semantic search over ingested logs, optionally filtered by container."""
        try:
            query_vector = self.embeddings.embed_one(query_text)
            # query_points replaces the legacy search(), which was removed in
            # newer qdrant-client releases (requirements pin only >=1.10).
            results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=self._container_filter(container),
                limit=limit,
            ).points
        except Exception as exc:
            logger.warning(f"Log search failed: {exc}")
            return []

        return [
            {
                "timestamp": point.payload.get("timestamp"),
                "container": point.payload.get("container"),
                "message": point.payload.get("message"),
                "score": point.score,
            }
            for point in results
        ]

    def get_recent_logs(
        self,
        container: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get the most recent logs by timestamp, optionally filtered by container."""
        try:
            points, _ = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._container_filter(container),
                limit=_SCROLL_WINDOW,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.warning(f"Failed to scroll logs from '{self.collection_name}': {exc}")
            return []

        logs = [
            {
                "timestamp": point.payload.get("timestamp", ""),
                "container": point.payload.get("container", ""),
                "message": point.payload.get("message", ""),
                "labels": point.payload.get("labels", {}),
            }
            for point in points
        ]
        # Qdrant scroll returns points in id order, so sort by timestamp here.
        logs.sort(key=lambda log: log["timestamp"], reverse=True)
        return logs[:limit]

    @staticmethod
    def _container_filter(container: Optional[str]) -> Optional[Filter]:
        if not container:
            return None
        return Filter(must=[FieldCondition(key="container", match=MatchValue(value=container))])

    @staticmethod
    def _generate_point_id(batch_id: str, index: int, log: dict) -> str:
        """Deterministic UUID so re-ingesting the same batch doesn't duplicate points."""
        key = f"{batch_id}:{index}:{log.get('timestamp', '')}:{log.get('container', '')}"
        digest = hashlib.sha256(key.encode()).hexdigest()
        return str(uuid.UUID(hex=digest[:32]))
