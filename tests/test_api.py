"""JSON API (/api/v1) — read access for aggregation apps.

Two layers: direct DB-primitive tests (list_episodes / count_episodes /
get_podcasts) over the in-memory `conn` fixture, and route tests over a
TestClient against a seeded file DB (the app opens its own connections, so the
seed must commit before closing — mirrors tests/test_feed.py).
"""
import json

from fastapi.testclient import TestClient

from podracer.config import Config
from podracer.db import (
    count_episodes,
    count_podcasts,
    get_connection,
    get_podcasts,
    init_db,
    list_episodes,
    save_summary,
    save_transcript,
    set_podcast_tags,
    subscribe,
    upsert_episode,
    upsert_podcast,
)
from podracer.summarize import (
    Chapter,
    Insight,
    PodcastSummary,
    SpeakerIdentification,
    SpeakerTake,
)
from podracer.web.app import create_app
from tests.conftest import feed_ep


def _summary_json(**overrides) -> str:
    base = dict(
        summary="A talk about copper.",
        speakers=[SpeakerIdentification(
            label="SPEAKER_00", name="Tracy", role="host",
            evidence_timestamp="00:00:01", evidence_quote="hi",
        )],
        chapters=[Chapter(title="Intro", timestamp="00:00:00", summary="open")],
    )
    base.update(overrides)
    return PodcastSummary(**base).model_dump_json()


# --- DB layer ----------------------------------------------------------------


def test_list_episodes_newest_first_with_flags(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("a", title="A"))
    upsert_episode(conn, pid, feed_ep("b", title="B"))
    conn.execute("UPDATE episodes SET published_at = '2026-01-01T00:00:00' WHERE id = 1")
    conn.execute("UPDATE episodes SET published_at = '2026-03-01T00:00:00' WHERE id = 2")
    save_summary(conn, 1, _summary_json(), "m", "b")     # ep 1 has a summary
    save_transcript(conn, 2, "words", "m")               # ep 2 has a transcript

    items = list_episodes(conn, limit=10)
    assert [it.title for it in items] == ["B", "A"]       # newest first
    by_id = {it.id: it for it in items}
    assert by_id[1].has_summary and not by_id[1].has_transcript
    assert by_id[2].has_transcript and not by_id[2].has_summary
    # summary_data only rides along when asked for
    assert by_id[1].summary_data is None
    assert count_episodes(conn) == 2


def test_list_episodes_status_filter(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("done", title="Done"))
    upsert_episode(conn, pid, feed_ep("wait", title="Wait"))
    save_summary(conn, 1, _summary_json(), "m", "b")     # ep 1 -> summarized

    assert [it.title for it in list_episodes(conn, limit=10, status="summarized")] == ["Done"]
    assert count_episodes(conn, status="summarized") == 1
    assert len(list_episodes(conn, limit=10, status="all")) == 2
    assert len(list_episodes(conn, limit=10)) == 2        # None == no filter


def test_list_episodes_tag_filter_or_semantics(conn):
    fin = upsert_podcast(conn, "Fin", None, "https://e/fin.xml")
    tech = upsert_podcast(conn, "Tech", None, "https://e/tech.xml")
    sport = upsert_podcast(conn, "Sport", None, "https://e/sport.xml")
    for p in (fin, tech, sport):
        subscribe(conn, p)
    set_podcast_tags(conn, fin, ["Finance"])
    set_podcast_tags(conn, tech, ["Technology"])
    set_podcast_tags(conn, sport, ["Sports"])
    upsert_episode(conn, fin, feed_ep("f", title="FinEp"))
    upsert_episode(conn, tech, feed_ep("t", title="TechEp"))
    upsert_episode(conn, sport, feed_ep("s", title="SportEp"))

    # single tag, case-insensitive (tags.name is COLLATE NOCASE)
    assert [it.title for it in list_episodes(conn, limit=10, tags=["finance"])] == ["FinEp"]
    # OR across two tags
    multi = {it.title for it in list_episodes(conn, limit=10, tags=["Finance", "Technology"])}
    assert multi == {"FinEp", "TechEp"}
    assert count_episodes(conn, tags=["Finance", "Technology"]) == 2
    # unknown tag -> empty, not an error
    assert list_episodes(conn, limit=10, tags=["Nonexistent"]) == []
    # 'all' sentinel and [] disable the filter
    assert len(list_episodes(conn, limit=10, tags=["all"])) == 3
    assert len(list_episodes(conn, limit=10, tags=[])) == 3


