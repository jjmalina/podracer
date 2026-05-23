"""Tests for the per-chapter transcript slicing used by enrich_chapters."""
from podracer.summarize import _slice_transcript_by_chapter

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
    out = _slice_transcript_by_chapter(TRANSCRIPT, "00:05:00", "00:10:00")
    assert out.splitlines() == [
        "[00:05:00] [Guest] start of chapter two",
        "[00:05:30] [Host] mid chapter two",
    ]


def test_slice_excludes_end_timestamp():
    # 00:10:00 belongs to the next chapter, not this one.
    out = _slice_transcript_by_chapter(TRANSCRIPT, "00:00:00", "00:10:00")
    assert "[00:10:00]" not in out
    assert "[00:05:30]" in out


def test_slice_skips_lines_without_timestamp():
    out = _slice_transcript_by_chapter(TRANSCRIPT, "00:00:00", "99:99:99")
    assert "unrelated line" not in out
    # Every kept line should start with a timestamp.
    for line in out.splitlines():
        assert line.startswith("[")


def test_slice_with_sentinel_end_catches_tail():
    out = _slice_transcript_by_chapter(TRANSCRIPT, "00:15:00", "99:99:99")
    assert "[00:15:00] [Host] final remark" in out
    assert "[00:30:00] [Host] way later" in out


def test_slice_empty_when_no_lines_in_window():
    out = _slice_transcript_by_chapter(TRANSCRIPT, "00:20:00", "00:25:00")
    assert out == ""
