"""Tests for the LLM output quality guards: content validators, the bounded
retry on degenerate output, the no-downgrade chapter fallback, and the
OpenRouter provider constraint.

See docs/plans/2026-06-12-llm-output-quality-guards.md. The detector/retry is
the whole fix, so these mock the chat layer (``summarize._chat``) to feed it the
exact degenerate responses observed in production (the 14-token stub, the empty
highlights list, prose-instead-of-JSON) and assert it retries and recovers."""
import io
import json
import sys

import pydantic
import pytest
import structlog

from podracer import logging_config, summarize
from podracer.models import Chapter, ChapterList, Highlight, HighlightList
from podracer.summarize import (
    Backend,
    DegenerateOutputError,
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
from podracer.timestamps import (
    format_timestamp,
    parse_timestamp,
    transcript_end_seconds,
    transcript_is_degenerate,
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


def test_parse_timestamp():
    assert parse_timestamp("00:04:38") == 278
    assert parse_timestamp("01:22:57") == 4977
    assert parse_timestamp("1:02:03") == 3723  # unpadded hours are tolerated
    assert parse_timestamp("01:01:56:00") is None  # four-field prod misfire
    assert parse_timestamp("04:15") is None
    assert parse_timestamp("00:99:00") is None
    assert parse_timestamp("soon") is None


def _chapters(*ts: str) -> ChapterList:
    return ChapterList(chapters=[
        Chapter(title=f"c{i}", timestamp=t, summary="s") for i, t in enumerate(ts)])


def test_check_chapters_rejects_malformed_timestamps():
    # Observed in prod: the chapters pass appended a spurious ":00" field.
    # With only one usable chapter left, the whole response is degenerate.
    with pytest.raises(DegenerateOutputError):
        _check_chapters(_chapters("00:00:00", "01:01:56:00"))


def test_check_chapters_rejects_timestamps_past_transcript_end():
    # The same misfire's sub-hour form, "MM:SS:00", parses as valid HH:MM:SS
    # but lands hours past the episode — only the transcript end exposes it.
    with pytest.raises(DegenerateOutputError):
        _check_chapters(_chapters("00:00:00", "04:15:00"), transcript_end=5185)
    # The identical list is fine in an actually-5-hour episode...
    _check_chapters(_chapters("00:00:00", "04:15:00"), transcript_end=5 * 3600)
    # ...and transcript_end=None disables the bound entirely.
    _check_chapters(_chapters("00:00:00", "04:15:00"), transcript_end=None)


def test_check_chapters_returns_filtered_copy_without_mutating_input():
    # One bad timestamp among many: drop it (like highlight stubs) rather than
    # burn a retry. Checks are PURE — the filtered list is the return value,
    # and the input model is untouched.
    m = _chapters("00:00:00", "00:05:00", "00:10:00", "00:15:00", "00:20:00", "01:01:56:00")
    out = _check_chapters(m, transcript_end=3600)  # 1 of 6 bad (<20%) → passes
    assert [c.timestamp for c in out.chapters] == [
        "00:00:00", "00:05:00", "00:10:00", "00:15:00", "00:20:00"]
    assert len(m.chapters) == 6  # input not mutated


def test_check_chapters_attaches_salvage_when_raising():
    # When it raises, the exception carries the filtered model (what
    # degraded-accept may store — never the malformed chapters) and its rank.
    m = _chapters("00:00:00", "04:15:00", "59:14:00", "01:01:56:00")
    with pytest.raises(DegenerateOutputError) as ei:
        _check_chapters(m, transcript_end=5185)
    assert [c.timestamp for c in ei.value.salvaged.chapters] == ["00:00:00"]
    assert ei.value.usable == 1
    assert len(m.chapters) == 4  # input not mutated


def test_check_chapters_keeps_duplicate_start_twins():
    # Chapters sharing a start (teaser + intro both at 00:00:00, or a dup
    # created by normalization "0:05:00" vs "00:05:00") are valid output —
    # dropping one deletes its title and writeup. All twins are kept; the
    # window logic (chapter_window) gives the later twin the real span.
    m = _chapters("00:00:00", "00:05:00", "0:05:00", "00:10:00")
    out = _check_chapters(m)
    assert [c.title for c in out.chapters] == ["c0", "c1", "c2", "c3"]


def test_duplicate_starts_do_not_count_toward_degenerate_fraction():
    # Even a dup-heavy list must not trigger the >20% degenerate retry, and a
    # list with at least two DISTINCT starts still spans a timeline.
    m = _chapters("00:00:00", "00:00:00", "00:05:00", "00:05:00")  # 50% dups
    assert len(_check_chapters(m).chapters) == 4


def test_llm_schema_property_order_unchanged():
    # Property order in model_json_schema() steers grammar-constrained
    # generation (Ollama `format`, strict `json_schema`) on every call; keep
    # the order the prompts were tuned against, not the base-class-first order
    # inheritance would produce.
    assert list(Chapter.model_json_schema()["properties"]) == ["title", "timestamp", "summary"]
    assert list(Highlight.model_json_schema()["properties"]) == ["text", "timestamp", "speaker", "kind"]


def test_chapterlist_normalizes_and_sorts_on_construction():
    # Fixture crosses an hour-of-day digit boundary: raw strings sort
    # "10:05:00" < "9:59:00", so this fails if sorting ever runs before
    # normalization (or on raw strings).
    m = _chapters("10:05:00", "9:59:00")
    assert [c.timestamp for c in m.chapters] == ["09:59:00", "10:05:00"]
    assert [c.seconds for c in m.chapters] == [9 * 3600 + 59 * 60, 10 * 3600 + 5 * 60]


def test_check_highlights_empty_is_degenerate():
    # episode 109241: an 8-token completion returned {"highlights": []}.
    with pytest.raises(DegenerateOutputError):
        _check_highlights(HighlightList(highlights=[]))


def test_check_highlights_drops_a_few_stubs_but_keeps_the_list():
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(10)] + [
        Highlight(text="On GenAI: ", timestamp="00:01:00", speaker="A", kind="takeaway"),
    ])
    out = _check_highlights(hl)  # 1 of 11 bad (<20%) → passes
    assert len(out.highlights) == 10  # the stub was dropped from the copy
    assert all(h.text != "On GenAI: " for h in out.highlights)
    assert len(hl.highlights) == 11  # input not mutated