def test_list_episodes_subscribed_and_podcast_scope(conn):
    sub = upsert_podcast(conn, "Sub", None, "https://e/sub.xml")
    subscribe(conn, sub)
    unsub = upsert_podcast(conn, "Unsub", None, "https://e/unsub.xml")
    upsert_episode(conn, sub, feed_ep("s", title="FromSub"))
    upsert_episode(conn, unsub, feed_ep("u", title="FromUnsub"))

    assert [it.title for it in list_episodes(conn, limit=10)] == ["FromSub"]
    everything = {it.title for it in list_episodes(conn, limit=10, subscribed_only=False)}
    assert everything == {"FromSub", "FromUnsub"}
    # podcast_id scopes to one show regardless of subscription
    only = list_episodes(conn, limit=10, podcast_id=unsub, subscribed_only=False)
    assert [it.title for it in only] == ["FromUnsub"]
    assert count_episodes(conn, podcast_id=unsub, subscribed_only=False) == 1


def test_list_episodes_include_summary_carries_raw_json(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("e", title="E"))
    save_summary(conn, 1, _summary_json(), "m", "b")

    row = list_episodes(conn, limit=10, include_summary=True)[0]
    assert row.has_summary and row.summary_data is not None
    assert json.loads(row.summary_data)["summary"] == "A talk about copper."


def test_pagination_windows_disjoint(conn):
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    for i in range(25):
        upsert_episode(conn, pid, feed_ep(f"e{i}", title=f"E{i}"))
    conn.execute("UPDATE episodes SET published_at = '2026-04-01T00:00:00'")

    p1 = list_episodes(conn, limit=10, offset=0)
    p2 = list_episodes(conn, limit=10, offset=10)
    p3 = list_episodes(conn, limit=10, offset=20)
    assert [len(p1), len(p2), len(p3)] == [10, 10, 5]
    ids = [it.id for it in p1 + p2 + p3]
    assert len(set(ids)) == 25
    assert ids == sorted(ids, reverse=True)          # stable id DESC tiebreak


def test_get_podcasts_tag_filter_and_pagination(conn):
    fin = upsert_podcast(conn, "Fin", None, "https://e/fin.xml")
    tech = upsert_podcast(conn, "Tech", None, "https://e/tech.xml")
    subscribe(conn, fin)
    subscribe(conn, tech)
    set_podcast_tags(conn, fin, ["Finance"])
    set_podcast_tags(conn, tech, ["Technology"])

    finance = get_podcasts(conn, tags=["Finance"])
    assert [p.title for p in finance] == ["Fin"]
    assert finance[0].topics == ["Finance"]            # topics attached
    assert count_podcasts(conn, tags=["Finance"]) == 1
    assert count_podcasts(conn) == 2
    # pagination
    page = get_podcasts(conn, limit=1, offset=1)
    assert [p.title for p in page] == ["Tech"]         # ORDER BY title, second row


# --- route layer -------------------------------------------------------------


def _seed(db_path: str) -> None:
    conn = get_connection(db_path)
    init_db(conn)
    fin = upsert_podcast(conn, "Odd Lots", None, "https://e/odd.xml")
    subscribe(conn, fin)
    set_podcast_tags(conn, fin, ["Finance"])
    unsub = upsert_podcast(conn, "Unsubbed", None, "https://e/uns.xml")
    upsert_episode(conn, fin, feed_ep("ready", title="Copper Squeeze"))
    upsert_episode(conn, fin, feed_ep("pending", title="Not Yet"))
    upsert_episode(conn, unsub, feed_ep("u", title="Off Feed"))
    # Transcribe then summarize — pipeline order. save_transcript sets status
    # 'transcribed'; save_summary then advances it to 'summarized'.
    save_transcript(conn, 1, "full transcript text", "whisper")
    save_summary(conn, 1, _summary_json(), "model-x", "backend-y")  # ep 1 -> summarized
    conn.commit()
    conn.close()


