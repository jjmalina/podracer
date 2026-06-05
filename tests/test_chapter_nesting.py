"""Unit tests for the chapter-nesting helper used by the episode page."""
from podracer.summarize import Chapter, Highlight, Insight, PodcastSummary, SpeakerTake
from podracer.web.routes.episodes import _nest_under_chapters


def _summary(
    chapters: list[Chapter],
    highlights: list[Highlight] | None = None,
    *,
    insights: list[Insight] | None = None,
    takes: list[SpeakerTake] | None = None,
) -> PodcastSummary:
    return PodcastSummary(
        summary="x",
        speakers=[],
        chapters=chapters,
        highlights=highlights or [],
        insights=insights or [],
        speaker_takes=takes or [],
    )


def _hl(ts: str, text: str = "h", kind: str = "takeaway") -> Highlight:
    return Highlight(text=text, timestamp=ts, speaker="A", kind=kind)


def _ch(ts: str, title: str) -> Chapter:
    return Chapter(title=title, timestamp=ts, summary="")


def test_happy_path_bins_into_correct_chapters():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two"), _ch("00:20:00", "three")],
        highlights=[_hl("00:01:00", "early"), _hl("00:15:00", "mid"), _hl("00:25:00", "late")],
    )

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two", "three"]
    assert [h.text for h in nested[0]["highlights"]] == ["early"]
    assert [h.text for h in nested[1]["highlights"]] == ["mid"]
    assert [h.text for h in nested[2]["highlights"]] == ["late"]
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_items_before_first_chapter_land_in_pre_chapter():
    summary = _summary(
        chapters=[_ch("00:01:00", "first")],
        highlights=[_hl("00:00:30", "intro")],
    )

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert nested[0]["highlights"] == []
    assert [h.text for h in pre["highlights"]] == ["intro"]
    assert orphan == {"highlights": []}


def test_items_after_last_chapter_land_in_final_chapter():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "last")],
        highlights=[_hl("01:00:00", "way-after")],
    )

    nested, _pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert nested[1]["highlights"][0].text == "way-after"
    assert orphan == {"highlights": []}


def test_empty_chapters_returns_none_for_caller_fallback():
    summary = _summary(chapters=[], highlights=[_hl("00:01:00")])

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is None
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_no_highlights_does_not_crash():
    summary = _summary(chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")])

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two"]
    assert all(e["highlights"] == [] for e in nested)
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_legacy_insights_and_takes_are_migrated():
    """Summaries stored before consolidation still render via effective_highlights."""
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")],
        insights=[Insight(text="fact", timestamp="00:01:00", speaker="A")],
        takes=[SpeakerTake(speaker="B", take="opine", timestamp="00:11:00")],
    )

    nested, _pre, _orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert nested[0]["highlights"][0].text == "fact"
    assert nested[0]["highlights"][0].kind == "takeaway"
    assert nested[1]["highlights"][0].text == "opine"
    assert nested[1]["highlights"][0].kind == "opinion"