def test_check_highlights_retries_when_mostly_degenerate():
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(6)] + [
        Highlight(text="On X: ", timestamp="00:01:00", speaker="A", kind="takeaway") for _ in range(6)
    ])
    with pytest.raises(DegenerateOutputError):  # 6 of 12 bad (>20%)
        _check_highlights(hl)


def test_check_highlights_drops_items_with_bad_timestamps():
    good_text = _good_highlight(0).text
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(10)] + [
        Highlight(text=good_text, timestamp="01:01:56:00", speaker="A", kind="takeaway"),
        Highlight(text=good_text, timestamp="04:15:00", speaker="A", kind="takeaway"),
    ])
    out = _check_highlights(hl, transcript_end=5185)  # 2 of 12 bad (<20%) → passes
    assert len(out.highlights) == 10
    assert all(h.timestamp.count(":") == 2 for h in out.highlights)


def test_highlight_timestamps_normalized_on_construction():
    h = Highlight(text=_good_highlight(0).text, timestamp="1:00:00", speaker="A", kind="takeaway")
    assert h.timestamp == "01:00:00"
    assert h.seconds == 3600
    # A malformed stamp survives construction (stored legacy summaries must
    # still deserialize) and surfaces as seconds=None instead.
    bad = Highlight(text=_good_highlight(0).text, timestamp="01:01:56:00", speaker="A", kind="takeaway")
    assert bad.timestamp == "01:01:56:00"
    assert bad.seconds is None


