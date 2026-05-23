"""Unit tests for the chapter-nesting helper used by the episode page."""
from podracer.summarize import Chapter, Insight, PodcastSummary, SpeakerTake
from podracer.web.routes.episodes import _nest_under_chapters


def _summary(
    chapters: list[Chapter],
    insights: list[Insight] | None = None,
    takes: list[SpeakerTake] | None = None,
) -> PodcastSummary:
    return PodcastSummary(
        summary="x",
        speakers=[],
        chapters=chapters,
        insights=insights or [],
        speaker_takes=takes or [],
    )


def _ins(ts: str, text: str = "i") -> Insight:
    return Insight(text=text, timestamp=ts, speaker="A")


def _take(ts: str, take: str = "t") -> SpeakerTake:
    return SpeakerTake(speaker="A", take=take, timestamp=ts)


def _ch(ts: str, title: str) -> Chapter:
    return Chapter(title=title, timestamp=ts, summary="")


def test_happy_path_bins_into_correct_chapters():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two"), _ch("00:20:00", "three")],
        insights=[_ins("00:01:00", "early"), _ins("00:15:00", "mid"), _ins("00:25:00", "late")],
        takes=[_take("00:05:00", "early"), _take("00:22:00", "late")],
    )

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two", "three"]
    assert [i.text for i in nested[0]["insights"]] == ["early"]
    assert [i.text for i in nested[1]["insights"]] == ["mid"]
    assert [i.text for i in nested[2]["insights"]] == ["late"]
    assert [t.take for t in nested[0]["takes"]] == ["early"]
    assert [t.take for t in nested[2]["takes"]] == ["late"]
    assert pre == {"insights": [], "takes": []}
    assert orphan == {"insights": [], "takes": []}


def test_items_before_first_chapter_land_in_pre_chapter():
    summary = _summary(
        chapters=[_ch("00:01:00", "first")],
        insights=[_ins("00:00:30", "intro")],
        takes=[_take("00:00:45", "intro-take")],
    )

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert nested[0]["insights"] == []
    assert nested[0]["takes"] == []
    assert [i.text for i in pre["insights"]] == ["intro"]
    assert [t.take for t in pre["takes"]] == ["intro-take"]
    assert orphan == {"insights": [], "takes": []}


def test_items_after_last_chapter_land_in_final_chapter():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "last")],
        insights=[_ins("01:00:00", "way-after")],
    )

    nested, _pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert nested[1]["insights"][0].text == "way-after"
    assert orphan == {"insights": [], "takes": []}


def test_empty_chapters_returns_none_for_caller_fallback():
    summary = _summary(
        chapters=[],
        insights=[_ins("00:01:00")],
        takes=[_take("00:01:00")],
    )

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is None
    assert pre == {"insights": [], "takes": []}
    assert orphan == {"insights": [], "takes": []}


def test_no_insights_or_takes_does_not_crash():
    summary = _summary(chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")])

    nested, pre, orphan = _nest_under_chapters(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two"]
    assert all(e["insights"] == [] and e["takes"] == [] for e in nested)
    assert pre == {"insights": [], "takes": []}
    assert orphan == {"insights": [], "takes": []}