def _client(tmp_path) -> TestClient:
    db_path = str(tmp_path / "api.db")
    _seed(db_path)
    return TestClient(create_app(Config(db_path=db_path)))


def test_route_list_episodes_default_subscribed(tmp_path):
    with _client(tmp_path) as client:
        r = client.get("/api/v1/episodes")
        assert r.status_code == 200
        body = r.json()
        titles = {it["title"] for it in body["items"]}
        assert "Copper Squeeze" in titles and "Not Yet" in titles
        assert "Off Feed" not in titles                 # unsubscribed excluded by default
        assert body["total"] == 2
        assert body["limit"] == 50 and body["offset"] == 0
        ep = next(it for it in body["items"] if it["title"] == "Copper Squeeze")
        assert ep["has_summary"] and ep["has_transcript"]
        assert ep["summary"] is None                    # not embedded without include


def test_route_status_filter_and_validation(tmp_path):
    with _client(tmp_path) as client:
        only = client.get("/api/v1/episodes", params={"status": "summarized"}).json()
        assert [it["title"] for it in only["items"]] == ["Copper Squeeze"]
        assert client.get("/api/v1/episodes", params={"status": "bogus"}).status_code == 422


def test_route_tag_filter(tmp_path):
    with _client(tmp_path) as client:
        hit = client.get("/api/v1/episodes", params={"tag": "Finance"}).json()
        assert {it["title"] for it in hit["items"]} == {"Copper Squeeze", "Not Yet"}
        miss = client.get("/api/v1/episodes", params={"tag": "Sports"}).json()
        assert miss["items"] == [] and miss["total"] == 0


def test_route_subscribed_false_includes_unsubbed(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/api/v1/episodes", params={"subscribed": "false"}).json()
        assert "Off Feed" in {it["title"] for it in body["items"]}


def test_route_include_summary_embeds_parsed(tmp_path):
    with _client(tmp_path) as client:
        body = client.get(
            "/api/v1/episodes", params={"status": "summarized", "include": "summary"},
        ).json()
        ep = body["items"][0]
        assert ep["summary"]["summary"] == "A talk about copper."
        assert ep["summary"]["chapters"][0]["title"] == "Intro"
        # normalized shape: highlights present, legacy keys gone
        assert "highlights" in ep["summary"]
        assert "insights" not in ep["summary"] and "speaker_takes" not in ep["summary"]


def test_route_include_summary_lowers_limit_cap(tmp_path):
    with _client(tmp_path) as client:
        no_embed = client.get("/api/v1/episodes", params={"limit": 999}).json()
        assert no_embed["limit"] == 200                 # MAX_LIMIT
        embed = client.get("/api/v1/episodes", params={"limit": 999, "include": "summary"}).json()
        assert embed["limit"] == 50                     # SUMMARY_MAX_LIMIT


def test_route_legacy_summary_migrates_to_highlights(tmp_path):
    """A pre-consolidation summary (insights/speaker_takes, no highlights) is
    migrated into highlights on read — the API never exposes the old shape."""
    db_path = str(tmp_path / "legacy.db")
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("e", title="Legacy"))
    legacy = _summary_json(
        insights=[Insight(text="point", timestamp="00:01:00", speaker="Tracy")],
        speaker_takes=[SpeakerTake(speaker="Joe", take="hot take", timestamp="00:02:00")],
    )
    save_summary(conn, 1, legacy, "m", "b")
    conn.commit()
    conn.close()

    with TestClient(create_app(Config(db_path=db_path))) as client:
        body = client.get("/api/v1/episodes/1/summary").json()
        texts = {h["text"] for h in body["highlights"]}
        assert texts == {"point", "hot take"}
        kinds = {h["kind"] for h in body["highlights"]}
        assert kinds == {"takeaway", "opinion"}


