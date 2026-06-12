"""Tests for the LLM output quality guards: content validators, the bounded
retry on degenerate output, the no-downgrade chapter fallback, and the
OpenRouter provider constraint.

See docs/plans/2026-06-12-llm-output-quality-guards.md. The detector/retry is
the whole fix, so these mock the chat layer (``summarize._chat``) to feed it the
exact degenerate responses observed in production (the 14-token stub, the empty
highlights list, prose-instead-of-JSON) and assert it retries and recovers."""
import json

import pytest

from podracer import summarize
from podracer.summarize import (
    Backend,
    Chapter,
    ChapterList,
    DegenerateOutputError,
    Highlight,
    HighlightList,
    SpeakerIdentifications,
    Summary,
    _chat_checked,
    _check_chapters,
    _check_highlights,
    _check_summary,
    _checked_or_fail,
    _ends_terminally,
    _enrich_one_chapter,
)

BACKEND = Backend.openrouter("deepseek/deepseek-v4-flash", api_key="x")
SUBSTANTIAL_SLICE = "spoken line. " * 600  # > _SUBSTANTIAL_SLICE_CHARS
GOOD_DETAIL = "This chapter walks through the argument in real depth. " * 12  # > 400 chars, ends terminally


def _good_highlight(i: int) -> Highlight:
    return Highlight(
        text=f"A complete, substantive highlight number {i} that a listener would remember.",
        timestamp=f"00:0{i % 10}:00", speaker="A", kind="takeaway",
    )


def _replies(*results):
    """Monkeypatchable _chat that yields the given ChatResults in order."""
    it = iter(results)
    return lambda *a, **k: next(it)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(summarize.time, "sleep", lambda *a, **k: None)


# --- validators ------------------------------------------------------------

def test_ends_terminally():
    assert _ends_terminally("A finished sentence.")
    assert _ends_terminally('He said "done"')
    assert _ends_terminally("Wrapped up (mostly)")
    assert _ends_terminally("Trailing whitespace is fine.   ")
    assert not _ends_terminally("Kelsey Hightower articulates a ")  # the production stub
    assert not _ends_terminally("On GenAI:")
    assert not _ends_terminally("")


def test_check_summary_flags_stub():
    with pytest.raises(DegenerateOutputError):
        _check_summary(Summary(summary="Kelsey Hightower articulates a "))
    # A real multi-paragraph summary passes.
    _check_summary(Summary(summary="A thorough summary. " * 20))


def test_check_chapters_needs_multiple_spanning_chapters():
    with pytest.raises(DegenerateOutputError):
        _check_chapters(ChapterList(chapters=[Chapter(title="x", timestamp="00:00:00", summary="s")]))
    with pytest.raises(DegenerateOutputError):  # all share one timestamp
        _check_chapters(ChapterList(chapters=[
            Chapter(title="a", timestamp="00:00:00", summary="s"),
            Chapter(title="b", timestamp="00:00:00", summary="s"),
        ]))
    _check_chapters(ChapterList(chapters=[
        Chapter(title="a", timestamp="00:00:00", summary="s"),
        Chapter(title="b", timestamp="00:10:00", summary="s"),
    ]))


def test_check_highlights_empty_is_degenerate():
    # episode 109241: an 8-token completion returned {"highlights": []}.
    with pytest.raises(DegenerateOutputError):
        _check_highlights(HighlightList(highlights=[]))


def test_check_highlights_drops_a_few_stubs_but_keeps_the_list():
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(10)] + [
        Highlight(text="On GenAI: ", timestamp="00:01:00", speaker="A", kind="takeaway"),
    ])
    _check_highlights(hl)  # 1 of 11 bad (<20%) → passes
    assert len(hl.highlights) == 10  # the stub was dropped in place
    assert all(h.text != "On GenAI: " for h in hl.highlights)


def test_check_highlights_retries_when_mostly_degenerate():
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(6)] + [
        Highlight(text="On X: ", timestamp="00:01:00", speaker="A", kind="takeaway") for _ in range(6)
    ])
    with pytest.raises(DegenerateOutputError):  # 6 of 12 bad (>20%)
        _check_highlights(hl)


# --- retry path ------------------------------------------------------------

def test_retries_on_prose_then_parses(monkeypatch):
    speakers_json = json.dumps({"speakers": [
        {"label": "SPEAKER_00", "name": "Ana", "role": "host",
         "evidence_timestamp": "00:00:01", "evidence_quote": "I'm Ana"},
    ]})
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content="Sure! Here are the speakers: ..."),  # prose, not JSON
        summarize.ChatResult(content=speakers_json),
    ))
    model, passed = _chat_checked(BACKEND, "s", "u", SpeakerIdentifications, summarize._check_speakers)
    assert passed
    assert [s.name for s in model.speakers] == ["Ana"]


