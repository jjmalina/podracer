"""Tests for the per-chapter transcript slicing used by enrich_chapters."""
from podracer.models import Chapter
from podracer.summarize import _is_teaser_chapter, _slice_transcript_by_chapter
from podracer.timestamps import chapter_window

TRANSCRIPT = """\
[00:00:10] [Host] welcome
[00:01:00] [Host] intro
[00:05:00] [Guest] start of chapter two
[00:05:30] [Host] mid chapter two
[00:10:00] [Guest] start of chapter three
[00:15:00] [Host] final remark
unrelated line without timestamp
[00:30:00] [Host] way later"""


def test_slice_returns_lines_in_window():
    out = _slice_transcript_by_chapter(TRANSCRIPT, 300, 600)
    assert out.splitlines() == [
        "[00:05:00] [Guest] start of chapter two",
        "[00:05:30] [Host] mid chapter two",
    ]


def test_slice_excludes_end_timestamp():
    # 00:10:00 belongs to the next chapter, not this one.
    out = _slice_transcript_by_chapter(TRANSCRIPT, 0, 600)
    assert "[00:10:00]" not in out
    assert "[00:05:30]" in out


def test_slice_skips_lines_without_timestamp():
    out = _slice_transcript_by_chapter(TRANSCRIPT, 0, None)
    assert "unrelated line" not in out
    # Every kept line should start with a timestamp.
    for line in out.splitlines():
        assert line.startswith("[")


def test_slice_with_open_end_catches_tail():
    out = _slice_transcript_by_chapter(TRANSCRIPT, 900, None)
    assert "[00:15:00] [Host] final remark" in out
    assert "[00:30:00] [Host] way later" in out


def test_slice_empty_when_no_lines_in_window():
    out = _slice_transcript_by_chapter(TRANSCRIPT, 1200, 1500)
    assert out == ""


def test_chapter_window_twins_and_malformed():
    # The ONE definition of chapter windows, shared by enrichment slicing and
    # page nesting. Twins sharing a start: the earlier gets an empty [s, s)
    # window, the later owns the span (pre-validation index-window behavior).
    chs = [Chapter(title="teaser", timestamp="00:00:00", summary=""),
           Chapter(title="intro", timestamp="00:00:00", summary=""),
           Chapter(title="two", timestamp="00:10:00", summary="")]
    assert chapter_window(chs, 0) == (0, 0)
    assert chapter_window(chs, 1) == (0, 600)
    assert chapter_window(chs, 2) == (600, None)

    # A malformed NEIGHBOR must not widen the window to the end of the
    # episode: the end skips to the next chapter whose stamp parses.
    mixed = [Chapter(title="a", timestamp="00:00:00", summary=""),
             Chapter(title="bad", timestamp="01:01:56:00", summary=""),
             Chapter(title="c", timestamp="00:10:00", summary="")]
    assert chapter_window(mixed, 0) == (0, 600)
    assert chapter_window(mixed, 1) == (None, None)  # malformed start: no window


def test_slice_empty_for_malformed_start():
    # chapter_window signals a malformed chapter stamp as start=None; the
    # slice contract turns that into an empty slice (enrichment then keeps the
    # short summary without spending an LLM call on a wrong window).
    assert _slice_transcript_by_chapter(TRANSCRIPT, None, None) == ""


def test_teaser_chapters_are_detected():
    # A teaser/cold-open chapter is skipped during enrichment so its montage of
    # clips-from-later isn't inflated into detail that duplicates real chapters.
    assert _is_teaser_chapter(Chapter(title="Teaser", timestamp="00:00:00", summary=""))
    assert _is_teaser_chapter(Chapter(title="Cold Open", timestamp="00:00:00", summary=""))
    assert not _is_teaser_chapter(Chapter(title="AI IPO Season", timestamp="00:02:00", summary=""))