def test_route_invalid_include_is_422(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/v1/episodes", params={"include": "summaries"}).status_code == 422
        assert client.get("/api/v1/episodes", params={"include": "Summary"}).status_code == 422
        assert client.get("/api/v1/episodes", params={"include": "summary"}).status_code == 200


def test_route_summary_excludes_ad_speakers(tmp_path):
    """Ad/sponsor voices the diarizer picks up are filtered from API summaries,
    matching the web episode page."""
    db_path = str(tmp_path / "ads.db")
    conn = get_connection(db_path)
    init_db(conn)
    pid = upsert_podcast(conn, "P", None, "https://e/f.xml")
    subscribe(conn, pid)
    upsert_episode(conn, pid, feed_ep("e", title="E"))
    summary = _summary_json(speakers=[
        SpeakerIdentification(
            label="SPEAKER_00", name="Tracy", role="host",
            evidence_timestamp="00:00:01", evidence_quote="hi",
        ),
        SpeakerIdentification(
            label="SPEAKER_01", name="Acme Corp", role="sponsor",
            evidence_timestamp="00:05:00", evidence_quote="buy now",
        ),
    ])
    save_summary(conn, 1, summary, "m", "b")
    conn.commit()
    conn.close()

    with TestClient(create_app(Config(db_path=db_path))) as client:
        body = client.get("/api/v1/episodes/1/summary").json()
        assert {s["name"] for s in body["speakers"]} == {"Tracy"}   # sponsor dropped


def test_route_episode_detail_and_404(tmp_path):
    with _client(tmp_path) as client:
        ok = client.get("/api/v1/episodes/1")
        assert ok.status_code == 200
        body = ok.json()
        assert body["title"] == "Copper Squeeze"
        assert body["topics"] == ["Finance"]            # inherited from the show
        assert body["has_summary"] and body["has_transcript"]
        assert body["audio_url"]                         # full metadata, not the list subset
        assert "local_path" not in body                  # server filesystem path never exposed
        assert client.get("/api/v1/episodes/9999").status_code == 404


def test_route_summary_and_transcript_endpoints(tmp_path):
    with _client(tmp_path) as client:
        s = client.get("/api/v1/episodes/1/summary")
        assert s.status_code == 200 and s.json()["summary"] == "A talk about copper."
        t = client.get("/api/v1/episodes/1/transcript")
        assert t.status_code == 200 and t.json()["text"] == "full transcript text"
        # episode 2 has neither artifact
        assert client.get("/api/v1/episodes/2/summary").status_code == 404
        assert client.get("/api/v1/episodes/2/transcript").status_code == 404
        # missing episode
        assert client.get("/api/v1/episodes/9999/summary").status_code == 404


def test_route_podcasts_tags_version(tmp_path):
    with _client(tmp_path) as client:
        pods = client.get("/api/v1/podcasts").json()
        assert [p["title"] for p in pods["items"]] == ["Odd Lots"]   # subscribed only
        assert pods["items"][0]["topics"] == ["Finance"]
        assert pods["items"][0]["feed_url"]                          # feed_url kept
        assert "artwork_path" not in pods["items"][0]                # server path dropped
        assert client.get("/api/v1/podcasts", params={"subscribed": "false"}).json()["total"] == 2
        assert client.get("/api/v1/podcasts/1").json()["title"] == "Odd Lots"
        assert client.get("/api/v1/podcasts/9999").status_code == 404

        assert client.get("/api/v1/tags").json()["tags"] == ["Finance"]

        ver = client.get("/api/v1/version").json()
        assert ver["schema_version"] == "v1"
        assert "podracer_version" in ver


def test_docs_served_under_api_prefix_only(tmp_path):
    with _client(tmp_path) as client:
        # Root docs are off — they would otherwise also document the HTML routes.
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        # The API docs live under /api/v1, and the schema is API-only.
        assert client.get("/api/v1/docs").status_code == 200
        schema = client.get("/api/v1/openapi.json")
        assert schema.status_code == 200
        paths = schema.json()["paths"]
        assert paths and all(p.startswith("/api/v1") for p in paths)
