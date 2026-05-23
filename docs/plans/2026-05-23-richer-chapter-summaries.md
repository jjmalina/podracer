# Richer chapter summaries

**Date:** 2026-05-23
**Status:** Planned
**Related:** `docs/plans/2026-05-23-nested-chapters.md` (this builds on the nested-chapter UI)

## Goal

Chapter summaries today are 1-2 sentence glosses that name the topic
without explaining it. Looking at episode 3867 (Cerebras / Andrew
Feldman):

> **Memory and Speed: SRAM vs. HBM** — _Feldman describes how the
> large chip allows using fast on-chip memory (SRAM) instead of slow
> HBM, making inference 15 to 1000 times faster than GPUs._

That tells you *what was discussed*, not *what was explained*. You
still have to listen to learn why SRAM is faster than HBM, what HBM
is, or what tradeoff Cerebras made by adding "more square millimeters."

The actual explanatory content does land in the insights list — in
this case `[00:06:44] Andrew Feldman: GPUs use memory that stores a
lot but is slow (HBM)...` — but it's pinned to the *prior* chapter's
window because that's when Feldman first mentioned the topic, not
when the dedicated chapter started. Even with the chapter nesting
shipped earlier, the user reads top-down and the chapter prose is what
they scan first.

Target: the chapter summary itself is substantive enough to learn
from, with the model encouraged to draw on background knowledge to
define terms when the transcript assumes them — bounded so it doesn't
invent facts.

## Design

### Single chapter-extraction call with an upgraded prompt

The existing `CHAPTERS_PROMPT` literally caps the model at "a 1-2
sentence summary." Removing that cap and tightening the framing is the
single load-bearing change. No new pass, no slicing helper, no new
LLM calls.

```python
class Chapter(BaseModel):
    title: str
    timestamp: str
    summary: str   # was: 1-2 sentences. now: substantive paragraph(s)
```

Schema unchanged — `summary` is already a `str`. DB unchanged —
chapters live inside `summaries.data` JSON.

Why not the per-chapter focused pass:

- The current shortcoming is mostly a prompt cap, not an attention-
  budget problem. DeepSeek V4 Flash has a 1M context and handles
  structured output of 15 chapters × ~250 words = ~4K output tokens
  comfortably.
- Per-chapter slicing + thread pool + per-call error handling is a
  meaningful pile of new code and operational surface.
- Latency: 15 parallel HTTP calls share backend capacity and eat
  startup costs N times; the slowest-of-N can exceed a single bigger
  call.
- Faster iteration loop: one prompt to tune, one call to test, one
  knob to revert.

**v2 trigger (do not build yet):** if the upgraded-prompt output
shows tail-drift — chapters 1-3 detailed and chapters 12-15
progressively thinner — that's the empirical signal to split into a
per-chapter focused pass. Until we see it, don't build it.

### The prompt

```
You are summarizing the chapters of a podcast for a reader who hasn't
listened. You will be given a speaker key and a timestamped
transcript. Break the episode into 10-20 logical chapters reflecting
natural topic transitions. Skip ad / sponsor segments entirely.

For each chapter, return:
- A short descriptive title.
- The timestamp (HH:MM:SS) where the chapter begins.
- A substantive summary, typically 2-4 short paragraphs (150-300
  words), that explains what was discussed in enough depth that the
  reader learns something. Not "they talked about X" — actually
  convey the substance of X.

Definitional asides: when speakers reference a technical concept,
named entity, or piece of shared context that they don't fully
explain, you may add a brief definition (1-2 clauses) drawing on
your background knowledge. Phrase these asides clearly so the reader
can tell what the speakers said versus what you're explaining: use
"X is Y, ..." or "(X here refers to Y)" rather than putting it in
the speaker's mouth. If you aren't confident in a definition, omit it
— let the speaker's words stand.

Do not invent quantitative claims (numbers, dates, prices,
benchmarks, named studies, quotes) that aren't in the transcript.

If a chapter is genuinely short on substance — an intro greeting,
a brief tangent — a 1-2 sentence summary is fine. Don't pad. Default
to substantive prose otherwise.

Use real speaker names from the speaker key. Cover the entire episode
from start to finish.
```

Key design choices, with the reasoning behind each:

- **"Substantive, typically 2-4 paragraphs."** "Typically" leaves
  room for short chapters without making "short" the default. Earlier
  draft said "permission to be short" — that probably backfires given
  the model's existing bias toward glossing.
- **Bounded definitional asides with clear phrasing.** "Phrase so the
  reader can tell what the speakers said vs. what you're explaining"
  is the load-bearing line. Without it the model paraphrases its
  background knowledge as if Feldman had said it.
- **"If unsure, omit"** as the hallucination escape valve. Better
  silence than confident fabrication.
- **Explicit "do not invent numbers/dates/quotes."** These are the
  highest-cost hallucinations (most readable, least caught by
  eyeball).

### UI

Template already renders `chapter.summary` as a single `<p>`. Bump it
to split on `\n\n` so paragraph breaks render properly:

```jinja
{% for para in entry.chapter.summary.split('\n\n') %}
<p class="chapter-summary">{{ para }}</p>
{% endfor %}
```

Considered but rejected: splitting into a short `summary` + a longer
`detail` field behind a per-chapter `<details>`. That serves a
scan-then-drill use case nicely, but:

