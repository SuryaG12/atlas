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
    ATLAS_MAX_TOKENS   generation cap, default 512 (0 disables the cap)
    ATLAS_KEEP_ALIVE   how long Ollama keeps the model loaded, default 2h
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# Speed defaults. A local 7B model on CPU generates at roughly 2 tok/s, so an
# uncapped answer can run for many minutes. 512 tokens is far more than a cited
# answer needs and stops a runaway generation. keep_alive stops Ollama from
# evicting the model between questions, which otherwise costs a full reload
# (tens of seconds) on the first ask after an idle period.
DEFAULT_MAX_TOKENS = 512
DEFAULT_KEEP_ALIVE = "2h"


def _max_tokens() -> int | None:
    raw = os.environ.get("ATLAS_MAX_TOKENS", "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else None


def _keep_alive() -> str:
    return os.environ.get("ATLAS_KEEP_ALIVE", DEFAULT_KEEP_ALIVE).strip() or DEFAULT_KEEP_ALIVE

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


def _post_stream(
    url: str, payload: dict, timeout: float, headers: dict | None = None
):
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    return urllib.request.urlopen(request, timeout=timeout)


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
    max_tokens = _max_tokens()

    try:
        if info.provider == "ollama":
            options: dict = {"temperature": 0.1}
            if max_tokens:
                options["num_predict"] = max_tokens
            data = _post_json(
                info.endpoint.rstrip("/") + "/api/chat",
                {
                    "model": info.model,
                    "messages": messages,
                    "stream": False,
                    "options": options,
                    "keep_alive": _keep_alive(),
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
            body = {"model": info.model, "messages": messages, "temperature": 0.1}
            if max_tokens:
                body["max_tokens"] = max_tokens
            data = _post_json(
                _chat_endpoint(info.endpoint),
                body,
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


def stream(
    system: str, prompt: str, info: ModelInfo, timeout: float | None = None
) -> Iterator[str]:
    """Yield answer deltas as the local model produces them.

    The transport is the same as ``complete``: Ollama NDJSON or OpenAI-compatible
    SSE. Errors surface as ``LLMUnavailable`` just like a non-streamed call, so
    the web layer can report them without leaking provider details.
    """
    request_timeout = _timeout(300.0) if timeout is None else timeout
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    max_tokens = _max_tokens()
    produced = False

    try:
        if info.provider == "ollama":
            options: dict = {"temperature": 0.1}
            if max_tokens:
                options["num_predict"] = max_tokens
            response = _post_stream(
                info.endpoint.rstrip("/") + "/api/chat",
                {
                    "model": info.model,
                    "messages": messages,
                    "stream": True,
                    "options": options,
                    "keep_alive": _keep_alive(),
                },
                request_timeout,
            )
            with response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise LLMUnavailable(str(data["error"]))
                    content = (data.get("message") or {}).get("content") or ""
                    if content:
                        produced = True
                        yield content
                    if data.get("done"):
                        break
        else:
            headers = {}
            api_key = os.environ.get("ATLAS_LLM_API_KEY", "").strip()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            body = {
                "model": info.model,
                "messages": messages,
                "temperature": 0.1,
                "stream": True,
            }
            if max_tokens:
                body["max_tokens"] = max_tokens
            response = _post_stream(
                _chat_endpoint(info.endpoint),
                body,
                request_timeout,
                headers,
            )
            with response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        break
                    data = json.loads(data_text)
                    choices = data.get("choices") or []
                    delta = ((choices[0] or {}).get("delta") or {}) if choices else {}
                    content = delta.get("content")
                    if content:
                        produced = True
                        yield content
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

    if not produced:
        raise LLMUnavailable(f"{info.provider} returned an empty response")

