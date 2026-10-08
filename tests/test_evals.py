"""The eval harness, offline: dataset loading and label seeding, each step's
structural metrics and frozen-input handling (chat layer mocked), the judge's
parsing/validation, usage attribution, and the run -> results -> compare path."""
import json
from pathlib import Path

import pytest

from podracer import summarize
from podracer.evals import EVALS
from podracer.evals.dataset import load_dataset, write_case
from podracer.evals.judge import RUBRICS, JudgeError, judge, judge_blocks
from podracer.evals.report import compare_text, summarize_run
from podracer.evals.runner import RunConfig, Usage, run
from podracer.evals.steps import STEPS, FrozenInputsMissing, _sample_indices
from podracer.summarize import Backend, ChatResult

BACKEND = Backend.anthropic("claude-haiku-5-5", api_key="k")
JUDGE = Backend.anthropic("claude-opus-5-5", api_key="k")

TRANSCRIPT = "\n".join(
    f"[{m // 60:02d}:{m % 60:02d}:00] [SPEAKER_0{m % 2}] Line {m} of the show, "
    f"with a substantive remark about topic {m // 10}."
    for m in range(0, 90)
)
PROD_SUMMARY = {
    "summary": "A prod summary. " * 20,
    "speakers": [
        {"label": "SPEAKER_00", "name": "Host Person", "role": "host",
         "evidence_timestamp": "00:00:00", "evidence_quote": "I'm the host"},
        {"label": "SPEAKER_01", "name": "Guest Person", "role": "guest",
         "evidence_timestamp": "00:01:00", "evidence_quote": "thanks for having me"},
    ],
    "chapters": [
        {"title": "Teaser", "timestamp": "00:00:00", "summary": "clips"},
        {"title": "Topic zero", "timestamp": "00:02:00", "summary": "prod enrichment " * 40},
        {"title": "Topic one", "timestamp": "00:30:00", "summary": "prod enrichment " * 40},
        {"title": "Topic two", "timestamp": "01:00:00", "summary": "prod enrichment " * 40},
    ],
    "highlights": [],
}

SPEAKERS_OUT = {"speakers": [
    {"label": "SPEAKER_00", "name": "Host Person", "role": "host",
     "evidence_timestamp": "00:00:00", "evidence_quote": "q"},
    {"label": "SPEAKER_01", "name": "Guest Persson", "role": "guest",
     "evidence_timestamp": "00:01:00", "evidence_quote": "q"},
]}
SUMMARY_OUT = {"summary": ("A real multi-paragraph summary of the episode. " * 8).strip()}
CHAPTERS_OUT = {"chapters": [
    {"title": "Teaser", "timestamp": "00:00:00", "summary": "clips"},
    {"title": "Opening", "timestamp": "00:02:00", "summary": "s"},
    {"title": "Middle", "timestamp": "00:40:00", "summary": "s"},
    {"title": "Close", "timestamp": "01:20:00", "summary": "s"},
]}
HIGHLIGHTS_OUT = {"highlights": [
    {"text": f"A complete, substantive highlight number {i} that a listener would remember.",
     "timestamp": f"00:{i * 10:02d}:00" if i < 6 else f"01:{(i - 6) * 10:02d}:00",
     "speaker": "Host Person" if i % 2 else "Guest Person", "kind": "takeaway" if i % 3 else "opinion"}
    for i in range(9)
]}
DETAIL_OUT = {"summary": ("This chapter walks through the argument in real depth. " * 12).strip()}
VERDICT = lambda dims: {  # noqa: E731
    "scores": [{"dimension": d, "score": 4, "rationale": "fine"} for d in dims],
    "issues": ["one unsupported number"], "overall": 4,
}


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    root = tmp_path / "eval"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"episodes": [{"id": 7, "tags": ["short"]}]}))
    write_case(root, 7, {"transcript": TRANSCRIPT, "show_notes": "Guests: Guest Person",
                         "podcast_description": "A show", "summary": PROD_SUMMARY,
                         "episode_title": "ep", "podcast_title": "show"})
    return root


def _fake_chat(responses: dict[str, dict]):
    """Route by system prompt: generation prompts and the judge prompt."""
    def chat(backend, system, user, schema, repair=False, ignore_providers=None):
        for key, payload in responses.items():
            if key in system:
                if callable(payload):
                    payload = payload(user)
                return ChatResult(content=json.dumps(payload), finish_reason="stop",
                                  input_tokens=1000, output_tokens=100)
        raise AssertionError(f"unexpected system prompt: {system[:60]}")
    return chat


def _judge_payload(user):
    task = user[1]["text"] if isinstance(user, list) else user
    dims = [line[2:] for line in task.splitlines() if line.startswith("- ")]
    return VERDICT(dims)