def test_timestamp_field_is_frozen():
    # seconds is a per-instance cache; freezing the field makes stale caches
    # impossible rather than a convention.
    h = _good_highlight(0)
    assert h.seconds == 0
    with pytest.raises(pydantic.ValidationError):
        h.timestamp = "00:59:00"


def test_transcript_end_slack_boundary_is_inclusive():
    end = 5185
    at_slack = Highlight(text=_good_highlight(0).text, speaker="A", kind="takeaway",
                         timestamp=format_timestamp(end + 60))
    past_slack = Highlight(text=_good_highlight(0).text, speaker="A", kind="takeaway",
                           timestamp=format_timestamp(end + 61))
    hl = HighlightList(highlights=[_good_highlight(i) for i in range(10)] + [at_slack, past_slack])
    out = _check_highlights(hl, transcript_end=end)
    kept = [h.timestamp for h in out.highlights]
    assert at_slack.timestamp in kept
    assert past_slack.timestamp not in kept


def test_transcript_end_seconds():
    assert transcript_end_seconds("[00:00:10] [A] hi\n[00:20:00] [B] bye") == 1200
    # The Deepgram no-utterances fallback emits one [00:00:00] line; an end of
    # 0 is no usable bound (it would reject everything past the first minute).
    assert transcript_end_seconds("[00:00:00] [SPEAKER_00] everything at once") is None
    assert transcript_end_seconds("no timestamps here") is None


def test_transcript_is_degenerate():
    # Degenerate = no usable timeline (extent under a minute): the single-line
    # no-utterances fallback, a many-line alignment failure where every stamp
    # sits at/near zero (a tiny extent would make the past-end guard reject
    # everything real), and sub-minute audio with nothing to chapter.
    assert transcript_is_degenerate("[00:00:00] [SPEAKER_00] the whole episode as one line")
    assert transcript_is_degenerate("\n".join("[00:00:00] [A] word" for _ in range(50)))
    assert transcript_is_degenerate("[00:00:00] [A] a\n[00:00:01] [A] b")  # 1s extent
    assert transcript_is_degenerate("no timestamps at all")
    assert transcript_is_degenerate("[00:00:05] [A] hi\n[00:00:31] [A] bye")  # 31s trailer
    # A short-but-real episode spanning at least a minute keeps the full
    # pipeline.
    assert not transcript_is_degenerate("[00:00:05] [A] hi\n[00:01:31] [A] ninety seconds in")


def test_summarize_stores_prose_only_for_degenerate_transcript(monkeypatch):
    # A transcript with no usable timeline must not fail the job (the worker
    # would retry the identical failure forever — re-transcribing reproduces
    # the same transcript) and must not burn ~13 timeline-pass LLM calls.
    # Exactly two calls are allowed: speakers + summary; _replies raises on a
    # third.
    speakers_json = json.dumps({"speakers": [
        {"label": "SPEAKER_00", "name": "Ana", "role": "host",
         "evidence_timestamp": "00:00:00", "evidence_quote": "hi"},
    ]})
    prose = json.dumps({"summary": "A thorough summary. " * 20})
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content=speakers_json),
        summarize.ChatResult(content=prose),
    ))
    result = summarize.summarize("[00:00:00] [SPEAKER_00] everything at once", backend=BACKEND)
    assert result.summary.startswith("A thorough summary.")
    assert [s.name for s in result.speakers] == ["Ana"]
    assert result.chapters == [] and result.highlights == []


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


def test_degraded_accept_keeps_fullest_attempt(monkeypatch):
    # The checks strip unusable items in place before raising, so without a
    # preference the LAST failed attempt — often the emptiest — would be
    # stored. prefer=len must keep the attempt with the most usable items.
    def hl(n: int) -> summarize.ChatResult:
        return summarize.ChatResult(content=json.dumps({"highlights": [
            {"text": _good_highlight(i).text, "timestamp": f"00:0{i}:00",
             "speaker": "A", "kind": "takeaway"} for i in range(n)]}))

    monkeypatch.setattr(summarize, "_chat", _replies(hl(3), hl(1), hl(2)))
    model = _checked_or_fail(HighlightList, BACKEND, "s", "u", _check_highlights)
    assert len(model.highlights) == 3  # not the last attempt's 2


