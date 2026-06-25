"""Digests: period/timezone math, the membership window, idempotent storage,
deterministic tree assembly, the scheduler's due/stale logic, and the web + API
routes. The LLM call is patched out (we test plumbing, not the model)."""
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import podracer.digest as digest_mod
from podracer.config import Config
from podracer.db import (
    count_digest_members,
    count_digests,
    digest_exists,
    get_connection,
    get_digest,
    get_digest_members,
    get_digests,
    get_podcast,
    init_db,
    save_digest,
    save_summary,
    save_transcript,
    set_config,
    set_podcast_tags,
    subscribe,
    topics_by_podcast,
    upsert_episode,
    upsert_podcast,
)
from podracer.digest import (
    DIGEST_WATERMARK_KEY,
    DigestData,
    DigestEpisode,
    DigestItem,
    DigestLLMOutput,
    DigestMember,
    DigestShow,
    DigestTopic,
    _assemble,
    _member_input,
    _week_daily_episodes,
    day_period,
    due_periods,
    format_period_label,
    generate_and_save,
    is_finalizable,
    utc_bounds,
    week_period,
)
from podracer.models import DigestMemberRow
from podracer.summarize import Chapter, Highlight, PodcastSummary, SpeakerIdentification
from podracer.web.app import create_app
from tests.conftest import feed_ep


def _summary_json(summary: str = "A long talk about copper markets and supply.", highlights=None) -> str:
    return PodcastSummary(
        summary=summary,
        speakers=[SpeakerIdentification(
            label="SPEAKER_00", name="Tracy", role="host",
            evidence_timestamp="00:00:01", evidence_quote="hi")],
        chapters=[Chapter(title="Intro", timestamp="00:00:00", summary="open")],
        highlights=highlights or [Highlight(
            text="Copper hit a record on a supply squeeze.",
            timestamp="00:05:00", speaker="Tracy", kind="takeaway")],
    ).model_dump_json()


def _member(eid, pid, ptitle, title, topics,
            prose="First sentence here. Then more detail follows.", highlights=None):
    return DigestMember(episode_id=eid, podcast_id=pid, podcast_title=ptitle,
                        title=title, topics=topics, summary_prose=prose,
                        top_highlights=highlights or [])


# --- period + timezone math --------------------------------------------------


def test_utc_bounds_utc_is_identity():
    lo, hi = utc_bounds(date(2026, 6, 15), date(2026, 6, 16), "UTC")
    assert (lo, hi) == ("2026-06-15 00:00:00", "2026-06-16 00:00:00")


def test_utc_bounds_eastern_offset():
    # America/New_York is UTC-4 in June (EDT): local midnight -> 04:00 UTC.
    lo, hi = utc_bounds(date(2026, 6, 15), date(2026, 6, 16), "America/New_York")
    assert (lo, hi) == ("2026-06-15 04:00:00", "2026-06-16 04:00:00")


def test_utc_bounds_dst_spring_forward_is_23h():
    # DST begins 2026-03-08 in the US: that local day is only 23h long. The
    # window must still be DST-correct (EST -5 at the start, EDT -4 at the end).
    lo, hi = utc_bounds(date(2026, 3, 8), date(2026, 3, 9), "America/New_York")
    assert (lo, hi) == ("2026-03-08 05:00:00", "2026-03-09 04:00:00")
    span = datetime.fromisoformat(hi) - datetime.fromisoformat(lo)
    assert span.total_seconds() == 23 * 3600


def test_week_period_snaps_to_monday():
    p = week_period(date(2026, 6, 17))  # a Wednesday
    assert p.kind == "week"
    assert (p.start, p.end) == (date(2026, 6, 15), date(2026, 6, 22))


def test_day_period_bounds():
    p = day_period(date(2026, 6, 23))
    assert (p.kind, p.start, p.end) == ("day", date(2026, 6, 23), date(2026, 6, 24))


def test_is_finalizable_respects_hour():
    p = day_period(date(2026, 6, 15))  # end = 2026-06-16
    nine_am = datetime(2026, 6, 16, 9, 0, tzinfo=ZoneInfo("UTC"))
    assert is_finalizable(p, nine_am, hour=8) is True
    assert is_finalizable(p, nine_am, hour=10) is False