ALL = {
    summarize.SPEAKER_ID_PROMPT[:40]: SPEAKERS_OUT,
    summarize.SUMMARY_PROMPT[:40]: SUMMARY_OUT,
    summarize.CHAPTERS_PROMPT[:40]: CHAPTERS_OUT,
    summarize.CHAPTER_DETAIL_PROMPT[:40]: DETAIL_OUT,
    summarize.HIGHLIGHTS_PROMPT[:40]: HIGHLIGHTS_OUT,
    "strict, calibrated evaluator": _judge_payload,
}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(summarize.time, "sleep", lambda *a, **k: None)


# --- dataset ---------------------------------------------------------------

def test_dataset_loads_and_seeds_labels(dataset):
    cases = load_dataset(dataset)
    assert len(cases) == 1
    c = cases[0]
    assert c.episode_id == 7 and c.tags == ["short"]
    assert c.prod_summary is not None and len(c.prod_summary.chapters) == 4
    assert c.labels["seeded_from"] == "prod_summary" and c.labels["verified"] is False
    assert {s["name"] for s in c.labels["speakers"]} == {"Host Person", "Guest Person"}
    with pytest.raises(ValueError, match="not in manifest"):
        load_dataset(dataset, ids=[99])


def test_missing_case_file_points_at_fetch(tmp_path):
    root = tmp_path / "eval"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"episodes": [{"id": 1}]}))
    with pytest.raises(FileNotFoundError, match="fetch"):
        load_dataset(root)


# --- steps -----------------------------------------------------------------

def test_every_eval_has_a_step_and_a_rubric():
    assert set(EVALS) == set(STEPS) == set(RUBRICS)


def test_speakers_step_scores_against_labels(dataset, monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat", _fake_chat(ALL))
    [item] = STEPS["speakers"](BACKEND, load_dataset(dataset)[0])
    s = item.structural
    assert s["n_speakers"] == 2 and s["label_coverage"] == 1.0
    # "Guest Persson" is a misspelling: one of two names right, label map 1/2.
    assert s["name_precision"] == 0.5 and s["name_recall"] == 0.5 and s["label_accuracy"] == 0.5
    assert item.passed and "SPEAKER KEY" in item.output_text
    assert item.source.startswith("PODCAST DESCRIPTION:")


def test_downstream_steps_use_frozen_prod_speakers(dataset, monkeypatch):
    seen = {}

    def chat(backend, system, user, schema, repair=False, ignore_providers=None):
        seen["user"] = user
        return _fake_chat(ALL)(backend, system, user, schema)

    monkeypatch.setattr(summarize, "_dispatch_chat", chat)
    [item] = STEPS["summary"](BACKEND, load_dataset(dataset)[0])
    # Transcript labels were rewritten with prod's names before the call.
    assert "[Host Person]" in seen["user"] and "SPEAKER_00" not in seen["user"]
    assert item.passed and item.structural["words"] > 50


def test_chapters_and_highlights_structural_metrics(dataset, monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat", _fake_chat(ALL))
    case = load_dataset(dataset)[0]
    [ch] = STEPS["chapters"](BACKEND, case)
    assert ch.structural["n_chapters"] == 4 and ch.structural["has_teaser"]
    assert ch.structural["coverage_ratio"] == pytest.approx(80 / 89)
    assert ch.passed
    [hl] = STEPS["highlights"](BACKEND, case)
    assert hl.structural["n_highlights"] == 9
    assert hl.structural["attribution_known"] == 1.0
    assert hl.structural["decile_coverage"] >= 0.8
    assert hl.structural["n_opinion"] == 3
    assert hl.passed


def test_chapter_detail_skips_teaser_and_caps_chapters(dataset, monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat", _fake_chat(ALL))
    case = load_dataset(dataset)[0]
    items = STEPS["chapter_detail"](BACKEND, case)
    assert [i.meta["chapter_index"] for i in items] == [1, 2, 3]  # teaser (0) skipped
    assert all(i.passed for i in items)
    assert all(i.structural["slice_chars"] > 0 for i in items)
    capped = STEPS["chapter_detail"](BACKEND, case, chapters_per_episode=2)
    assert [i.meta["chapter_index"] for i in capped] == [1, 3]


def test_sample_indices_are_evenly_spaced():
    assert _sample_indices(10, None) == list(range(10))
    assert _sample_indices(10, 3) == [0, 4, 9]
    assert _sample_indices(2, 5) == [0, 1]
    assert _sample_indices(10, 1) == [5]


def test_downstream_step_needs_prod_summary(tmp_path):
    root = tmp_path / "eval"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"episodes": [{"id": 1}]}))
    write_case(root, 1, {"transcript": TRANSCRIPT, "summary": None})
    case = load_dataset(root)[0]
    with pytest.raises(FrozenInputsMissing):
        STEPS["summary"](BACKEND, case)


# --- judge -----------------------------------------------------------------

def test_judge_blocks_cache_the_source():
    blocks = judge_blocks("SRC", "INSTR", "CAND", ["a", "b"])
    assert blocks[0]["cache_control"] == {"type": "ephemeral"} and "SRC" in blocks[0]["text"]
    assert "- a\n- b" in blocks[1]["text"] and "CAND" in blocks[1]["text"]


