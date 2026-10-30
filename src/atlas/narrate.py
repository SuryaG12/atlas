"""Local LLM narration.

Two transports, both local by default:

* Ollama            - http://127.0.0.1:11434
* OpenAI-compatible - anything that speaks /v1/chat/completions

Configuration (all optional):

    ATLAS_LLM_URL      base URL or full /chat/completions endpoint
    ATLAS_LLM_MODEL    model name for the OpenAI-compatible transport
    ATLAS_LLM_API_KEY  bearer token, if the local server wants one
    ATLAS_OLLAMA_URL   override the Ollama base URL
    ATLAS_MODEL        model name for Ollama (defaults to first installed)
    ATLAS_LLM_TIMEOUT  seconds, default 300
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

NO_MODEL_MESSAGE = (
    "No local model configured. Start Ollama on http://127.0.0.1:11434 "
    "or set ATLAS_LLM_URL to an OpenAI-compatible server, then retry. "
    "Use --no-llm to inspect retrieval without a model."
)


class LLMUnavailable(RuntimeError):
    """No local model is configured or reachable."""


@dataclass(slots=True)
class ModelInfo:
    provider: str
    model: str
    endpoint: str

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
        }


def _timeout(default: float) -> float:
    try:
        return float(os.environ.get("ATLAS_LLM_TIMEOUT", default))
    except ValueError:
        return default


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def detect_model(timeout: float | None = None) -> ModelInfo | None:
    """Return a reachable local model, or None. Never raises."""
    probe_timeout = _timeout(1.0) if timeout is None else timeout

    url = os.environ.get("ATLAS_LLM_URL", "").strip()
    if url:
        model = os.environ.get("ATLAS_LLM_MODEL", "").strip() or "local"
        return ModelInfo("openai-compatible", model, url)

    base = os.environ.get("ATLAS_OLLAMA_URL", DEFAULT_OLLAMA).rstrip("/")
    try:
        data = _get_json(base + "/api/tags", probe_timeout)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None

    models = [
        entry.get("name")
        for entry in data.get("models", [])
        if isinstance(entry, dict) and entry.get("name")
    ]
    if not models:
        return None
    model = os.environ.get("ATLAS_MODEL", "").strip() or models[0]
    return ModelInfo("ollama", model, base)


def _chat_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def complete(system: str, prompt: str, info: ModelInfo, timeout: float | None = None) -> str:
    """Generate narration. Raises LLMUnavailable with a readable message."""
    request_timeout = _timeout(300.0) if timeout is None else timeout
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    try:
        if info.provider == "ollama":
            data = _post_json(
                info.endpoint.rstrip("/") + "/api/chat",
                {
                    "model": info.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.1},
                },
                request_timeout,
            )
            message = data.get("message") or {}
            content = message.get("content")
        else:
            headers = {}
            api_key = os.environ.get("ATLAS_LLM_API_KEY", "").strip()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            data = _post_json(
                _chat_endpoint(info.endpoint),
                {"model": info.model, "messages": messages, "temperature": 0.1},
                request_timeout,
                headers,
            )
            choices = data.get("choices") or []
            content = (
                ((choices[0] or {}).get("message") or {}).get("content") if choices else None
            )
    except urllib.error.HTTPError as exc:
        raise LLMUnavailable(
            f"{info.provider} returned HTTP {exc.code} at {info.endpoint}"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LLMUnavailable(
            f"could not reach {info.provider} at {info.endpoint}: {exc}"
        ) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMUnavailable(f"{info.provider} returned invalid JSON: {exc}") from exc

    if not content or not str(content).strip():
        raise LLMUnavailable(f"{info.provider} returned an empty response")
    return str(content).strip()