def test_format_period_label():
    assert format_period_label("day", date(2026, 6, 23), date(2026, 6, 24)) == "Tue · Jun 23"
    assert format_period_label("week", date(2026, 6, 15), date(2026, 6, 22)) == "Week of Jun 15–21"


# --- membership query --------------------------------------------------------


def test_membership_only_subscribed_summarized_in_window(conn):
    sub = upsert_podcast(conn, "Subbed", None, "https://e/sub.xml")
    subscribe(conn, sub)
    unsub = upsert_podcast(conn, "Unsub", None, "https://e/uns.xml")

    upsert_episode(conn, sub, feed_ep("in1", title="In Window 1"))
    upsert_episode(conn, sub, feed_ep("in2", title="In Window 2"))
    upsert_episode(conn, sub, feed_ep("nosum", title="No Summary"))
    upsert_episode(conn, sub, feed_ep("out", title="Out Of Window"))
    upsert_episode(conn, unsub, feed_ep("unsubbed", title="Unsubbed Show"))
    conn.execute("UPDATE episodes SET published_at = '2026-06-15T09:00:00' WHERE guid = 'in1'")
    conn.execute("UPDATE episodes SET published_at = '2026-06-15T18:00:00' WHERE guid = 'in2'")
    conn.execute("UPDATE episodes SET published_at = '2026-06-15T10:00:00' WHERE guid = 'nosum'")
    conn.execute("UPDATE episodes SET published_at = '2026-06-20T10:00:00' WHERE guid = 'out'")
    conn.execute("UPDATE episodes SET published_at = '2026-06-15T11:00:00' WHERE guid = 'unsubbed'")
    conn.commit()
    # Summarize everything except 'nosum'.
    for guid in ("in1", "in2", "out", "unsubbed"):
        eid = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()["id"]
        save_summary(conn, eid, _summary_json(), "m", "b")

    lo, hi = utc_bounds(date(2026, 6, 15), date(2026, 6, 16), "UTC")
    members = get_digest_members(conn, lo, hi)
    titles = [m.title for m in members]
    # Only subscribed + summarized + in-window, newest first (18:00 before 09:00).
    assert titles == ["In Window 2", "In Window 1"]
    assert count_digest_members(conn, lo, hi) == 2


# --- primary topic (feed order) ----------------------------------------------


def test_topics_kept_in_feed_order_so_primary_is_declared(conn):
    pid = upsert_podcast(conn, "Show", None, "https://e/s.xml")
    # Feed declares them non-alphabetically; topics[0] must be the declared primary.
    set_podcast_tags(conn, pid, ["News", "Technology", "Education"])
    assert topics_by_podcast(conn, [pid])[pid] == ["News", "Technology", "Education"]
    assert get_podcast(conn, pid).topics == ["News", "Technology", "Education"]

    # Reordering by the feed re-ranks (changes the primary) even with the same set.
    set_podcast_tags(conn, pid, ["Technology", "News", "Education"])
    assert topics_by_podcast(conn, [pid])[pid] == ["Technology", "News", "Education"]


# --- storage (idempotent upsert) ---------------------------------------------


def test_save_digest_upserts_on_kind_and_start(conn):
    data1 = DigestData(overview="v1", topics=[], episode_count=3)
    save_digest(conn, "day", "2026-06-15", "2026-06-16", data1.model_dump_json(), 3, "m", "b")
    assert digest_exists(conn, "day", "2026-06-15")
    assert count_digests(conn, kind="day") == 1

    data2 = DigestData(overview="v2", topics=[], episode_count=5)
    save_digest(conn, "day", "2026-06-15", "2026-06-16", data2.model_dump_json(), 5, "m2", "b2")
    assert count_digests(conn, kind="day") == 1  # still one row
    rec = get_digest(conn, "day", "2026-06-15")
    assert rec.episode_count == 5 and rec.model == "m2"
    assert DigestData.model_validate_json(rec.data).overview == "v2"


