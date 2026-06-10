"""Semantic text embeddings via the Ollama /api/embed endpoint."""
import logging

import requests

from backend.config.settings import Settings

logger = logging.getLogger(__name__)

# Embedding dimension per (url, model), probed once per process so read-heavy
# request paths don't pay an extra embed call.
_DIMENSION_CACHE: dict[tuple[str, str], int] = {}

# Keep batches small enough that CPU-only embedding stays well under the timeout.
_BATCH_SIZE = 64


class OllamaEmbeddings:
    """Generates embeddings with a local Ollama embedding model (e.g. nomic-embed-text)."""

    def __init__(self, settings: Settings):
        self.base_url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_embed_model
        self.session = requests.Session()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url}/api/embed"
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            try:
                resp = self.session.post(url, json={"model": self.model, "input": batch}, timeout=120)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Embedding request failed ({exc}). "
                    f"Make sure the embedding model is installed: ollama pull {self.model}"
                ) from exc
            batch_embeddings = resp.json().get("embeddings", [])
            if len(batch_embeddings) != len(batch):
                raise RuntimeError("Ollama returned an unexpected number of embeddings")
            embeddings.extend(batch_embeddings)
        if embeddings:
            _DIMENSION_CACHE[(self.base_url, self.model)] = len(embeddings[0])
        return embeddings

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def dimension(self) -> int:
        """Vector size of the configured model (probed once and cached per process)."""
        key = (self.base_url, self.model)
        if key not in _DIMENSION_CACHE:
            self.embed_one("dimension probe")
        return _DIMENSION_CACHE[key]
