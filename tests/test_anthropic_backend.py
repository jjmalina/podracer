"""The Anthropic backend: request shape (schema-enforced JSON, effort), usage
logging, refusal handling, and the config/CLI plumbing. Mocks the SDK client
at ``summarize._anthropic_client`` so it runs offline."""
import json
from types import SimpleNamespace

import pytest

from podracer import config as config_mod
from podracer import summarize
from podracer.config import load_config
from podracer.models import ChapterList
from podracer.process import _build_summarize_backend
from podracer.summarize import Backend, _anthropic_schema, _chat, _chat_anthropic, validate_effort

from .test_llm_token_logging import _capture_json_logs, _llm_call


def _response(text: str, *, stop_reason: str = "end_turn", cache_read: int = 0, stop_details=None):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=250,
                            cache_read_input_tokens=cache_read, cache_creation_input_tokens=0)
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop_reason, stop_details=stop_details)


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _install(monkeypatch, response):
    messages = _FakeMessages(response)
    monkeypatch.setattr(summarize, "_anthropic_client",
                        lambda api_key: SimpleNamespace(messages=messages))
    return messages


def test_request_shape_enforces_schema_and_effort(monkeypatch):
    messages = _install(monkeypatch, _response('{"summary": "ok."}'))
    backend = Backend.anthropic("claude-haiku-5-5", api_key="k", effort="medium")
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    result = _chat_anthropic(backend, "sys", "user text", schema)

    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": [{"type": "text", "text": "user text"}]}]
    assert call["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema",
                   "schema": {**schema, "additionalProperties": False}},
    }
    assert json.loads(result.content) == {"summary": "ok."}
    assert result.finish_reason == "end_turn"
    assert result.provider == "anthropic"


def test_pydantic_schemas_are_closed_for_the_structured_output_grammar():
    # The API rejects any object schema without additionalProperties: false,
    # including the nested ones pydantic puts under $defs. Nothing else changes.
    schema = ChapterList.model_json_schema()
    closed = _anthropic_schema(schema)
    assert closed["additionalProperties"] is False
    assert closed["$defs"]["Chapter"]["additionalProperties"] is False
    assert closed["$defs"]["Chapter"]["required"] == schema["$defs"]["Chapter"]["required"]
    assert "additionalProperties" not in schema  # input untouched


def test_prebuilt_content_blocks_pass_through(monkeypatch):
    # The eval judge sends the transcript as its own block with cache_control.
    messages = _install(monkeypatch, _response("{}"))
    backend = Backend.anthropic("claude-opus-5-5", api_key="k")
    blocks = [{"type": "text", "text": "big", "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": "task"}]
    _chat(backend, "sys", blocks, {"type": "object"})
    assert messages.calls[0]["messages"][0]["content"] == blocks


def test_blocks_are_flattened_for_other_backends(monkeypatch):
    seen = {}

    def fake_openrouter(backend, system, user, schema, repair=False, ignore_providers=None):
        seen["user"] = user
        return summarize.ChatResult(content="{}")

    monkeypatch.setattr(summarize, "_chat_openrouter", fake_openrouter)
    backend = Backend.openrouter("m", api_key="x")
    _chat(backend, "sys", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], {})
    assert seen["user"] == "a\n\nb"


def test_logs_token_usage_including_cache_reads(monkeypatch):
    _install(monkeypatch, _response("{}", cache_read=600))
    backend = Backend.anthropic("claude-haiku-5-5", api_key="k")
    lines = _capture_json_logs(monkeypatch, lambda: _chat_anthropic(backend, "s", "u", {"type": "object"}))
    rec = _llm_call(lines)
    assert rec["backend"] == "anthropic"
    assert rec["model"] == "claude-haiku-5-5"
    # input_tokens means prompt size, as on the OpenAI-compatible backends:
    # the SDK's input_tokens excludes cached tokens, so they're folded back in.
    assert rec["input_tokens"] == 1600 and isinstance(rec["input_tokens"], int)
    assert rec["output_tokens"] == 250
    assert rec["total_tokens"] == 1850
    assert rec["finish_reason"] == "end_turn"


def test_refusal_yields_empty_content_so_the_guards_retry(monkeypatch):
    details = SimpleNamespace(category="general_harms", explanation="nope")
    _install(monkeypatch, _response("", stop_reason="refusal", stop_details=details))
    backend = Backend.anthropic("claude-haiku-5-5", api_key="k")
    lines = _capture_json_logs(monkeypatch, lambda: _chat_anthropic(backend, "s", "u", {"type": "object"}))
    refusal = next(r for r in lines if r.get("event") == "llm_refusal")
    assert refusal["category"] == "general_harms"
    # Empty content is invalid JSON, so _chat_checked treats it as degenerate.
    result = _chat_anthropic(backend, "s", "u", {"type": "object"})
    assert result.finish_reason == "refusal"
    with pytest.raises(Exception):
        json.loads(result.content)


def test_effort_is_validated_at_the_boundary():
    assert validate_effort("xhigh") == "xhigh"
    with pytest.raises(ValueError, match="effort"):
        Backend.anthropic("m", api_key="k", effort="turbo")
    with pytest.raises(ValueError, match="effort"):
        validate_effort(None)


def test_key_and_effort_flow_from_toml_to_backend(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[summarize]\nbackend = "anthropic"\nmodel = "claude-haiku-5-5"\nanthropic_effort = "medium"\n'
        '[keys]\nanthropic_api_key = "from-toml"\n',
    )
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = load_config()
    backend = _build_summarize_backend(cfg, None, None)
    assert backend.name == "anthropic"
    assert backend.model == "claude-haiku-5-5"
    assert backend.api_key == "from-toml"
    assert backend.effort == "medium"


def test_env_key_overrides_toml(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[keys]\nanthropic_api_key = "from-toml"\n')
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    cfg = load_config()
    assert cfg.anthropic_api_key == "from-env"
    assert cfg.summarize_anthropic_effort == "low"


def test_bad_effort_in_toml_fails_at_load(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[summarize]\nanthropic_effort = "max!"\n')
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    with pytest.raises(ValueError, match="anthropic_effort"):
        load_config()


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[summarize]\nbackend = "anthropic"\n')
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = load_config()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _build_summarize_backend(cfg, None, None)


def test_openrouter_effort_turns_reasoning_on(monkeypatch):
    # Hosted Claude Opus/Sonnet 5.5 reject reasoning "none"; an explicit effort
    # (e.g. the eval judge) is sent through, the pipeline default stays off.
    seen: list[dict] = []

    class _Resp:
        status_code = 200
        is_success = True

        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {}, "provider": "Anthropic"}

        def raise_for_status(self):
            pass

    def post(url, json, headers, timeout):
        seen.append(json)
        return _Resp()

    monkeypatch.setattr(summarize.httpx, "post", post)
    summarize._chat_openrouter(Backend.openrouter("anthropic/claude-opus-5.5", api_key="k", effort="medium"),
                               "s", "u", {"type": "object"})
    summarize._chat_openrouter(Backend.openrouter("deepseek/deepseek-v4-flash", api_key="k"),
                               "s", "u", {"type": "object"})
    assert seen[0]["reasoning"] == {"effort": "medium"}
    assert seen[1]["reasoning"] == {"effort": "none"}
    with pytest.raises(ValueError, match="effort"):
        Backend.openrouter("m", api_key="k", effort="none")