def test_get_digests_filters_kind_and_orders_desc(conn):
    blob = DigestData(overview="x", topics=[], episode_count=1).model_dump_json()
    save_digest(conn, "day", "2026-06-14", "2026-06-15", blob, 1, "m", "b")
    save_digest(conn, "day", "2026-06-16", "2026-06-17", blob, 1, "m", "b")
    save_digest(conn, "week", "2026-06-08", "2026-06-15", blob, 1, "m", "b")

    days = get_digests(conn, kind="day", limit=10)
    assert [d.period_start for d in days] == ["2026-06-16", "2026-06-14"]  # newest first
    assert count_digests(conn) == 3  # both kinds
    assert [d.kind for d in get_digests(conn, kind="week", limit=10)] == ["week"]


# --- tree assembly -----------------------------------------------------------


def test_assemble_groups_orders_dedupes_and_falls_back():
    members = [
        _member(1, 10, "Acme Tech", "A1", ["Technology"]),
        _member(2, 10, "Acme Tech", "A2", ["Technology"]),
        _member(3, 11, "Dev Pod", "D1", ["Technology"]),
        _member(4, 20, "Biz Cast", "B1", ["Business", "Technology"]),  # primary = Business
        _member(5, 21, "Capital Desk", "C1", ["Business"]),
        _member(6, 30, "Untagged", "U1", [],
                prose="Untagged summary sentence. Extra clause. Third one.",
                highlights=["Stored highlight one."]),
    ]
    items = {
        1: ("Acme blurb one with enough length to read as a real blurb.", ["A1 highlight."]),
        2: ("Acme blurb two with enough length to read as a real blurb.", ["A2 highlight."]),
        3: ("Dev Pod blurb with enough length to read as a real blurb.", ["D1 highlight."]),
        4: ("Biz Cast blurb with enough length to read as a real blurb.", ["B1 highlight."]),
        5: ("Capital blurb with enough length to read as a real blurb.", ["C1 highlight."]),
    }
    data = _assemble(members, "Overview of the day.", items)

    # Topic order: most-covered first, 'Other' always last.
    assert [t.topic for t in data.topics] == ["Technology", "Business", "Other"]
    tech = data.topics[0]
    assert tech.episode_count == 3
    assert [s.podcast_title for s in tech.shows] == ["Acme Tech", "Dev Pod"]  # by count desc
    assert tech.shows[0].episodes[0].blurb.startswith("Acme blurb one")

    # A multi-topic show lands under its *primary* topic only — no repeats.
    all_ids = [e.episode_id for t in data.topics for s in t.shows for e in s.episodes]
    assert sorted(all_ids) == [1, 2, 3, 4, 5, 6]      # every episode exactly once
    assert len(all_ids) == len(set(all_ids))          # no duplication across topics
    biz = next(t for t in data.topics if t.topic == "Business")
    assert {s.podcast_title for s in biz.shows} == {"Biz Cast", "Capital Desk"}
    assert data.episode_count == 6

    # A dropped episode falls back to the summary's first sentences + stored highlights.
    other_ep = data.topics[-1].shows[0].episodes[0]
    assert other_ep.blurb == "Untagged summary sentence. Extra clause."
    assert other_ep.highlights == ["Stored highlight one."]


def test_member_input_prefers_daily_for_weekly():
    row = DigestMemberRow(episode_id=7, podcast_id=1, podcast_title="P", title="T",
                          summary_data=_summary_json("Stored summary prose."))
    # No daily entry -> the stored summary + highlights are the input.
    prose, highs = _member_input(row, {})
    assert prose == "Stored summary prose."
    assert highs == ["Copper hit a record on a supply squeeze."]
    # Covered by a daily digest -> feed that daily blurb + highlights (hierarchical).
    prose2, highs2 = _member_input(row, {7: ("The daily blurb.", ["A daily highlight."])})
    assert (prose2, highs2) == ("The daily blurb.", ["A daily highlight."])


def test_week_daily_episodes_reads_stored_dailies(conn):
    day_data = DigestData(
        overview="day", episode_count=1,
        topics=[DigestTopic(topic="Technology", episode_count=1, shows=[
            DigestShow(podcast_id=1, podcast_title="P", episodes=[
                DigestEpisode(episode_id=42, title="T", blurb="A daily blurb.",
                              highlights=["A daily highlight."])])])])
    save_digest(conn, "day", "2026-06-16", "2026-06-17", day_data.model_dump_json(), 1, "m", "b")
    got = _week_daily_episodes(conn, week_period(date(2026, 6, 16)))
    assert got == {42: ("A daily blurb.", ["A daily highlight."])}