def test_degraded_accept_ranks_by_distinct_chapters(monkeypatch):
    # Ranking failed chapter attempts must count DISTINCT usable starts, not
    # raw length — an all-duplicates list (4 chapters, one start) must lose to
    # a shorter attempt spanning two real starts.
    def chs(stamps: list[str]) -> summarize.ChatResult:
        return summarize.ChatResult(content=json.dumps({"chapters": [
            {"title": f"c{i}", "timestamp": t, "summary": "s"}
            for i, t in enumerate(stamps)]}))

    dup_heavy = chs(["00:00:00"] * 4)                       # usable=1, len 4
    spans_two = chs(["00:00:00", "00:05:00", "junk", "junk"])  # usable=2, len 2, >20% bad
    monkeypatch.setattr(summarize, "_chat", _replies(dup_heavy, spans_two, dup_heavy))
    model, passed = _chat_checked(BACKEND, "s", "u", ChapterList,
                                  lambda m: _check_chapters(m, transcript_end=3600))
    assert not passed
    assert [c.timestamp for c in model.chapters] == ["00:00:00", "00:05:00"]


def test_summarize_fails_when_no_usable_chapters_or_highlights(monkeypatch):
    # Degraded-accept keeps a filtered best-failed model; if that leaves BOTH
    # lists empty, storing the summary would be a near-empty episode page —
    # fail the job so the worker retries instead.
    speakers = json.dumps({"speakers": [
        {"label": "SPEAKER_00", "name": "Ana", "role": "host",
         "evidence_timestamp": "00:00:01", "evidence_quote": "I'm Ana"},
    ]})
    ep_summary = json.dumps({"summary": "A thorough summary. " * 20})
    bad_chapters = json.dumps({"chapters": [
        {"title": "x", "timestamp": "junk", "summary": "s"},
        {"title": "y", "timestamp": "01:01:56:00", "summary": "s"},
    ]})
    bad_highlights = json.dumps({"highlights": [
        {"text": "On X: ", "timestamp": "00:01:00", "speaker": "A", "kind": "takeaway"},
    ]})
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content=speakers),
        summarize.ChatResult(content=ep_summary),
        *[summarize.ChatResult(content=bad_chapters)] * 3,
        *[summarize.ChatResult(content=bad_highlights)] * 3,
    ))
    with pytest.raises(DegenerateOutputError):
        summarize.summarize(
            "[00:00:10] [SPEAKER_00] hello there\n[00:30:00] [SPEAKER_00] goodbye",
            backend=BACKEND)


# --- chapter enrichment: retry + no-downgrade fallback ---------------------

def test_summarize_fails_on_stamp_shift_misfire_with_surviving_opener(monkeypatch):
    # The systematic "MM:SS:00" stamp-shift misfire: the 00:00:00 opener
    # always survives filtering, so chapters is non-empty — but one chapter
    # and zero highlights is a near-empty page, strictly worse than failing
    # the job so the (stochastic) misfire can be retried.
    speakers = json.dumps({"speakers": [
        {"label": "SPEAKER_00", "name": "Ana", "role": "host",
         "evidence_timestamp": "00:00:01", "evidence_quote": "I'm Ana"},
    ]})
    ep_summary = json.dumps({"summary": "A thorough summary. " * 20})
    shifted = json.dumps({"chapters": [
        {"title": "open", "timestamp": "00:00:00", "summary": "s"},
        {"title": "shifted", "timestamp": "04:15:00", "summary": "s"},   # 4m15s emitted as 4h15m
        {"title": "shifted2", "timestamp": "10:27:00", "summary": "s"},
    ]})
    bad_highlights = json.dumps({"highlights": [
        {"text": "On X: ", "timestamp": "00:01:00", "speaker": "A", "kind": "takeaway"},
    ]})
    detail = json.dumps({"summary": "A decent chapter detail sentence."})
    monkeypatch.setattr(summarize, "_chat", _replies(
        summarize.ChatResult(content=speakers),
        summarize.ChatResult(content=ep_summary),
        *[summarize.ChatResult(content=shifted)] * 3,
        summarize.ChatResult(content=detail),  # enrichment of the salvaged opener
        *[summarize.ChatResult(content=bad_highlights)] * 3,
    ))
    with pytest.raises(DegenerateOutputError):
        summarize.summarize(
            "[00:00:10] [SPEAKER_00] hello there\n[00:30:00] [SPEAKER_00] goodbye",
            backend=BACKEND)


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
    # Baidu is always denied (structured-output liar), even with no retry-level
    # exclusions yet.
    assert captured["payload"]["provider"] == {"require_parameters": True, "ignore": ["Baidu"]}