def test_valid_content_with_length_finish_reason_is_accepted(monkeypatch):
    # finish_reason == "length" alone is not a retry trigger: a complete, long
    # answer that merely hit the cap passes the content check. Truncation that
    # actually cuts a sentence fails _ends_terminally and is retried instead.
    calls = {"n": 0}

    def chat(*a, **k):
        calls["n"] += 1
        return summarize.ChatResult(content=json.dumps({"summary": "A complete summary. " * 20}),
                                    finish_reason="length")

    monkeypatch.setattr(summarize, "_chat", chat)
    model, passed = _chat_checked(BACKEND, "s", "u", Summary, _check_summary)
    assert passed and calls["n"] == 1  # accepted on the first call, no wasteful retry


def test_checked_or_fail_raises_when_nothing_parses(monkeypatch):
    # Three prose responses in a row → nothing to store → fail the job so the
    # worker retries the whole episode rather than persisting garbage.
    monkeypatch.setattr(summarize, "_chat", lambda *a, **k: summarize.ChatResult(content="not json"))
    with pytest.raises(DegenerateOutputError):
        _checked_or_fail(Summary, BACKEND, "s", "u", _check_summary)


def test_checked_or_fail_accepts_best_effort_on_exhaustion(monkeypatch):
    # Every attempt returns a short-but-parseable highlights list. After retries
    # we accept the best effort (filtered) rather than failing the whole job.
    short = json.dumps({"highlights": [
        {"text": _good_highlight(0).text, "timestamp": "00:00:00", "speaker": "A", "kind": "takeaway"},
    ]})
    monkeypatch.setattr(summarize, "_chat", lambda *a, **k: summarize.ChatResult(content=short))
    model = _checked_or_fail(HighlightList, BACKEND, "s", "u", _check_highlights)
    assert len(model.highlights) == 1  # accepted, not raised


# --- chapter enrichment: retry + no-downgrade fallback ---------------------

def test_chapter_retries_on_stub_then_succeeds(monkeypatch):
    # The exact production failure: a 14-token stub, then a clean retry.
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content=json.dumps({"summary": "Kelsey Hightower articulates a "})),
        summarize.ChatResult(content=json.dumps({"summary": GOOD_DETAIL})),
    ))
    ch = Chapter(title="A People-First View of GenAI", timestamp="02:14:33", summary="short chapters-pass summary")
    out = _enrich_one_chapter(BACKEND, "KEY", ch, SUBSTANTIAL_SLICE)
    assert out.strip() == GOOD_DETAIL.strip()


def test_chapter_fallback_never_downgrades(monkeypatch):
    # Every attempt is a stub shorter than the existing summary → keep the
    # chapters-pass summary (the original behavior), never the stub.
    monkeypatch.setattr(summarize, "_chat",
                        lambda *a, **k: summarize.ChatResult(content=json.dumps({"summary": "stub "})))
    original = "the original chapters-pass summary, a legitimate one to two sentences."
    ch = Chapter(title="X", timestamp="00:05:00", summary=original)
    out = _enrich_one_chapter(BACKEND, "KEY", ch, SUBSTANTIAL_SLICE)
    assert out == original


def test_thin_summary_for_substantial_chapter_is_retried(monkeypatch):
    # A 385-char "thin but valid" detail (episode 109241 chapter 6) on a
    # substantial slice is degenerate; a fuller retry replaces it.
    thin = "The host asks whether a drawdown is coming. " * 2  # < 400 chars, ends terminally
    assert len(thin) < summarize._MIN_CHAPTER_DETAIL_CHARS
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content=json.dumps({"summary": thin})),
        summarize.ChatResult(content=json.dumps({"summary": GOOD_DETAIL})),
    ))
    ch = Chapter(title="Are We Headed for a Major Drawdown?", timestamp="00:05:00", summary="s")
    out = _enrich_one_chapter(BACKEND, "KEY", ch, SUBSTANTIAL_SLICE)
    assert out.strip() == GOOD_DETAIL.strip()


def test_short_detail_for_thin_chapter_is_allowed(monkeypatch):
    # The prompt explicitly allows a 1-2 sentence summary for a thin chapter.
    # With a small slice, a short-but-complete detail must NOT be flagged.
    short_ok = "They exchange brief greetings and introduce the topic."
    monkeypatch.setattr(summarize, "_chat",
                        lambda *a, **k: summarize.ChatResult(content=json.dumps({"summary": short_ok})))
    ch = Chapter(title="Intro", timestamp="00:00:00", summary="s")
    out = _enrich_one_chapter(BACKEND, "KEY", ch, "[00:00:01] [Host] hi there")  # tiny slice
    assert out == short_ok


# --- provider constraint ---------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.is_success = True

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_openrouter_constrains_provider_to_structured_output(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                          "usage": {}, "provider": "DeepInfra"})

    monkeypatch.setattr(summarize.httpx, "post", fake_post)
    summarize._chat_openrouter(BACKEND, "s", "u", {"type": "object"})
    assert captured["payload"]["provider"] == {"require_parameters": True}
