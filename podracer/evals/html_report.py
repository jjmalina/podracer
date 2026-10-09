"""Self-contained HTML page for reading eval runs side by side.

`compare` gives the numbers; this gives the outputs. One section per eval,
one block per episode, the runs as columns so each model's output for the
same (episode, rep, chapter) sits next to the others' with its judge scores,
issues and rationales. Written into eval/runs/ (gitignored: the page embeds
episode titles and generated text about real episodes).
"""
import html
import json
from pathlib import Path

from podracer.evals.report import load_meta, load_rows, summarize_run

_CSS = """
:root { --bg:#111; --panel:#1a1a1a; --line:#333; --fg:#ddd; --dim:#888; --accent:#e8a33d;
        --good:#6c6; --bad:#d66; }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font: 14px/1.45 system-ui, sans-serif; }
h1, h2, h3 { font-weight:600; margin:1.2em 0 .4em; }
h1 { font-size:20px; } h2 { font-size:17px; color:var(--accent); } h3 { font-size:15px; }
.mono { font-family: ui-monospace, monospace; }
table.sum { border-collapse:collapse; margin:8px 0 16px; font-size:13px; }
table.sum th, table.sum td { border:1px solid var(--line); padding:4px 8px; text-align:right; }
table.sum th:first-child, table.sum td:first-child { text-align:left; }
.ep { border:1px solid var(--line); border-radius:6px; margin:12px 0; background:var(--panel); }
.ep > summary { padding:8px 12px; cursor:pointer; }
.ep > summary .tags { color:var(--dim); margin-left:8px; font-size:12px; }
.item { padding:0 12px 12px; }
.item h3 { margin-top:12px; color:var(--dim); }
.cols { display:grid; grid-template-columns: repeat(var(--n), minmax(0, 1fr)); gap:10px; }
.col { border:1px solid var(--line); border-radius:4px; padding:8px; background:var(--bg); min-width:0; }
.col .hdr { display:flex; justify-content:space-between; align-items:baseline; gap:8px;
            border-bottom:1px solid var(--line); padding-bottom:6px; margin-bottom:6px; }
.col .hdr .model { font-weight:600; }
.score { font-size:18px; color:var(--accent); }
.dims { color:var(--dim); font-size:12px; }
.out { white-space:pre-wrap; overflow-wrap:anywhere; max-height:28em; overflow:auto;
       font-size:13px; padding:6px; border:1px dashed var(--line); border-radius:4px; }
.issues { margin:8px 0 0; padding-left:18px; font-size:12px; }
.issues li { margin:2px 0; }
details.rat { font-size:12px; margin-top:6px; }
details.rat summary { color:var(--dim); cursor:pointer; }
.rat dl { margin:4px 0; } .rat dt { color:var(--accent); } .rat dd { margin:0 0 4px 0; }
.err { color:var(--bad); } .pass { color:var(--good); } .fail { color:var(--bad); }
.meta { color:var(--dim); font-size:12px; }
.top { display:flex; gap:16px; flex-wrap:wrap; align-items:baseline; }
"""


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _episode_titles(dataset: Path) -> dict[int, str]:
    titles: dict[int, str] = {}
    for case in (dataset / "data").glob("*/case.json"):
        try:
            c = json.loads(case.read_text())
            titles[int(c["id"])] = f"{c.get('podcast_title', '')} — {c.get('episode_title', '')}"
        except (OSError, ValueError, KeyError):
            continue
    return titles


def _load_outputs(run_dir: Path) -> dict[tuple[int, int, str], dict]:
    out: dict[tuple[int, int, str], dict] = {}
    for f in (run_dir / "outputs").glob("*.json"):
        o = json.loads(f.read_text())
        out[(int(o["episode_id"]), int(o["rep"]), o.get("item") or "")] = o
    return out


