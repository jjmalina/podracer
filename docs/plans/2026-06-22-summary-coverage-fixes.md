# Summary Coverage: Provider Routing + Highlight Density

**Date:** 2026-06-22
**Status:** Tiers 1 + 2 implemented in `summarize.py` (branch
`summarize-coverage-fixes`); pending deploy + reprocess. Tier 3 (structured,
domain-specific extraction via per-show instructions) is scoped here but not
built — it depends on the roadmap's custom-instructions feature.

## Problem

On long episodes, information-dense segments get under-summarized relative to
looser narrative discussion — thin chapter detail and too few highlights —
even though they often carry the most substance. Surfaced while reviewing a
2h29m episode whose ~1h analytical back half was far less covered than its
interview front half, but both root causes are general and affect any episode.

## Evidence

From the summarize-job logs (OpenSearch) and the stored summary. Coverage per
chapter (chapter labels anonymized):

| Chapter | Dur | Detail words | Highlights |
|---|---|---|---|
| Narrative segment A | 17m | 296 | 6 |
| Narrative segment B | 12m | 338 | 2 |
| Narrative segment C | 12m | 216 | 3 |
| **Dense analytical segment 1** | **21m** | **39** | **1** |
| **Dense analytical segment 2** | **18m** | **43** | **2** |
| **Dense analytical segment 3** | **24m** | **48** | **3** |

The transcript for the dense segments is rich with concrete, specific detail
(numbers, levels, conditional calls) that the output dropped. Two independent
root causes:

### 1. Thin chapter detail — a provider-routing bug, not truncation

The chapter-detail enrichment for the three densest chapters (the three largest
transcript slices) was routed by OpenRouter to provider **"Baidu," which
advertises `json_schema` support but returns prose** — `reason=invalid_json` on
all 3 retry attempts each. Enrichment was discarded (`enrichment_chars=0`) and
the UI fell back to the coarse 1–2-sentence chapter summary:

```
chapter_enrichment_fallback  Segment 1  kept=short_summary  enrichment_chars=0
chapter_enrichment_fallback  Segment 2  kept=short_summary  enrichment_chars=0
chapter_enrichment_fallback  Segment 3  kept=short_summary  enrichment_chars=0
```

`finish_reason` was `stop` on every call — this was **not** truncation. The
root cause: `_chat_openrouter` set `provider.require_parameters=true` to only
use providers that honor `json_schema`, but Baidu *advertises* the parameter
and ignores it — and the degenerate-output retry re-sent the **identical
payload**, so it kept landing on the same bad provider. This can corrupt **any
step** (speakers / summary / chapters / chapter-detail / highlights) of **any
episode**; the dense chapters just made it visible here.

### 2. Sparse highlights — prompt/budget design

The highlights pass *succeeded* (no degenerate event) but under-extracts dense
content by design: `HIGHLIGHTS_PROMPT` asked for a single global "15–25
highlights covering the full episode," which the model spent on the quotable
narrative, starving the dense segments; and the "takeaway/opinion" taxonomy
didn't cue capture of specific figures/conditions. Affects any dense episode.

### 3. Recurring producer misclassified as `advertiser`

A recurring producer/co-host was tagged `role=advertiser` by speaker-ID. Every
prompt says "ignore advertisement and sponsor segments entirely," so some of
their contributions were likely suppressed; the episode-detail route also
filters ad speakers out of the Speakers list while still rendering highlights
attributed to them. Contributing factor, separate fix.

## Plan

### Tier 1 — never route to Baidu + provider exclusion on retry  *(implemented)*

Two layers, both via OpenRouter `provider.ignore`:

- **Static denylist** (`_DENYLISTED_PROVIDERS = ["Baidu"]`): always excluded on
  every OpenRouter call, so a known structured-output liar is never used even
  on the first attempt. Add a provider here once it proves untrustworthy.
- **Dynamic backstop**: thread an `ignore_providers` list through
  `_chat → _chat_openrouter`; in `_chat_checked`, accumulate any provider that
  produces degenerate output mid-call and exclude it from subsequent attempts,
  so future offenders are routed around without a code change.

openrouter-only (ollama/vllm are single-backend). Tests:
`test_openrouter_constrains_provider_to_structured_output` (denylist on by
default), `test_openrouter_merges_denylist_with_retry_exclusions`,
`test_degenerate_provider_excluded_on_retry`. After deploy, a re-summarize
should restore the dense chapters' detail for free.

### Tier 2 — highlights prompt: even density + concrete specifics  *(implemented)*

Rewrote `HIGHLIGHTS_PROMPT` (same `Highlight` schema) to (a) cover the episode
at even density start-to-finish and explicitly not collapse long substantive
segments into one or two highlights — target ~one highlight per few minutes of
substance, ~25–40 for a long/dense episode, scaled to substance; and (b)
capture concrete particulars verbatim — numbers, thresholds, dates, names,
levels, conditions/recommendations with their qualifiers — without inventing
figures.

### Tier 3 — structured domain extraction via per-show instructions  *(future)*

Even with 1 + 2, output is prose highlights, not structured data. For shows
where the value is a structured table (e.g. financial/markets shows want
trade ideas: `{instrument, level, direction, condition, timeframe, conviction,
speaker, timestamp}`; a research show wants studies cited), the right vehicle
is the roadmap's per-podcast custom summarization instructions
(`docs/roadmap.md:64-117`) extended with a typed schema run as an extra
extraction pass when a show opts in. v2 typed-field approach from that roadmap
entry, fed through the existing `response_format` json-schema path.

### Also (separate, small): fix the producer → advertiser misclassification

Tighten the speaker-ID prompt so a recurring producer/co-host isn't labeled
`advertiser`, so the "ignore ads" instruction stops suppressing their
contributions. Not in this branch.

## Validation (A/B, n=3 per arm, same transcript)

Ran old code (main) and new code 3× each on one 2h29m transcript, to separate
the fix from run-to-run provider luck:

| arm | Baidu in routing | Baidu invalid_json | enrichment fallbacks | dense-half highlights | …with a figure |
|---|---|---|---|---|---|
| old | 3/3 runs | 0 / 3 / 3 | 0 / 1 / 1 | 5 / 7 / 10 | 3 / 2 / 6 |
| new | 0/3 runs | 0 / 0 / 0 | 0 / 0 / 0 | 11 / 12 / 13 | 9 / 8 / 9 |

- **Tier 1 is causal, not luck.** Baidu is in the old routing pool every run
  and collapsed a dense chapter in 2/3 (prod was a worse draw — 3 collapses).
  The denylist removes Baidu entirely (0/3 runs), so fallbacks are zero *by
  construction*. old1's lucky no-collapse draw is the point: old is a coin flip,
  new is guaranteed.
- **Tier 2 is causal and isolated from provider luck.** The highlights step is
  a single call that essentially never routes to Baidu, so the difference can't
  be provider routing. Dense-segment highlights are consistently 11–13 (new) vs
  5–10 (old) and figure-bearing 8–9 vs 2–6 — non-overlapping across runs.

## Open questions

- Does `provider.ignore` use the same provider names returned in
  `response.provider`? (Assumed yes.) Verify against a real OpenRouter run
  post-deploy.
- Denylist is a hardcoded module constant. If providers churn, move it to
  config (`[summarize]`) so it's editable without a deploy.
- Tier 2 raises the highlight target → ~+token cost per episode. Acceptable?