def test_openrouter_merges_denylist_with_retry_exclusions(monkeypatch):
    # provider.ignore is the union of the always-on denylist and the providers
    # the retry loop blacklists mid-call, de-duplicated.
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                          "usage": {}, "provider": "DeepInfra"})

    monkeypatch.setattr(summarize.httpx, "post", fake_post)
    summarize._chat_openrouter(BACKEND, "s", "u", {"type": "object"},
                               ignore_providers=["Baidu", "Wafer"])
    assert captured["payload"]["provider"] == {"require_parameters": True,
                                               "ignore": ["Baidu", "Wafer"]}


def test_degenerate_provider_excluded_on_retry(monkeypatch):
    # Backstop for any provider that returns prose mid-call (not just the
    # statically denied ones): the retry must blacklist it rather than re-roll
    # onto the same offender.
    seen_ignores = []
    replies = iter([
        summarize.ChatResult(content="prose, not json", provider="Wafer"),
        summarize.ChatResult(content=json.dumps({"summary": "A complete summary. " * 20}),
                             provider="DeepInfra"),
    ])

    def chat(backend, system, user, schema, repair=False, ignore_providers=None):
        seen_ignores.append(ignore_providers)
        return next(replies)

    monkeypatch.setattr(summarize, "_chat", chat)
    model, passed = _chat_checked(BACKEND, "s", "u", Summary, _check_summary)
    assert passed
    assert seen_ignores[0] is None        # first attempt: nothing excluded yet
    assert seen_ignores[1] == ["Wafer"]   # retry routes around the offender


# --- retry warning event carries diagnostics -------------------------------

def _capture_json_logs(monkeypatch, fn):
    monkeypatch.setenv("PODRACER_LOG_FORMAT", "json")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    monkeypatch.setattr(logging_config, "_configured_format", None)
    structlog.reset_defaults()
    logging_config.configure_logging()
    fn()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_degenerate_event_carries_provider_model_tokens_episode(monkeypatch):
    # The 109241 highlights failure shape: a degenerate first response, then a
    # clean retry. The warning must name the provider/model/tokens/episode so a
    # regression is triageable from OpenSearch.
    degenerate = summarize.ChatResult(content="here are the highlights ...", provider="Baidu",
                                      finish_reason="stop", input_tokens=29390, output_tokens=8)
    good = summarize.ChatResult(content=json.dumps({"summary": "A complete summary. " * 20}))
    monkeypatch.setattr(summarize, "_chat", _replies(degenerate, good))

    def run():
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(episode_id=5588)  # as summarize_episode/worker do
        try:
            _chat_checked(BACKEND, "s", "u", Summary, _check_summary)
        finally:
            structlog.contextvars.clear_contextvars()

    lines = _capture_json_logs(monkeypatch, run)
    ev = next(r for r in lines if r.get("event") == "llm_degenerate_output")
    assert ev["backend"] == "openrouter"
    assert ev["model"] == "deepseek/deepseek-v4-flash"
    assert ev["provider"] == "Baidu"
    assert ev["input_tokens"] == 29390 and ev["output_tokens"] == 8
    assert ev["reason"] == "invalid_json"
    assert ev["episode_id"] == 5588  # auto-attached via the bound contextvar
