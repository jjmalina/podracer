"""Verify LLM token usage is logged as a structured `llm_call` event with
numeric fields, for each backend. Mocks the HTTP layer with realistic response
shapes so it runs offline and asserts the exact aggregation-relevant fields."""
import io
import json
import sys

import structlog

from podracer import logging_config, summarize
from podracer.summarize import (
    Backend,
    Chapter,
    _chat_ollama,
    _chat_openrouter,
    _chat_vllm,
    enrich_chapters,
)

# OpenAI-compatible response (OpenRouter + vLLM)
OPENAI_PAYLOAD = {
    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250},
}
# Ollama uses different field names
OLLAMA_PAYLOAD = {"message": {"content": "{}"}, "prompt_eval_count": 800, "eval_count": 200}


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _capture_json_logs(monkeypatch, fn):
    monkeypatch.setenv("PODRACER_LOG_FORMAT", "json")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    monkeypatch.setattr(logging_config, "_configured_format", None)
    structlog.reset_defaults()
    logging_config.configure_logging()
    fn()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _llm_call(lines):
    return next(r for r in lines if r.get("event") == "llm_call")


def test_openrouter_logs_token_usage(monkeypatch):
    monkeypatch.setattr("podracer.summarize.httpx.post", lambda *a, **k: _FakeResp(OPENAI_PAYLOAD))
    backend = Backend.openrouter("deepseek/deepseek-v4-flash", api_key="x")
    lines = _capture_json_logs(monkeypatch, lambda: _chat_openrouter(backend, "s", "u", {"type": "object"}))
    rec = _llm_call(lines)
    assert rec["backend"] == "openrouter"
    assert rec["model"] == "deepseek/deepseek-v4-flash"
    assert rec["input_tokens"] == 1000 and isinstance(rec["input_tokens"], int)
    assert rec["output_tokens"] == 250 and isinstance(rec["output_tokens"], int)
    assert rec["total_tokens"] == 1250


def test_vllm_logs_token_usage(monkeypatch):
    monkeypatch.setattr("podracer.summarize.httpx.post", lambda *a, **k: _FakeResp(OPENAI_PAYLOAD))
    backend = Backend.vllm("my-model")
    lines = _capture_json_logs(monkeypatch, lambda: _chat_vllm(backend, "s", "u", {"type": "object"}))
    rec = _llm_call(lines)
    assert rec["backend"] == "vllm"
    assert rec["input_tokens"] == 1000 and isinstance(rec["input_tokens"], int)
    assert rec["output_tokens"] == 250


def test_ollama_logs_token_usage(monkeypatch):
    monkeypatch.setattr("podracer.summarize.httpx.post", lambda *a, **k: _FakeResp(OLLAMA_PAYLOAD))
    backend = Backend.ollama("llama3")
    lines = _capture_json_logs(monkeypatch, lambda: _chat_ollama(backend, "s", "u", {"type": "object"}))
    rec = _llm_call(lines)
    assert rec["backend"] == "ollama"
    assert rec["input_tokens"] == 800 and isinstance(rec["input_tokens"], int)
    assert rec["output_tokens"] == 200
    assert rec["total_tokens"] == 1000  # computed input + output


def test_chapter_pool_preserves_bound_context(monkeypatch):
    """Token events from the chapter-detail ThreadPoolExecutor must still carry
    contextvars (episode_id) bound on the calling thread — contextvars don't
    propagate to pool threads automatically, so enrich_chapters re-binds them."""
    seen: dict = {}

    def fake_enrich(backend, speaker_key, chapter, slice_text):
        seen.update(structlog.contextvars.get_contextvars())  # captured inside a pool thread
        return "enriched"

    monkeypatch.setattr(summarize, "_enrich_one_chapter", fake_enrich)
    monkeypatch.setattr(summarize, "_is_teaser_chapter", lambda c: False)
    monkeypatch.setattr(summarize, "_slice_transcript_by_chapter", lambda *a: "segment text")
    monkeypatch.setattr(summarize, "format_speaker_key", lambda speakers: "")

    chapters = [Chapter(title="t", timestamp="00:00:00", summary="short")]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(episode_id=42, job_id=7)
    try:
        enrich_chapters(chapters, "transcript", [], backend=Backend.openrouter("m", api_key="x"))
    finally:
        structlog.contextvars.clear_contextvars()

    assert seen.get("episode_id") == 42
    assert seen.get("job_id") == 7
