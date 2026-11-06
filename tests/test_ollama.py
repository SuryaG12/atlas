"""Ollama transport: detection and configuration. Skips cleanly without Ollama."""

from __future__ import annotations

import json
import urllib.request

import pytest

from atlas.narrate import DEFAULT_OLLAMA, ModelInfo, detect_model


def _live_tags() -> dict | None:
    try:
        with urllib.request.urlopen(
            DEFAULT_OLLAMA + "/api/tags", timeout=1.0
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


LIVE_TAGS = _live_tags()
LIVE_MODELS = [
    entry["name"]
    for entry in (LIVE_TAGS or {}).get("models", [])
    if isinstance(entry, dict) and entry.get("name")
]


def test_detect_model_returns_none_when_ollama_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_LLM_URL", raising=False)
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")

    assert detect_model(timeout=0.2) is None


def test_detect_model_prefers_an_explicit_openai_compatible_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_LLM_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("ATLAS_LLM_MODEL", "local-model")

    info = detect_model(timeout=0.2)

    assert info == ModelInfo(
        "openai-compatible", "local-model", "http://127.0.0.1:1234/v1"
    )


def test_detect_model_honours_the_ollama_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_LLM_URL", raising=False)
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")

    assert detect_model(timeout=0.2) is None


@pytest.mark.skipif(LIVE_TAGS is None, reason="Ollama is not running")
def test_live_ollama_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LIVE_MODELS:
        pytest.skip("Ollama is running but no model is pulled")
    monkeypatch.delenv("ATLAS_LLM_URL", raising=False)
    monkeypatch.delenv("ATLAS_OLLAMA_URL", raising=False)
    monkeypatch.delenv("ATLAS_MODEL", raising=False)

    info = detect_model(timeout=2.0)

    assert info is not None
    assert info.provider == "ollama"
    assert info.endpoint == DEFAULT_OLLAMA
    assert info.model in LIVE_MODELS


@pytest.mark.skipif(not LIVE_MODELS, reason="no Ollama model available")
def test_live_ollama_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_LLM_URL", raising=False)
    monkeypatch.delenv("ATLAS_OLLAMA_URL", raising=False)
    monkeypatch.setenv("ATLAS_MODEL", LIVE_MODELS[0])

    info = detect_model(timeout=2.0)

    assert info is not None
    assert info.model == LIVE_MODELS[0]


def test_complete_sends_generation_cap_and_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas import narrate

    captured: dict = {}

    def fake_post(url, payload, timeout, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        return {"message": {"content": "  cited answer  "}}

    monkeypatch.delenv("ATLAS_MAX_TOKENS", raising=False)
    monkeypatch.delenv("ATLAS_KEEP_ALIVE", raising=False)
    monkeypatch.setattr(narrate, "_post_json", fake_post)
    info = ModelInfo("ollama", "test-model", "http://127.0.0.1:11434")

    assert narrate.complete("system", "prompt", info) == "cited answer"
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["options"]["num_predict"] == 512
    assert captured["payload"]["keep_alive"] == "2h"


def test_complete_can_disable_the_generation_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas import narrate

    captured: dict = {}

    def fake_post(url, payload, timeout, headers=None):
        captured.update(payload)
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("ATLAS_MAX_TOKENS", "0")
    monkeypatch.setattr(narrate, "_post_json", fake_post)
    info = ModelInfo("ollama", "test-model", "http://127.0.0.1:11434")

    narrate.complete("system", "prompt", info)

    assert "num_predict" not in captured["options"]


class _FakeStreamResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def __iter__(self):
        return iter(self._lines)


def test_stream_yields_ollama_deltas_with_the_same_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas import narrate

    captured: dict = {}

    def fake_stream(url, payload, timeout, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        return _FakeStreamResponse(
            [
                b'{"message":{"content":"hello "},"done":false}\n',
                b'{"message":{"content":"world"},"done":true}\n',
            ]
        )

    monkeypatch.delenv("ATLAS_MAX_TOKENS", raising=False)
    monkeypatch.setattr(narrate, "_post_stream", fake_stream)
    info = ModelInfo("ollama", "test-model", "http://127.0.0.1:11434")

    assert list(narrate.stream("system", "prompt", info)) == ["hello ", "world"]
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["options"]["num_predict"] == 512