def test_judge_validates_dimensions_and_clamps(monkeypatch):
    calls = []

    def chat(backend, system, user, schema, repair=False, ignore_providers=None):
        calls.append(system)
        if len(calls) == 1:
            return ChatResult(content="not json")
        if len(calls) == 2:
            return ChatResult(content=json.dumps(VERDICT(["faithfulness"])))  # missing dims
        payload = VERDICT(list(RUBRICS["summary"]))
        payload["scores"][0]["score"] = 9
        payload["overall"] = 0
        return ChatResult(content=json.dumps(payload), input_tokens=50, output_tokens=5)

    monkeypatch.setattr(summarize, "_dispatch_chat", chat)
    v = judge(JUDGE, "summary", "src", "instr", "cand")
    assert len(calls) == 3
    assert set(v.scores) == set(RUBRICS["summary"])
    assert v.scores["faithfulness"] == 5 and v.overall == 1
    assert v.issues == ["one unsupported number"] and v.input_tokens == 50
    assert "faithfulness:" in calls[0]  # rubric is in the system prompt


def test_judge_gives_up_after_three_bad_answers(monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat",
                        lambda *a, **k: ChatResult(content="{}"))
    with pytest.raises(JudgeError):
        judge(JUDGE, "summary", "src", "instr", "cand")


# --- runner + report -------------------------------------------------------

def test_run_writes_results_and_compare_reads_them(dataset, monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat", _fake_chat(ALL))
    out = dataset / "runs" / "summary" / "test"
    run(RunConfig(eval="summary", backend=BACKEND, judge=JUDGE, dataset=dataset, out=out, reps=2, workers=2))
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and {r["rep"] for r in rows} == {0, 1}
    row = rows[0]
    assert row["status"] == "ok" and row["passed"] and row["overall"] == 4
    assert set(row["judge"]) == set(RUBRICS["summary"])
    assert row["gen"] == {"calls": 1, "input_tokens": 1000, "output_tokens": 100}
    assert row["judge_usage"]["calls"] == 1 and row["judge_usage"]["input_tokens"] == 1000
    outputs = sorted(p.name for p in (out / "outputs").iterdir())
    assert outputs == ["7_rep0.json", "7_rep1.json"]
    saved = json.loads((out / "outputs" / "7_rep0.json").read_text())
    assert saved["judge"]["rationales"]["faithfulness"] == "fine"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["generator"]["model"] == "claude-haiku-5-5" and meta["judge"]["model"] == "claude-opus-5-5"
    assert "finished_at" in meta

    s = summarize_run(out)
    assert s["items"] == 2 and s["errors"] == 0 and s["structural_pass_rate"] == 1.0
    assert s["overall"]["mean"] == 4.0 and s["judge"]["coverage"]["mean"] == 4.0
    assert s["gen_tokens_per_episode"] == {"input": 1000.0, "output": 100.0}
    assert s["gen_cost_per_episode_usd"] == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
    text = compare_text([out], show_episodes=True)
    assert "claude-haiku-5-5 (low)" in text and "4.00" in text and "overall by episode" in text


def test_run_records_failures_instead_of_dying(dataset, monkeypatch):
    def chat(backend, system, user, schema, repair=False, ignore_providers=None):
        return ChatResult(content="prose, not json", input_tokens=10, output_tokens=1)

    monkeypatch.setattr(summarize, "_dispatch_chat", chat)
    out = dataset / "runs" / "summary" / "fail"
    run(RunConfig(eval="summary", backend=BACKEND, judge=None, dataset=dataset, out=out, reps=1))
    [row] = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert row["status"] == "error" and row["failure_class"] == "degenerate"
    assert row["gen"]["calls"] == 3  # all retries counted
    s = summarize_run(out)
    assert s["error_rate"] == 1.0 and s["failure_classes"] == {"degenerate": 1}


def test_chapter_detail_run_attributes_usage_across_pool_threads(dataset, monkeypatch):
    monkeypatch.setattr(summarize, "_dispatch_chat", _fake_chat(ALL))
    out = dataset / "runs" / "chapter_detail" / "test"
    run(RunConfig(eval="chapter_detail", backend=BACKEND, judge=None, dataset=dataset, out=out, reps=1))
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [r["item"] for r in rows] == ["c01", "c02", "c03"]
    # Three chapter calls made in pool threads all land on the case's sink.
    assert rows[0]["gen"]["calls"] == 3 and rows[1]["gen"] is None


def test_usage_add_tolerates_missing_counts():
    u = Usage()
    u.add(ChatResult(content="{}"))
    u.add(ChatResult(content="{}", input_tokens=5, output_tokens=2))
    assert u.as_dict() == {"calls": 2, "input_tokens": 5, "output_tokens": 2}