# --- generation (LLM patched) ------------------------------------------------


def _patch_llm(monkeypatch):
    """Patch the LLM so generate_digest returns one canned line per episode found
    in the prompt — exercising the user-message builder, the check, and assembly
    without a network call."""
    def fake(model_cls, backend, system, user, check):
        ids = [int(m) for m in re.findall(r"episode_id: (\d+)", user)]
        out = DigestLLMOutput(
            overview="A steady day across the shows, with a few standout threads.",
            items=[DigestItem(
                episode_id=i,
                blurb=f"Episode {i} made a concrete, specific claim worth remembering, "
                      f"and walked through the reasoning behind it.",
                highlights=[f"Episode {i} stated a hard number.", f"Episode {i} drew a clear conclusion."],
            ) for i in ids])
        check(out)
        return out
    monkeypatch.setattr(digest_mod, "_checked_or_fail", fake)


def _cfg(**kw) -> Config:
    base = dict(digest_timezone="UTC", digest_hour=0,
                summarize_backend="ollama", summarize_model="test-model")
    base.update(kw)
    return Config(**base)


def _seed_day(conn, *, day="2026-06-15", topic="Technology", n=2):
    pid = upsert_podcast(conn, "Acme Tech", None, "https://e/acme.xml")
    subscribe(conn, pid)
    set_podcast_tags(conn, pid, [topic])
    for i in range(n):
        upsert_episode(conn, pid, feed_ep(f"e{i}", title=f"Episode {i}"))
        eid = conn.execute("SELECT id FROM episodes WHERE guid = ?", (f"e{i}",)).fetchone()["id"]
        conn.execute("UPDATE episodes SET published_at = ? WHERE id = ?",
                     (f"{day}T{10 + i:02d}:00:00", eid))
        save_summary(conn, eid, _summary_json(f"Summary {i} body."), "m", "b")
    conn.commit()
    return pid


def test_generate_and_save_end_to_end(conn, monkeypatch):
    _patch_llm(monkeypatch)
    _seed_day(conn, day="2026-06-15", topic="Technology", n=2)

    data = generate_and_save(conn, _cfg(), day_period(date(2026, 6, 15)))
    assert data is not None
    assert data.episode_count == 2
    assert [t.topic for t in data.topics] == ["Technology"]
    assert data.overview.endswith(".")

    rec = get_digest(conn, "day", "2026-06-15")
    assert rec.episode_count == 2 and rec.backend == "ollama" and rec.model == "test-model"
    stored = DigestData.model_validate_json(rec.data)
    ep = stored.topics[0].shows[0].episodes[0]
    assert ep.blurb.startswith("Episode ")
    assert len(ep.highlights) >= 1


def test_generate_and_save_empty_period_writes_no_row(conn, monkeypatch):
    _patch_llm(monkeypatch)  # never invoked: empty period short-circuits
    assert generate_and_save(conn, _cfg(), day_period(date(2026, 1, 1))) is None
    assert not digest_exists(conn, "day", "2026-01-01")


# --- scheduler (due / fresh / stale) -----------------------------------------


def test_due_periods_missing_then_fresh_then_stale(conn, monkeypatch):
    _patch_llm(monkeypatch)
    cfg = _cfg()
    # A day that is safely finalizable (two days ago) and within the horizon.
    target = datetime.now(UTC).date() - timedelta(days=2)
    _seed_day(conn, day=target.isoformat(), topic="Technology", n=1)
    set_config(conn, DIGEST_WATERMARK_KEY, target.isoformat())

    # Missing row -> the day is due.
    due = due_periods(conn, cfg)
    assert any(p.kind == "day" and p.start == target for p in due)

    # Generate it; now it's fresh -> not due.
    generate_and_save(conn, cfg, day_period(target))
    due = due_periods(conn, cfg)
    assert not any(p.kind == "day" and p.start == target for p in due)

    # A straggler summarized after the day closed grows the count -> stale -> due.
    pid = conn.execute("SELECT id FROM podcasts LIMIT 1").fetchone()["id"]
    upsert_episode(conn, pid, feed_ep("late", title="Late Arrival"))
    eid = conn.execute("SELECT id FROM episodes WHERE guid = 'late'").fetchone()["id"]
    conn.execute("UPDATE episodes SET published_at = ? WHERE id = ?",
                 (f"{target.isoformat()}T20:00:00", eid))
    save_summary(conn, eid, _summary_json("late body"), "m", "b")
    conn.commit()
    due = due_periods(conn, cfg)
    assert any(p.kind == "day" and p.start == target for p in due)


