"""CLI: ``python -m podracer.evals {fetch,run,compare}``. See eval/README.md."""
import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from podracer import logger, logging_config
from podracer.config import load_config
from podracer.evals import EVALS
from podracer.evals.dataset import DEFAULT_DATASET
from podracer.evals.fetch import fetch
from podracer.evals.html_report import build_html
from podracer.evals.report import compare_text, summarize_run
from podracer.evals.runner import RunConfig, run
from podracer.summarize import ANTHROPIC_DEFAULT_EFFORT, Backend

BACKENDS = ("anthropic", "openrouter", "ollama", "vllm")
# Sonnet 5.5 is half the price of Opus 5.5 per token and the judge dominates run
# cost. Override per run with --judge-model, or for a shell with the env var.
DEFAULT_JUDGE_MODEL = os.environ.get("PODRACER_EVAL_JUDGE_MODEL", "claude-sonnet-5-5")


def build_backend(name: str, model: str, *, effort: str | None = None, providers: str | None = None,
                  base_url: str | None = None) -> Backend:
    """API keys come from env vars first (ANTHROPIC_API_KEY / OPENROUTER_API_KEY),
    then the usual config.toml / .credentials resolution."""
    cfg = load_config()
    if name == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY") or cfg.anthropic_api_key
        if not key:
            raise SystemExit("anthropic backend needs ANTHROPIC_API_KEY")
        return Backend.anthropic(model, key, effort=effort or ANTHROPIC_DEFAULT_EFFORT)
    if name == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY") or cfg.openrouter_api_key
        if not key:
            raise SystemExit("openrouter backend needs OPENROUTER_API_KEY")
        allow = [p.strip() for p in providers.split(",") if p.strip()] if providers else None
        return Backend.openrouter(model, key, providers=allow, effort=effort)
    if name == "vllm":
        return Backend.vllm(model, base_url or "http://localhost:8000")
    return Backend.ollama(model, base_url or "http://localhost:11434")


def _slug(backend: Backend) -> str:
    s = re.sub(r"[^A-Za-z0-9.-]+", "_", backend.model)
    return f"{s}-{backend.effort}" if backend.effort else s


def _ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(x) for x in value.split(",") if x.strip()]


def cmd_fetch(args: argparse.Namespace) -> int:
    fetched = fetch(args.dataset, ssh=args.ssh, db=args.db, ids=_ids(args.ids), force=args.force)
    print(json.dumps({"fetched": fetched}) if args.json else f"fetched {len(fetched)} case(s) into {args.dataset}/data")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    backend = build_backend(args.backend, args.model, effort=args.effort, providers=args.providers,
                            base_url=args.base_url)
    judge = None
    if not args.no_judge:
        judge = build_backend(args.judge_backend, args.judge_model, effort=args.judge_effort,
                              providers=args.judge_providers, base_url=args.judge_base_url)
    out = args.out or (args.dataset / "runs" / args.eval
                       / f"{_slug(backend)}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}")
    cfg = RunConfig(
        eval=args.eval, backend=backend, judge=judge, dataset=args.dataset, out=out, reps=args.reps,
        ids=_ids(args.ids), chapters_per_episode=args.chapters_per_episode, workers=args.workers,
        label=args.label,
    )
    run(cfg)
    summary = summarize_run(out)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        print(compare_text([out]))
        print(f"\nresults: {out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    dirs = [Path(d) for d in args.runs]
    missing = [d for d in dirs if not (d / "results.jsonl").exists()]
    if missing:
        raise SystemExit(f"no results.jsonl in: {', '.join(map(str, missing))}")
    if args.json:
        print(json.dumps([summarize_run(d) for d in dirs], indent=1))
    else:
        print(compare_text(dirs, show_episodes=args.by_episode))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    dirs = [Path(d) for d in args.runs]
    missing = [d for d in dirs if not (d / "results.jsonl").exists()]
    if missing:
        raise SystemExit(f"no results.jsonl in: {', '.join(map(str, missing))}")
    out = args.out or (args.dataset / "runs" / "report.html")
    out.write_text(build_html(dirs, args.dataset, title=args.title))
    print(json.dumps({"report": str(out)}) if args.json else f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m podracer.evals",
                                     description="Model evals for podracer's per-episode LLM prompts")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                        help="dataset root holding manifest.json, data/, runs/ (default: eval/)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the pipeline's INFO logs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="pull manifest episodes from a deployed podracer over SSH")
    p.add_argument("--ssh", default=None, help="user@host (default: $PODRACER_EVAL_SSH)")
    p.add_argument("--db", default=None, help="remote sqlite path (default: $PODRACER_EVAL_DB)")
    p.add_argument("--ids", default=None, help="comma-separated episode ids (default: whole manifest)")
    p.add_argument("--force", action="store_true", help="re-fetch cases that already exist")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("run", help="run one eval for one model, N times")
    p.add_argument("--eval", required=True, choices=EVALS)
    p.add_argument("--backend", required=True, choices=BACKENDS)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", default=None,
                   help="anthropic/openrouter reasoning effort: low/medium/high/xhigh/max (default: off)")
    p.add_argument("--providers", default=None, help="openrouter only: comma-separated provider allowlist")
    p.add_argument("--base-url", default=None, help="ollama/vllm base URL")
    p.add_argument("--judge-backend", default="anthropic", choices=BACKENDS)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                   help=f"judge model (default: $PODRACER_EVAL_JUDGE_MODEL or {DEFAULT_JUDGE_MODEL})")
    p.add_argument("--judge-effort", default="medium")
    p.add_argument("--judge-providers", default=None)
    p.add_argument("--judge-base-url", default=None)
    p.add_argument("--no-judge", action="store_true", help="structural metrics only (free)")
    p.add_argument("--reps", type=int, default=3, help="runs per episode (default 3)")
    p.add_argument("--ids", default=None, help="comma-separated episode ids (default: whole manifest)")
    p.add_argument("--chapters-per-episode", type=int, default=None,
                   help="chapter_detail only: cap chapters per episode (evenly spaced)")
    p.add_argument("--workers", type=int, default=2, help="concurrent (episode, rep) tasks")
    p.add_argument("--out", type=Path, default=None, help="run dir (default: <dataset>/runs/<eval>/<model>-<ts>)")
    p.add_argument("--label", default=None, help="free-text note stored in meta.json")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="table of one or more run dirs")
    p.add_argument("runs", nargs="+")
    p.add_argument("--by-episode", action="store_true", help="also show overall per episode")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("report", help="self-contained HTML page: outputs of several runs side by side")
    p.add_argument("runs", nargs="+")
    p.add_argument("--out", type=Path, default=None, help="output file (default: <dataset>/runs/report.html)")
    p.add_argument("--title", default="podracer eval report")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    logging_config.configure_logging(level=logging.INFO if args.verbose else logging.WARNING)
    if not args.verbose:
        # Keep the eval's own progress lines visible.
        logging.getLogger("podracer").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        logger.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
