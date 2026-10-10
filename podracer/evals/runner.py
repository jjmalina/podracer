"""Run one eval for one model over the dataset, N times, and write the results.

Run directory layout::

    meta.json                 what ran: eval, backend/model/effort, judge, reps, ids, git rev
    results.jsonl             one row per (episode, rep, item): metrics, usage, judge scores
    outputs/<ep>_rep<k>[_<key>].json   the generated output + judge rationale/issues
    summary.json              aggregate written at the end (see report.summarize_run)

Token usage is captured through ``summarize.chat_observer``: each (case, rep)
task sets a ContextVar sink, so every completion made under it is attributed
to that task. Generation and judging use separate sinks so each is reported on
its own.
"""
import contextvars
import json
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from podracer import summarize
from podracer.evals import logger
from podracer.evals.dataset import EvalCase, load_dataset
from podracer.evals.judge import JudgeError, judge
from podracer.evals.steps import STEPS, FrozenInputsMissing, Item, _dump
from podracer.providers import ProviderNotAllowedError
from podracer.summarize import Backend, ChatResult, DegenerateOutputError


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # chapter_detail's pool threads all report into one case sink.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, r: ChatResult) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += r.input_tokens or 0
            self.output_tokens += r.output_tokens or 0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


def _observe(usage: Usage | None) -> None:
    """Route subsequent completions in this context to ``usage`` (None = off)."""
    summarize.chat_observer.set(usage.add if usage is not None else None)


@dataclass
class RunConfig:
    eval: str
    backend: Backend
    judge: Backend | None
    dataset: Path
    out: Path
    reps: int = 3
    ids: list[int] | None = None
    chapters_per_episode: int | None = None
    workers: int = 2
    label: str | None = None
    extra_meta: dict = field(default_factory=dict)


def _git_rev() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None


def _backend_meta(b: Backend | None) -> dict | None:
    if b is None:
        return None
    return {"backend": b.name, "model": b.model, "effort": b.effort, "providers": b.providers,
            "base_url": b.base_url}


def _failure_class(e: Exception) -> str:
    if isinstance(e, FrozenInputsMissing):
        return "no_frozen_inputs"
    if isinstance(e, DegenerateOutputError):
        return "degenerate"
    if isinstance(e, ProviderNotAllowedError):
        return "provider"
    if isinstance(e, JudgeError):
        return "judge"
    if isinstance(e, httpx.HTTPError):
        return "http"
    return type(e).__name__


