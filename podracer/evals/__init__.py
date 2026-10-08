"""Model evals for the per-episode LLM prompts.

One eval per prompt in :mod:`podracer.summarize` (speakers, summary, chapters,
chapter_detail, highlights). Each eval runs the exact production code path for
that pass against a fixed dataset of real transcripts, scores the output with
the same structural guards production applies, and (optionally) has an LLM
judge grade it on a per-prompt rubric. Runs are written to disk so repeated
runs of different models can be compared side by side.

    python -m podracer.evals fetch                       # pull the dataset from prod
    python -m podracer.evals run --eval summary --backend anthropic --model claude-haiku-5-5
    python -m podracer.evals compare eval/runs/summary/*

See eval/README.md.
"""

EVALS = ("speakers", "summary", "chapters", "chapter_detail", "highlights")
