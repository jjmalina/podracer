# LLM prompt evals

One eval per LLM prompt in the episode pipeline (`podracer/summarize.py`):

| eval | prompt | unit | frozen upstream input (from the prod-stored summary) |
|---|---|---|---|
| `speakers` | `SPEAKER_ID_PROMPT` | episode | none (raw transcript + show notes) |
| `summary` | `SUMMARY_PROMPT` | episode | speaker key |
| `chapters` | `CHAPTERS_PROMPT` | episode | speaker key |
| `chapter_detail` | `CHAPTER_DETAIL_PROMPT` | one chapter | speaker key + chapter list |
| `highlights` | `HIGHLIGHTS_PROMPT` | episode | speaker key |

Each eval calls the same function the worker calls for that pass, with the
same content checks and retries, so what is measured is what ships. Downstream
passes take their upstream inputs from the summary prod already stored, so a
model's chapter writeups are not penalized for its own speaker-ID mistakes.
Transcription is out of scope (see `docs/plans/transcription-eval.md`).

## Dataset

`manifest.json` (committed) lists prod episode ids with tags describing why
each is in the set: short/mid/long/very-long, speaker count, teaser cold-open,
solo monologue, panel, technical vs finance. The transcripts, show notes and
prod summaries live in `eval/data/<id>/case.json`, which is **gitignored** —
this is a public repo and the transcripts and speaker names are not.

Pull them from the deployment over SSH (read-only sqlite on the host):

```bash
export PODRACER_EVAL_SSH="-i ~/.ssh/<key> user@host"   # or pass --ssh
python -m podracer.evals fetch                          # fills eval/data/
```

`eval/data/<id>/labels.json` is the speaker ground truth for the `speakers`
eval. It is **seeded from the prod output** and marked `"verified": false`;
until you correct it by hand, the labels metrics measure agreement with prod
(i.e. with whatever model produced it), not correctness. The judge score does
not depend on the labels.

## Keeping the dataset outside the repo

`pack` zips the manifest and the fetched case files; `matrix --cases` unpacks
such a zip before running. The zip has the same content restrictions as
`eval/data/` (it is gitignored under `eval/`), so keep it somewhere private:

```bash
python -m podracer.evals pack --out ~/podracer-eval/cases-$(date +%F).zip
```

## Running everything at once

`matrix` runs every eval for every model with one judge, in parallel, then
writes two reports: the full one (outputs, issues, rationales; private) under
`eval/runs/`, and a **redacted** one (scores only) under `eval/reports/`, which
is committed as the record of how the models compared at that point in time.

```bash
python -m podracer.evals matrix --cases ~/podracer-eval/cases-2026-10-09.zip \
    --model anthropic:claude-haiku-5-5:low \
    --model anthropic:claude-sonnet-5-5:low \
    --model openrouter:deepseek/deepseek-v4-flash --providers deepinfra,digitalocean,parasail,venice,gmicloud,azure \
    --judge anthropic:claude-opus-5-5:medium \
    --slug haiku-sonnet-deepseek-opus-judge --title "Haiku 5.5 vs Sonnet 5.5 vs DeepSeek V4 Flash"
```

Model specs are `backend:model[:effort]`. `--dry-run` prints the plan and a
rough judge-cost upper bound without calling anything; `--evals`, `--ids`,
`--reps` narrow it. The 2026-10-09 three-model run cost about $145 at list
price (judge ≈ $40 per model on Opus 5.5, Sonnet generation ≈ $18), so check
the estimate before a full run. Use the same judge for every run you intend
to compare; scores from different judges are not on the same scale.

Reports so far: see `reports/`.

## Running one eval

API keys come from env vars (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`), falling
back to the usual `config.toml` / `.credentials/` resolution.

```bash
# Haiku 5.5 on the summary prompt, 3 runs per episode, judged by Sonnet 5.5 (the default)
python -m podracer.evals run --eval summary --backend anthropic --model claude-haiku-5-5 --effort low

# The incumbent, same eval, same judge, with the prod provider allowlist
python -m podracer.evals run --eval summary --backend openrouter --model deepseek/deepseek-v4-flash \
    --providers deepinfra,digitalocean,parasail,venice,gmicloud,azure

# Compare every summary run side by side (add --by-episode to see where they differ)
python -m podracer.evals compare eval/runs/summary/*

# Read the outputs side by side (--redact for the scores-only version that can be committed)
python -m podracer.evals report eval/runs/summary/* eval/runs/chapters/* --out eval/runs/report.html
```

Useful flags: `--reps N` (default 3), `--ids 1,2,3` (subset of the manifest),
`--chapters-per-episode K` (chapter_detail: evenly spaced sample, default 3;
`0` judges every chapter, about 18 per episode), `--no-judge` (structural metrics only, free), `--judge-model claude-opus-5-5`
(a stronger judge at twice the price; `PODRACER_EVAL_JUDGE_MODEL` sets the
default), `--judge-backend
openrouter --judge-model <model>` (a non-Anthropic judge, to check for
same-family bias when grading Claude), `--workers N`, `--json`.

Each run writes `eval/runs/<eval>/<model>-<timestamp>/`:

- `meta.json` — what ran (model, effort, judge, reps, ids, git rev)
- `results.jsonl` — one row per (episode, rep, item): structural metrics,
  pass/fail against the production content check, judge scores, issues,
  token usage, latency, failure class on error
- `outputs/<ep>_rep<k>[_cNN].json` — the generated output plus the judge's
  per-dimension rationale and issue list, for reading individual losses
- `summary.json` — the aggregate `compare` prints

## Scoring

**Structural (free, deterministic).** The production content guard for the
pass (`passed`), plus per-eval metrics: speaker label coverage and
name precision/recall vs labels; chapter count, coverage of the timeline,
duplicate starts, teaser detection; highlight count, kind split, decile
coverage across the episode, attribution to a known speaker, duplicate ratio;
writeup length vs slice length. Error rate and failure class (degenerate
output after retries, provider policy, HTTP) are first-class metrics: the
incumbent's known failure mode is transient degenerate output.

**Judge (LLM, pointwise 1-5 per dimension, Sonnet 5.5 by default).** Rubrics are in
`podracer/evals/judge.py`, derived from each prompt's own instructions. The
judge sees the exact source the model saw, the model's instructions, and the
output; it grades against the source only, lists concrete issues, and gives a
holistic `overall`. The transcript block carries `cache_control` so the three
reps of an episode (and its four full-transcript evals) share one cached
prefix on the anthropic backend.

Caveats: a pointwise judge is noisier than a pairwise one, so run `--reps 3`
and read the `±` (95% CI over items) before calling a difference real. An
Anthropic judge grading an Anthropic generator has a same-family bias risk;
spot-check a subset with a non-Anthropic judge via OpenRouter. Cost in the
table ignores cache discounts (an upper bound).

## Rough cost

Generation is pennies per episode on either Haiku 5.5 or DeepSeek V4 Flash.
The judge dominates: a Sonnet 5.5 judge reading a full transcript is roughly
$0.10-0.25 per item (Opus 5.5 is double), so a 14-episode × 3-rep run of one
full-transcript eval is on the order of $5-10 before caching; `chapter_detail`
judges only the chapter slice and is much cheaper per item. Use `--ids` and `--reps 1` to
pilot before a full run.
