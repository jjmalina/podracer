"""Aggregate a run directory and compare several side by side."""
import json
import math
import statistics
from pathlib import Path

# $/MTok (input, output). First-party list prices; OpenRouter models use the
# median of the US providers podracer routes to. Cache discounts are ignored,
# so judge cost is an upper bound. Unknown models get no cost column.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-1": (10.00, 50.00),
    "deepseek/deepseek-v4-flash": (0.12, 0.24),
    # The same Claude models routed through OpenRouter (list price there too).
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "anthropic/claude-opus-5.5": (4.00, 20.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1e6


def load_rows(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_meta(run_dir: Path) -> dict:
    path = run_dir / "meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _ci95(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))


def summarize_run(run_dir: Path) -> dict:
    """Headline numbers for one run: error/pass rates, judge means with a 95%
    CI over items, latency, tokens and cost per episode."""
    meta = load_meta(run_dir)
    rows = load_rows(run_dir)
    # "ok" = generated and judged; "judge_error" = generated, judge failed. Both
    # carry valid structural metrics; only generation failures are errors.
    ok = [r for r in rows if r["status"] in ("ok", "judge_error")]
    judged = [r for r in ok if r.get("overall") is not None]
    gen_model = (meta.get("generator") or {}).get("model", "?")
    judge_model = (meta.get("judge") or {}).get("model")
    dims: dict[str, list[float]] = {}
    for r in judged:
        for d, s in (r.get("judge") or {}).items():
            dims.setdefault(d, []).append(s)
    overall = [r["overall"] for r in judged]
    # Usage: gen is attached per episode-rep (chapter_detail puts it on the first row).
    gen_rows = [r for r in rows if r.get("gen")]
    episodes_reps = len({(r["episode_id"], r["rep"]) for r in rows}) or 1
    in_tok = sum(r["gen"]["input_tokens"] for r in gen_rows)
    out_tok = sum(r["gen"]["output_tokens"] for r in gen_rows)
    judge_in = sum((r.get("judge_usage") or {}).get("input_tokens", 0) for r in rows)
    judge_out = sum((r.get("judge_usage") or {}).get("output_tokens", 0) for r in rows)
    gen_cost = cost_usd(gen_model, in_tok, out_tok)
    judge_cost = cost_usd(judge_model, judge_in, judge_out) if judge_model else None
    structural_keys = sorted({k for r in ok for k, v in r.get("structural", {}).items()
                              if isinstance(v, (int, float)) and not isinstance(v, bool)})
    structural = {k: _mean([r["structural"][k] for r in ok if k in r["structural"]]) for k in structural_keys}
    return {
        "run": run_dir.name, "path": str(run_dir), "eval": meta.get("eval"), "label": meta.get("label"),
        "model": gen_model, "effort": (meta.get("generator") or {}).get("effort"), "judge_model": judge_model,
        "reps": meta.get("reps"), "episodes": len(meta.get("episode_ids") or []),
        "items": len(rows), "ok": len(ok), "errors": len(rows) - len(ok),
        "error_rate": (len(rows) - len(ok)) / len(rows) if rows else None,
        "failure_classes": _counts([r.get("failure_class") for r in rows if r["status"] == "error"]),
        "judge_errors": sum(1 for r in rows if r["status"] == "judge_error"),
        "structural_pass_rate": (sum(1 for r in ok if r.get("passed")) / len(ok)) if ok else None,
        "judge": {d: {"mean": _mean(v), "ci95": _ci95(v), "n": len(v)} for d, v in dims.items()},
        "overall": {"mean": _mean(overall), "ci95": _ci95(overall), "n": len(overall)},
        "issues_per_item": _mean([len(r.get("issues") or []) for r in judged]),
        "latency_p50_s": statistics.median([r["latency_s"] for r in ok]) if ok else None,
        "gen_tokens_per_episode": {"input": in_tok / episodes_reps, "output": out_tok / episodes_reps},
        "gen_cost_per_episode_usd": (gen_cost / episodes_reps) if gen_cost is not None else None,
        "judge_cost_per_episode_usd": (judge_cost / episodes_reps) if judge_cost is not None else None,
        "structural": structural,
    }


def _counts(xs: list) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def by_episode(run_dir: Path) -> dict[int, dict]:
    """Per-episode means (over reps and items) of overall and structural pass."""
    rows = [r for r in load_rows(run_dir) if r["status"] in ("ok", "judge_error")]
    out: dict[int, dict] = {}
    for ep in sorted({r["episode_id"] for r in rows}):
        rs = [r for r in rows if r["episode_id"] == ep]
        ov = [r["overall"] for r in rs if r.get("overall") is not None]
        out[ep] = {"overall": _mean(ov), "n": len(rs), "pass_rate": sum(1 for r in rs if r["passed"]) / len(rs),
                   "tags": rs[0].get("tags", [])}
    return out


def _fmt(v, nd=2, suffix="") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join([line, sep, *body])


def compare_text(run_dirs: list[Path], show_episodes: bool = False) -> str:
    summaries = [summarize_run(d) for d in run_dirs]
    evals = {s["eval"] for s in summaries}
    dims: list[str] = []
    for s in summaries:
        for d in s["judge"]:
            if d not in dims:
                dims.append(d)
    headers = ["run", "model", "n", "err", "pass", *dims, "overall", "issues", "p50 s", "tok in/out",
               "$/ep", "judge $/ep"]
    rows = []
    for s in summaries:
        rows.append([
            s["run"][:40], f"{s['model']}" + (f" ({s['effort']})" if s["effort"] else ""),
            str(s["items"]), _fmt(s["error_rate"], 0, "%") if s["error_rate"] is None
            else f"{100 * s['error_rate']:.0f}%",
            "-" if s["structural_pass_rate"] is None else f"{100 * s['structural_pass_rate']:.0f}%",
            *[_fmt(s["judge"].get(d, {}).get("mean")) for d in dims],
            (f"{s['overall']['mean']:.2f}±{s['overall']['ci95']:.2f}" if s["overall"]["ci95"] is not None
             else _fmt(s["overall"]["mean"])),
            _fmt(s["issues_per_item"], 1), _fmt(s["latency_p50_s"], 0),
            f"{s['gen_tokens_per_episode']['input'] / 1000:.0f}k/{s['gen_tokens_per_episode']['output'] / 1000:.1f}k",
            _fmt(s["gen_cost_per_episode_usd"], 3), _fmt(s["judge_cost_per_episode_usd"], 2),
        ])
    out = [f"eval: {', '.join(sorted(e for e in evals if e))}", _table(headers, rows)]
    if show_episodes:
        per = [by_episode(d) for d in run_dirs]
        eps = sorted({ep for p in per for ep in p})
        headers = ["episode", "tags", *[s["run"][:24] for s in summaries]]
        rows = [[str(ep), ",".join(next((p[ep]["tags"] for p in per if ep in p), []))[:28],
                 *[_fmt(p.get(ep, {}).get("overall")) for p in per]] for ep in eps]
        out += ["", "overall by episode:", _table(headers, rows)]
    return "\n".join(out)