def test_due_periods_respects_watermark(conn):
    # A finalizable day with members but a watermark in the future: not due.
    target = datetime.now(UTC).date()
    day = target - timedelta(days=2)
    _seed_day(conn, day=day.isoformat(), topic="Technology", n=1)
    set_config(conn, DIGEST_WATERMARK_KEY, (target + timedelta(days=1)).isoformat())
    assert not any(p.start == day for p in due_periods(conn, _cfg()))


# --- web + API routes --------------------------------------------------------


def _digest_blob(podcast_id=1, episode_id=1, overview="A busy Tuesday in tech.") -> str:
    return DigestData(
        overview=overview, episode_count=1,
        topics=[DigestTopic(topic="Technology", episode_count=1, shows=[
            DigestShow(podcast_id=podcast_id, podcast_title="Acme Tech", episodes=[
                DigestEpisode(
                    episode_id=episode_id, title="Quantum leap",
                    blurb="Quantum chips cleared a milestone, and the team explained why it matters.",
                    highlights=["Chips hit a new benchmark.", "Costs fell sharply."])])])],
    ).model_dump_json()


def _seed_routes(db_path: str) -> None:
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "Acme Tech", None, "https://e/acme.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("e1", title="Quantum leap"))
    save_transcript(conn, 1, "text", "whisper")
    save_summary(conn, 1, _summary_json(), "m", "b")
    blob = _digest_blob(podcast_id=pid, episode_id=1)
    save_digest(conn, "day", "2026-06-23", "2026-06-24", blob, 1, "model-x", "backend-y")
    save_digest(conn, "week", "2026-06-15", "2026-06-22", blob, 1, "model-x", "backend-y")
    conn.commit()
    conn.close()


def _client(tmp_path) -> TestClient:
    db_path = str(tmp_path / "digest.db")
    _seed_routes(db_path)
    return TestClient(create_app(Config(db_path=db_path)))


def test_web_digest_feed_lists_cards(tmp_path):
    with _client(tmp_path) as client:
        r = client.get("/digests")
        assert r.status_code == 200
        assert "A busy Tuesday in tech." in r.text
        assert "/digests/day/2026-06-23" in r.text
        # The week toggle scopes the feed to weekly rows.
        rw = client.get("/digests?kind=week")
        assert "/digests/week/2026-06-15" in rw.text


def test_web_digest_detail_links_into_archive(tmp_path):
    with _client(tmp_path) as client:
        r = client.get("/digests/day/2026-06-23")
        assert r.status_code == 200
        assert "Quantum chips cleared a milestone" in r.text   # the blurb
        assert "Chips hit a new benchmark." in r.text          # a highlight
        assert "/episodes/1" in r.text                         # links back to its episode
        assert "model-x" not in r.text                         # the model isn't displayed
        assert client.get("/digests/day/2099-01-01").status_code == 404


def test_api_digest_list_and_detail(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/api/v1/digests?kind=day").json()
        assert page["total"] == 1
        assert page["items"][0]["period_start"] == "2026-06-23"
        assert page["items"][0]["episode_count"] == 1
        assert page["items"][0]["topic_count"] == 1

        detail = client.get("/api/v1/digests/day/2026-06-23").json()
        assert detail["overview"] == "A busy Tuesday in tech."
        assert detail["topics"][0]["topic"] == "Technology"
        ep = detail["topics"][0]["shows"][0]["episodes"][0]
        assert ep["blurb"].startswith("Quantum chips cleared a milestone")
        assert ep["highlights"] == ["Chips hit a new benchmark.", "Costs fell sharply."]


def test_api_digest_404_and_bad_kind(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/v1/digests/day/2099-01-01").status_code == 404
        assert client.get("/api/v1/digests/bogus/2026-06-23").status_code == 422  # DigestKind literal