def _summary_table(summaries: list[dict]) -> str:
    dims: list[str] = []
    for s in summaries:
        for d in s["judge"]:
            if d not in dims:
                dims.append(d)
    head = ["run", "model", "judge", "n", "err", "pass", *dims, "overall", "issues/item", "p50 s", "$/ep", "judge $/ep"]
    rows = []
    for s in summaries:
        ov = s["overall"]
        rows.append([
            s["run"], f"{s['model']}" + (f" ({s['effort']})" if s["effort"] else ""), s["judge_model"] or "-",
            s["items"], f"{100 * (s['error_rate'] or 0):.0f}%",
            "-" if s["structural_pass_rate"] is None else f"{100 * s['structural_pass_rate']:.0f}%",
            *[f"{s['judge'][d]['mean']:.2f}" if d in s["judge"] else "-" for d in dims],
            (f"{ov['mean']:.2f} ± {ov['ci95']:.2f}" if ov["mean"] is not None and ov["ci95"] is not None
             else ("-" if ov["mean"] is None else f"{ov['mean']:.2f}")),
            "-" if s["issues_per_item"] is None else f"{s['issues_per_item']:.1f}",
            "-" if s["latency_p50_s"] is None else f"{s['latency_p50_s']:.0f}",
            "-" if s["gen_cost_per_episode_usd"] is None else f"{s['gen_cost_per_episode_usd']:.3f}",
            "-" if s["judge_cost_per_episode_usd"] is None else f"{s['judge_cost_per_episode_usd']:.2f}",
        ])
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table class='sum'><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def _col(summary: dict, row: dict | None, out: dict | None) -> str:
    model = _e(summary["model"]) + (f" <span class='dim'>({_e(summary['effort'])})</span>" if summary["effort"] else "")
    if row is None:
        return (f"<div class='col'><div class='hdr'><span class='model'>{model}</span></div>"
                "<div class='meta'>no row</div></div>")
    if row["status"] == "error":
        return (f"<div class='col'><div class='hdr'><span class='model'>{model}</span>"
                f"<span class='err'>error: {_e(row.get('failure_class'))}</span></div>"
                f"<div class='out err'>{_e(row.get('error'))}</div></div>")
    judge = (out or {}).get("judge") or {}
    scores = judge.get("scores") or row.get("judge") or {}
    overall = judge.get("overall", row.get("overall"))
    passed = "<span class='pass'>pass</span>" if row.get("passed") else "<span class='fail'>FAIL check</span>"
    dims = " · ".join(f"{_e(k)} {_e(v)}" for k, v in scores.items())
    text = (out or {}).get("output_text") or (out or {}).get("output") or ""
    if not isinstance(text, str):
        text = json.dumps(text, indent=1, ensure_ascii=False)
    issues = judge.get("issues") or row.get("issues") or []
    rationales = judge.get("rationales") or {}
    structural = row.get("structural") or {}
    struct = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in structural.items())
    parts = [
        "<div class='col'>",
        f"<div class='hdr'><span class='model'>{model}</span><span>{passed} "
        f"<span class='score'>{_e(overall) if overall is not None else '-'}</span></span></div>",
        f"<div class='dims'>{dims}</div>" if dims else "",
        f"<div class='meta'>{_e(struct)} · {row.get('latency_s', 0):.0f}s</div>",
        f"<div class='out'>{_e(text)}</div>",
    ]
    if issues:
        parts.append("<ul class='issues'>" + "".join(f"<li>{_e(i)}</li>" for i in issues) + "</ul>")
    if rationales:
        dl = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rationales.items())
        parts.append(f"<details class='rat'><summary>judge rationale</summary><dl>{dl}</dl></details>")
    parts.append("</div>")
    return "".join(parts)


def build_html(run_dirs: list[Path], dataset: Path, title: str = "podracer eval report") -> str:
    summaries = [summarize_run(d) for d in run_dirs]
    metas = [load_meta(d) for d in run_dirs]
    rows_by_run = [{(r["episode_id"], r["rep"], r.get("item") or ""): r for r in load_rows(d)} for d in run_dirs]
    outs_by_run = [_load_outputs(d) for d in run_dirs]
    titles = _episode_titles(dataset)
    evals: list[str] = []
    for s in summaries:
        if s["eval"] and s["eval"] not in evals:
            evals.append(s["eval"])

    body = [f"<h1>{_e(title)}</h1>"]
    for ev in evals:
        idx = [i for i, s in enumerate(summaries) if s["eval"] == ev]
        body.append(f"<h2>{_e(ev)}</h2>")
        body.append(_summary_table([summaries[i] for i in idx]))
        judges = {(metas[i].get("judge") or {}).get("model") for i in idx}
        if len(judges) > 1:
            names = ", ".join(map(str, judges))
            body.append(f"<p class='err'>runs in this section used different judges: {_e(names)}</p>")
        keys = sorted({k for i in idx for k in rows_by_run[i]})
        episodes: list[int] = []
        for ep, _, _ in keys:
            if ep not in episodes:
                episodes.append(ep)
        for ep in episodes:
            ep_keys = [k for k in keys if k[0] == ep]
            tags = next((r.get("tags") for i in idx for r in rows_by_run[i].values() if r["episode_id"] == ep), None)
            per_run = []
            for i in idx:
                ov = [r["overall"] for k, r in rows_by_run[i].items() if k[0] == ep and r.get("overall") is not None]
                mean = f"{sum(ov) / len(ov):.2f}" if ov else "-"
                per_run.append(f"{summaries[i]['model']} {mean}")
            body.append(f"<details class='ep'><summary><b>{ep}</b> {_e(titles.get(ep, ''))}"
                        f"<span class='tags'>{_e(', '.join(tags or []))} · {_e(' | '.join(per_run))}</span></summary>")
            body.append(f"<div class='item' style='--n:{len(idx)}'>")
            for key in ep_keys:
                _, rep, item = key
                label = f"rep {rep}" + (f" · chapter {item}" if item else "")
                o = next((outs_by_run[i].get(key) for i in idx if key in outs_by_run[i]), None)
                if o and o.get("chapter_title"):
                    label += f" · [{_e(o.get('timestamp'))}] {_e(o['chapter_title'])}"
                body.append(f"<h3>{label}</h3><div class='cols'>")
                for i in idx:
                    body.append(_col(summaries[i], rows_by_run[i].get(key), outs_by_run[i].get(key)))
                body.append("</div>")
            body.append("</div></details>")
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>{''.join(body)}</body></html>")