def _run_one(cfg: RunConfig, case: EvalCase, rep: int) -> tuple[list[dict], list[tuple[str, dict]]]:
    """Generate + judge one (case, rep). Returns (result rows, output files)."""
    base = {"eval": cfg.eval, "episode_id": case.episode_id, "tags": case.tags, "rep": rep,
            "backend": cfg.backend.name, "model": cfg.backend.model, "effort": cfg.backend.effort}
    gen = Usage()
    _observe(gen)
    t0 = time.monotonic()
    try:
        items: list[Item] = STEPS[cfg.eval](cfg.backend, case, chapters_per_episode=cfg.chapters_per_episode)
    except Exception as e:  # one failed case must not sink the run
        latency = time.monotonic() - t0
        logger.warning("eval_case_failed", episode_id=case.episode_id, rep=rep, error=str(e),
                       failure_class=_failure_class(e))
        row = {**base, "item": None, "status": "error", "failure_class": _failure_class(e), "error": str(e),
               "latency_s": round(latency, 2), "gen": gen.as_dict(), "structural": {}, "passed": False,
               "judge": None, "overall": None, "issues": [], "traceback": traceback.format_exc()}
        return [row], []
    finally:
        _observe(None)
    gen_latency = time.monotonic() - t0
    if not items:  # e.g. chapter_detail on a prod summary with no enrichable chapters
        logger.warning("eval_case_empty", episode_id=case.episode_id, rep=rep)
        row = {**base, "item": None, "status": "error", "failure_class": "no_items",
               "error": "the case produced no items to grade", "latency_s": round(gen_latency, 2),
               "gen": gen.as_dict(), "structural": {}, "passed": False, "judge": None, "overall": None,
               "issues": []}
        return [row], []

    rows: list[dict] = []
    files: list[tuple[str, dict]] = []
    per_item_latency = gen_latency / max(1, len(items))  # chapter_detail items ran in a pool
    for item in items:
        name = f"{case.episode_id}_rep{rep}" + (f"_{item.key}" if item.key else "")
        row = {**base, "item": item.key or None, "status": "ok", "failure_class": None, "error": None,
               "latency_s": round(per_item_latency, 2), "gen": gen.as_dict() if not item.key else None,
               "structural": item.structural, "passed": item.passed, **item.meta,
               "judge": None, "overall": None, "issues": []}
        out = {"episode_id": case.episode_id, "rep": rep, "item": item.key, "output": _dump(item.output),
               "output_text": item.output_text, "structural": item.structural, "passed": item.passed, **item.meta}
        if item.error is not None:
            row.update(status="error", failure_class=_failure_class(item.error), error=str(item.error))
            logger.warning("eval_item_failed", episode_id=case.episode_id, rep=rep, item=item.key,
                           error=str(item.error), failure_class=row["failure_class"])
        elif cfg.judge is not None:
            jud = Usage()
            _observe(jud)
            t1 = time.monotonic()
            try:
                verdict = judge(cfg.judge, cfg.eval, item.source, item.instructions, item.output_text)
                row["judge"] = verdict.scores
                row["overall"] = verdict.overall
                row["issues"] = verdict.issues
                out["judge"] = {"scores": verdict.scores, "overall": verdict.overall,
                                "issues": verdict.issues, "rationales": verdict.rationales}
            except Exception as e:
                row["status"] = "judge_error"
                row["failure_class"] = _failure_class(e)
                row["error"] = str(e)
                logger.warning("eval_judge_failed", episode_id=case.episode_id, rep=rep, item=item.key,
                               error=str(e))
            finally:
                _observe(None)
            row["judge_usage"] = {**jud.as_dict(), "latency_s": round(time.monotonic() - t1, 2)}
        rows.append(row)
        files.append((name, out))
    if cfg.eval == "chapter_detail":
        # Per-chapter token split isn't tracked; attach the case total to the first row.
        rows[0]["gen"] = gen.as_dict()
    return rows, files


def run(cfg: RunConfig) -> Path:
    cases = load_dataset(cfg.dataset, cfg.ids)
    cfg.out.mkdir(parents=True, exist_ok=True)
    (cfg.out / "outputs").mkdir(exist_ok=True)
    meta = {
        "eval": cfg.eval, "label": cfg.label, "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_rev": _git_rev(), "generator": _backend_meta(cfg.backend), "judge": _backend_meta(cfg.judge),
        "reps": cfg.reps, "episode_ids": [c.episode_id for c in cases],
        "chapters_per_episode": cfg.chapters_per_episode, "dataset": str(cfg.dataset), **cfg.extra_meta,
    }
    (cfg.out / "meta.json").write_text(json.dumps(meta, indent=1))
    results_path = cfg.out / "results.jsonl"
    results_path.write_text("")
    write_lock = threading.Lock()
    # Case-major so the reps of one episode run back to back and the judge's
    # cached transcript prefix is still warm for them.
    tasks = [(case, rep) for case in cases for rep in range(cfg.reps)]
    logger.info("eval_run_start", eval=cfg.eval, model=cfg.backend.model, cases=len(cases), reps=cfg.reps,
                judge=cfg.judge.model if cfg.judge else None, out=str(cfg.out))

    def task(case: EvalCase, rep: int) -> None:
        rows, files = _run_one(cfg, case, rep)
        with write_lock:
            with results_path.open("a") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            for name, payload in files:
                (cfg.out / "outputs" / f"{name}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
        done = [r for r in rows if r["status"] == "ok"]
        logger.info("eval_case_done", episode_id=case.episode_id, rep=rep, items=len(rows),
                    ok=len(done), overall=[r["overall"] for r in done][:5])

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as ex:
        futures = [ex.submit(contextvars.copy_context().run, task, case, rep) for case, rep in tasks]
        for f in futures:
            f.result()

    meta["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (cfg.out / "meta.json").write_text(json.dumps(meta, indent=1))
    return cfg.out
