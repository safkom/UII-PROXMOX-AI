import json
from typing import Any

import requests

from backend.config.settings import Settings


class OllamaClient:
    """Minimal client for Ollama generate API."""

    def __init__(self, settings: Settings):
        self.base_url = settings.ollama_url
        self.model = settings.ollama_model
        self.session = requests.Session()

    def generate_json(self, prompt: str, system_prompt: str = "") -> dict[str, Any]:
        """Generate structured JSON output from Ollama."""
        try_models = [self.model]

        available_models = self.list_models()
        if self.model not in available_models and available_models:
            try_models.append(available_models[0])

        data = None
        last_error: Exception | None = None
        for model in try_models:
            try:
                data = self._generate_with_model(model, prompt, system_prompt)
                break
            except requests.RequestException as exc:
                last_error = exc
                continue

        if data is None:
            if last_error:
                raise last_error
            raise RuntimeError("Ollama generation failed with no model response")

        raw_response = data.get("response", "{}")
        try:
            parsed = json.loads(raw_response)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        return {
            "summary": raw_response.strip() or "No response from model.",
            "reasoning": "Model did not return valid JSON; using raw output fallback.",
            "suggested_actions": [],
            "confidence": 0.3,
        }

    def list_models(self) -> list[str]:
        """Return installed Ollama model names."""
        url = f"{self.base_url}/api/tags"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException:
            return []

        models = data.get("models", [])
        names = []
        for item in models:
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    def _generate_with_model(self, model: str, prompt: str, system_prompt: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,
            },
        }

        response = self.session.post(url, json=payload, timeout=90)
        response.raise_for_status()
        return response.json()

    def generate_stream(self, prompt: str, system_prompt: str = ""):
        """Stream raw lines from Ollama generate API (server streaming).

        Yields decoded text chunks from the model as they arrive.
        """
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": True,
            "format": "json",
            "options": {"temperature": 0.2},
        }
        try:
            resp = self.session.post(url, json=payload, stream=True, timeout=(15, None))
            resp.raise_for_status()
        except requests.RequestException:
            # On failure to start streaming, re-raise to caller
            raise

        # Iterate over chunked lines
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                continue
            # yield the raw line so caller can decide how to display/parse
            yield line + "\n"
