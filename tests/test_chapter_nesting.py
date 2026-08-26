"""Unit tests for the chapter-nesting helper used by the episode page."""
from fastapi.testclient import TestClient

from podracer.config import Config
from podracer.db import get_connection, init_db, save_summary, subscribe, upsert_episode, upsert_podcast
from podracer.models import Chapter, Highlight, Insight, PodcastSummary, SpeakerTake
from podracer.timestamps import chapter_window
from podracer.web.app import create_app
from podracer.web.routes.episodes import _nest_under_chapters
from tests.conftest import feed_ep


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


def _nest(summary: PodcastSummary, transcript_end: int | None = None):
    """Call the route helper the way episode_detail does: highlights computed
    once and passed in, plus the transcript's end bound."""
    return _nest_under_chapters(summary, summary.effective_highlights(), transcript_end)


def _hl(ts: str, text: str = "h", kind: str = "takeaway") -> Highlight:
    return Highlight(text=text, timestamp=ts, speaker="A", kind=kind)


def _ch(ts: str, title: str) -> Chapter:
    return Chapter(title=title, timestamp=ts, summary="")


def test_happy_path_bins_into_correct_chapters():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two"), _ch("00:20:00", "three")],
        highlights=[_hl("00:01:00", "early"), _hl("00:15:00", "mid"), _hl("00:25:00", "late")],
    )

    nested, pre, orphan = _nest(summary)

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

    nested, pre, orphan = _nest(summary)

    assert nested is not None
    assert nested[0]["highlights"] == []
    assert [h.text for h in pre["highlights"]] == ["intro"]
    assert orphan == {"highlights": []}


def test_items_after_last_chapter_land_in_final_chapter():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "last")],
        highlights=[_hl("01:00:00", "way-after")],
    )

    nested, _pre, orphan = _nest(summary)

    assert nested is not None
    assert nested[1]["highlights"][0].text == "way-after"
    assert orphan == {"highlights": []}


def test_empty_chapters_returns_none_for_caller_fallback():
    summary = _summary(chapters=[], highlights=[_hl("00:01:00")])

    nested, pre, orphan = _nest(summary)

    assert nested is None
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_no_highlights_does_not_crash():
    summary = _summary(chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")])

    nested, pre, orphan = _nest(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two"]
    assert all(e["highlights"] == [] for e in nested)
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_binning_is_numeric_not_lexicographic():
    # "01:30:00" sorts before "1:00:00" as a string but is after it as a time.
    # The prod incident was this shape: chapter timestamps in a different
    # (mis)format than highlight timestamps, mis-binned by string comparison.
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("1:00:00", "two")],
        highlights=[_hl("01:30:00", "late")],
    )

    nested, _pre, _orphan = _nest(summary)

    assert nested is not None
    assert nested[0]["highlights"] == []
    assert [h.text for h in nested[1]["highlights"]] == ["late"]


def test_malformed_chapter_timestamp_falls_back_to_flat():
    # Stored summaries predating timestamp validation can carry four-field
    # chapter timestamps ("01:01:56:00"). Mis-binning is worse than no
    # binning, so the page falls back to the flat highlight list.
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("01:01:56:00", "two")],
        highlights=[_hl("00:01:00")],
    )

    nested, pre, orphan = _nest(summary)

    assert nested is None
    assert pre == {"highlights": []}
    assert orphan == {"highlights": []}


def test_chapters_past_transcript_end_fall_back_to_flat():
    # The shifted "MM:SS:00" misfire parses as valid HH:MM:SS and sorts fine
    # ("04:15:00" meaning 4m15s reads as 4h15m); only the transcript's own
    # extent exposes it on read — RSS durations are not trusted (feeds publish
    # bare-integer minutes that get stored as seconds).
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("04:15:00", "two")],
        highlights=[_hl("00:30:00", "h")],
    )

    nested, _pre, _orphan = _nest(summary, transcript_end=5185)
    assert nested is None

    # The identical chapters are fine when the episode really is that long,
    # and with no transcript to anchor to, the guard stays out of the way.
    assert _nest(summary, transcript_end=5 * 3600)[0] is not None
    assert _nest(summary)[0] is not None


def test_highlight_past_transcript_end_lands_in_orphan():
    # A legacy shifted highlight stamp hours past the episode must not bin
    # under the final chapter's open-ended window.
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")],
        highlights=[_hl("04:15:00", "shifted"), _hl("00:11:00", "kept")],
    )

    nested, _pre, orphan = _nest(summary, transcript_end=5185)

    assert nested is not None
    assert [h.text for h in nested[1]["highlights"]] == ["kept"]
    assert [h.text for h in orphan["highlights"]] == ["shifted"]


