"""Backend selection for `transcribe()`: prefer-whisperx falls back to Deepgram
when the whisper service host is unreachable, and records which engine ran."""
import socket

import httpx
import pytest

from podracer import transcribe as tr
from podracer.config import Config
from podracer.transcribe import TranscribeConfigError, TranscriptResult, transcribe

URL = "http://gpu-host:9000"


def _cfg(**kw) -> Config:
    base = dict(transcribe_backend="prefer-whisperx", deepgram_api_key="dg",
                transcribe_service_url=URL, transcribe_service_auth_token="tok",
                diarize=True)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def calls(monkeypatch):
    """Record which engine ran; whisper/health behaviour is set per test."""
    log: list[str] = []

    def fake_deepgram(audio_path, model, api_key, diarize):
        log.append("deepgram")
        return TranscriptResult("[00:00:00] [SPEAKER_00] dg", f"deepgram:{model}")

    monkeypatch.setattr(tr, "_transcribe_deepgram", fake_deepgram)
    monkeypatch.setattr(tr, "whisper_service_unavailable", lambda url, diarize: None)
    return log


def _whisper_returns(monkeypatch, log, body=None, exc=None, status=200):
    def fake_post(audio_path, service_url, headers, data, timeout_seconds):
        log.append("whisper")
        assert headers == {"Authorization": "Bearer tok"}
        if exc:
            raise exc
        return httpx.Response(status, json=body or {}, request=httpx.Request("POST", service_url))

    monkeypatch.setattr(tr, "_post_to_whisper_service", fake_post)


def test_healthy_service_is_used_and_labelled_with_its_model(monkeypatch, calls):
    _whisper_returns(monkeypatch, calls, body={"text": "hi", "model": "large-v3"})
    result = transcribe("a.mp3", _cfg(transcribe_whisperx_model="small"))
    assert calls == ["whisper"]
    # Label comes from the service response, not the client's config.
    assert result == TranscriptResult("hi", "whisperx:large-v3")


def test_unhealthy_service_goes_straight_to_deepgram(monkeypatch, calls):
    monkeypatch.setattr(tr, "whisper_service_unavailable", lambda url, diarize: "down")
    _whisper_returns(monkeypatch, calls, body={"text": "hi"})
    result = transcribe("a.mp3", _cfg())
    assert calls == ["deepgram"]
    assert result.source == "deepgram:nova-3"


@pytest.mark.parametrize("exc", [
    httpx.RemoteProtocolError("server disconnected"),
    httpx.ReadError("connection reset"),
    httpx.ConnectError("refused"),
])
def test_service_dying_mid_request_falls_back_without_retrying(monkeypatch, calls, exc):
    _whisper_returns(monkeypatch, calls, exc=exc)
    result = transcribe("a.mp3", _cfg())
    assert calls == ["whisper", "deepgram"]
    assert result.source == "deepgram:nova-3"


def test_service_500_falls_back(monkeypatch, calls):
    _whisper_returns(monkeypatch, calls, body={"detail": "oom"}, status=500)
    assert transcribe("a.mp3", _cfg()).source == "deepgram:nova-3"
    assert calls == ["whisper", "deepgram"]


def test_rejected_request_falls_back_and_reports(monkeypatch, calls):
    reported = []
    monkeypatch.setattr(tr.sentry_sdk, "capture_message", lambda msg, level: reported.append(msg))
    _whisper_returns(monkeypatch, calls, body={"detail": "invalid_token"}, status=403)
    assert transcribe("a.mp3", _cfg()).source == "deepgram:nova-3"
    assert len(reported) == 1 and "403" in reported[0]


def test_strict_whisperx_backend_does_not_fall_back(monkeypatch, calls):
    _whisper_returns(monkeypatch, calls, exc=httpx.ConnectError("refused"))
    # Skip the retry backoff sleeps; still fails rather than falling back.
    monkeypatch.setattr(tr, "_post_with_retries", tr._post_to_whisper_service)
    with pytest.raises(httpx.ConnectError):
        transcribe("a.mp3", _cfg(transcribe_backend="whisperx-http"))
    assert calls == ["whisper"]


@pytest.mark.parametrize("kw, missing", [
    ({"deepgram_api_key": None}, "DEEPGRAM_API_KEY"),
    ({"transcribe_service_url": None}, "service_url"),
    ({"transcribe_backend": "nope"}, "unknown transcribe backend"),
])
def test_config_errors(kw, missing):
    with pytest.raises(TranscribeConfigError, match=missing):
        transcribe("a.mp3", _cfg(**kw))


# --- health probe ---------------------------------------------------------

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_probe_unreachable_host():
    reason = tr.whisper_service_unavailable(f"http://127.0.0.1:{_closed_port()}", diarize=False)
    assert reason and "health check failed" in reason


@pytest.mark.parametrize("health, diarize, ok", [
    ({"status": "ok", "diarize_available": True}, True, True),
    ({"status": "ok", "diarize_available": False}, False, True),
    ({"status": "ok", "diarize_available": False}, True, False),
    ({"status": "loading"}, False, False),
])
def test_health_probe_reads_status(monkeypatch, health, diarize, ok):
    monkeypatch.setattr(tr.httpx, "get", lambda url, timeout: httpx.Response(
        200, json=health, request=httpx.Request("GET", url)))
    assert (tr.whisper_service_unavailable(URL, diarize) is None) is ok