- It doubles the prompt-design work (two distinct fields the model
  has to decide what to put where) for unclear gain.
- The chapter `<details>` we already shipped *is* the scan-then-drill
  mechanism — collapsed shows just the title, expanded reveals the
  prose.
- If long expanded chapters become a real ergonomic issue we can
  revisit; cheaper to learn from the simpler shape first.

## Re-summarize existing episodes

Existing summaries stay valid against the unchanged schema, so reads
don't break — old episodes just keep their old short summaries until
re-summarized.

For refreshing individual episodes: add a small "Re-summarize" button
on the episode detail page. Clicking it:

1. Deletes the existing `summaries` row for the episode.
2. Enqueues a fresh `summarize` job (the existing `transcribe` step
   is skipped automatically since the transcript row still exists).

This sidesteps the missing `jobs.force` flag (called out in the
"Job management" roadmap entry as future work). By removing the
artifact first, the worker treats it as a clean enqueue.

- Route: `POST /episodes/{episode_id}/resummarize`.
- UI: a second button next to the existing "Process / Re-summarize
  this episode" form. Distinguishing the two: the existing button
  enqueues only when there's no summary; the new button always wipes
  and re-runs (gated by a JS `confirm()` since it discards work).
- Safety: refuse if there's an active summarize job for the episode
  (would race the worker). Show a flash instead.
- No bulk re-process for now. A whole-archive refresh is out of scope
  for this change — that's the job-management roadmap entry.

## Implementation

1. **`podracer/summarize.py`** — replace `CHAPTERS_PROMPT` with the
   prompt above. No other Python changes.
2. **`podracer/web/templates/episodes/detail.html`** — split chapter
   summary on `\n\n` (one-line change).
3. **`podracer/web/routes/episodes.py`** — add `resummarize_episode`
   POST handler: delete the summary row, then call
   `enqueue_episode_pipeline` (which will skip transcribe since the
   transcript exists and enqueue summarize). Guard against active
   jobs.
4. **`podracer/db/summaries.py`** — add `delete_summary(conn,
   episode_id)` helper.
5. **`podracer/web/templates/episodes/detail.html`** — add the
   Re-summarize button next to the existing process form when a
   summary exists. `onsubmit` confirm.

## Tests

- **`tests/test_resummarize.py`**: with the in-memory DB fixture,
  seed an episode + transcript + summary; POST to the resummarize
  route; assert the summary row is gone and a `summarize` job
  exists. Assert the route refuses (4xx or flash) when an active
  summarize job is present.
- **No tests on the prompt itself.** Prompt quality is an eval
  concern, not a unit-test concern.

## Evaluating the prompt

Empirical, not gated in CI:

1. Pick 3 reference episodes spanning topics + length: episode 3867
   (Cerebras, technical), plus one founder-interview episode and one
   long (90+ min) wide-ranging episode.
2. Re-summarize each, eyeball:
   - Does each chapter convey enough that you'd learn something
     without listening?
   - Definitional asides: are they correct? Phrased so it's clear
     they're not the speaker's words?
   - Hallucinated numbers / dates / quotes?
   - Tail drift: do late chapters get progressively thinner?
3. If tail drift is real, that's the trigger for the per-chapter
   focused pass (v2). If hallucinations are real, tighten the
   "don't invent" / "if unsure, omit" framing. If asides are
   miscredited to speakers, tighten the phrasing instruction.

## Cost

Same one chapter-extraction call as today, just with longer output.
At ~250 words/chapter × 15 chapters ≈ 5000 output tokens vs. ~500
today. On DeepSeek V4 Flash that's +5000 × $0.28/1M ≈ **+$0.0014
per episode**. Negligible.

## Open questions

- **Hallucination risk from "draw on background knowledge."** Most
  acute on niche topics, recent events, less-common podcasts. The
  prompt's "if unsure, omit" + "do not invent numbers" guardrails
  are necessary but not sufficient on their own — the eval pass
  above is the real check before considering this landed.
- **Do we still need the separate `insights` list?** After chapter
  prose gets richer, much of what insights captured is now in-
  chapter. Insights still earn their keep as "scannable highlights
  across the whole episode" — different use case. Keep both for
  now; revisit if the insights list feels redundant in practice.
- **Timestamp drift between chapter boundaries and where a topic is
  first mentioned.** Episode 3867 has the SRAM/HBM insight at
  `00:06:44` (prior chapter's window) even though there's a dedicated
  "Memory and Speed: SRAM vs. HBM" chapter at `00:06:55`. With richer
  per-chapter prose, the "Memory and Speed" chapter will carry its
  own SRAM/HBM explanation regardless of where the insight is pinned,
  so the drift matters less. The orthogonal fix — re-binning
  insights to the chapter whose *topic* matches rather than whose
  *window* the timestamp falls in — is a separate (harder) entry.
- **Latency.** Bigger output → longer single call. Probably 5-15s
  added to the summarize step. Worth noting, not blocking.

## Phasing

1. Update prompt, template, route, helper; add re-summarize button +
   route test. One commit.
2. Re-summarize the 3 reference episodes; eyeball.
3. Iterate prompt based on what's broken. Most likely tightening
   definitional-aside phrasing or "if unsure, omit." If tail drift
   shows up, promote to v2 (per-chapter focused pass).
4. Document the per-episode re-summarize flow in the README so
   archive owners know how to refresh old chapters they care about.