def test_duplicate_chapter_starts_bin_under_later_twin():
    # Legacy data can hold two chapters at the same second. Both render with
    # their writeups, and the LATER twin owns the window (matching the
    # pre-validation index windows, where the second twin carried the real
    # content and got enriched) — a highlight must never appear under both.
    summary = _summary(
        chapters=[_ch("00:00:00", "teaser"), _ch("00:00:00", "intro"), _ch("00:10:00", "two")],
        highlights=[_hl("00:01:00", "early")],
    )

    nested, _pre, _orphan = _nest(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["teaser", "intro", "two"]
    assert nested[0]["highlights"] == []
    assert [h.text for h in nested[1]["highlights"]] == ["early"]


def test_nesting_index_windows_match_chapter_window():
    # _nest_under_chapters bins with consecutive index windows over its
    # verified-parseable sorted starts; for such a list that must be exactly
    # timestamps.chapter_window's semantics (twins included) — this pins the
    # equivalence the nesting code relies on.
    summary = _summary(
        chapters=[_ch("00:00:00", "teaser"), _ch("00:00:00", "intro"),
                  _ch("00:10:00", "two"), _ch("00:25:00", "three")],
    )
    chapters = summary.chapters
    starts = [c.seconds for c in chapters]
    for i in range(len(chapters)):
        expected_end = starts[i + 1] if i + 1 < len(chapters) else None
        assert chapter_window(chapters, i) == (starts[i], expected_end)


def test_unparseable_highlight_timestamp_lands_in_orphan():
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")],
        highlights=[_hl("garbled", "lost"), _hl("00:11:00", "kept")],
    )

    nested, pre, orphan = _nest(summary)

    assert nested is not None
    assert nested[0]["highlights"] == []
    assert [h.text for h in nested[1]["highlights"]] == ["kept"]
    assert pre == {"highlights": []}
    assert [h.text for h in orphan["highlights"]] == ["lost"]


def test_each_highlight_rendered_exactly_once():
    # Chapters arrive in any order; the model sorts them, so the windows
    # partition the timeline — no highlight may appear in two buckets (the
    # prod incident double-rendered highlights under overlapping windows).
    summary = _summary(
        chapters=[_ch("00:10:00", "two"), _ch("00:01:00", "one")],  # unsorted input
        highlights=[_hl("00:00:30", "w"), _hl("00:05:00", "x"),
                    _hl("00:10:00", "y"), _hl("00:30:00", "z"), _hl("junk", "j")],
    )

    nested, pre, orphan = _nest(summary)

    assert nested is not None
    assert [e["chapter"].title for e in nested] == ["one", "two"]
    everywhere = (
        [h.text for h in pre["highlights"]]
        + [h.text for e in nested for h in e["highlights"]]
        + [h.text for h in orphan["highlights"]]
    )
    assert sorted(everywhere) == ["j", "w", "x", "y", "z"]  # each exactly once
    assert [h.text for h in pre["highlights"]] == ["w"]
    assert [h.text for h in orphan["highlights"]] == ["j"]


def test_legacy_unparseable_timestamps_keep_string_order():
    # A pre-consolidation summary whose stamps don't parse at all (e.g. MM:SS)
    # still interleaves insights and takes in the old lexicographic order
    # rather than exposing the concatenation order of the two legacy lists.
    summary = _summary(
        chapters=[],
        insights=[Insight(text="second", timestamp="00:02", speaker="A")],
        takes=[SpeakerTake(speaker="B", take="first", timestamp="00:01")],
    )

    assert [h.text for h in summary.effective_highlights()] == ["first", "second"]


def test_page_keeps_chapter_writeups_when_nesting_falls_back(tmp_path):
    # When the un-nested fallback fires (a stored chapter timestamp doesn't
    # parse), the page must still render the chapter titles and writeups plus
    # the flat highlights — not silently drop the whole Chapters section.
    db_path = str(tmp_path / "page.db")
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("g", title="Ep"))
    summary = _summary(
        chapters=[_ch("00:00:00", "Opening Notes"), _ch("01:01:56:00", "Late Section")],
        highlights=[Highlight(text="a memorable point", timestamp="00:05:00",
                              speaker="A", kind="takeaway")],
    )
    summary.chapters[0].summary = "a substantive writeup"
    save_summary(conn, 1, summary.model_dump_json(), "m", "b")
    conn.commit()
    conn.close()

    with TestClient(create_app(Config(db_path=db_path))) as client:
        page = client.get("/episodes/1").text

    assert "Opening Notes" in page
    assert "Late Section" in page
    assert "a substantive writeup" in page
    assert "a memorable point" in page


def test_legacy_insights_and_takes_are_migrated():
    """Summaries stored before consolidation still render via effective_highlights."""
    summary = _summary(
        chapters=[_ch("00:00:00", "one"), _ch("00:10:00", "two")],
        insights=[Insight(text="fact", timestamp="00:01:00", speaker="A")],
        takes=[SpeakerTake(speaker="B", take="opine", timestamp="00:11:00")],
    )

    nested, _pre, _orphan = _nest(summary)

    assert nested is not None
    assert nested[0]["highlights"][0].text == "fact"
    assert nested[0]["highlights"][0].kind == "takeaway"
    assert nested[1]["highlights"][0].text == "opine"
    assert nested[1]["highlights"][0].kind == "opinion"
