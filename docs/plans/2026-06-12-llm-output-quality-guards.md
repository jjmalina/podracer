# LLM Output Quality Guards: Detect and Retry Degenerate Completions

**Date:** 2026-06-12
**Status:** Implemented in `summarize.py` (branch `fix/llm-output-quality-guards`);
pending deploy + reprocess of 95741/109241. See "Implementation notes" below for
where the shipped code deviates from this plan.

## Problem

Episode 95741 (Kelsey Hightower) displayed truncated highlights — items in the
last few chapters reading like "On GenAI: " with the actual content missing.
Reprocessing on 2026-06-12 produced clean highlights but a **truncated chapter
summary**: the "A People-First View of GenAI" chapter (02:14:33) stored
`"Kelsey Hightower articulates a "` as its entire detailed summary.

## Evidence

From the 2026-06-12 reprocess (summarize job 154952, structured logs in
OpenSearch, backend `openrouter/deepseek/deepseek-v4-flash`):

- One `chapter_detail` call returned **14 output tokens** for a 4,424-token
  input — the truncated chapter above. No error, no warning. The response was
  schema-valid JSON (`{"summary": "Kelsey Hightower articulates a "}`), so it
  passed Pydantic validation and was stored as-is.
- Three other `chapter_detail` calls (chapters 1, 3, 18) returned **raw prose
  instead of JSON** despite `response_format: json_schema, strict: true`.
  Pydantic raised, and `enrich_chapters` silently kept the short summary —
  no retry.
- The original 2026-06-09 run (job 127270, pre-structured-logging journal on
  the LXC) shows a completely clean log: no truncation-repair warning, no
  enrichment failures. Yet it stored the "On GenAI: " stub highlights. The
  degenerate output was invisible.

### Second episode: 109241 (MacroVoices #536, processed 2026-06-11)

Same failure class, two more manifestations, all from one summarize run
(job logs 17:45–17:46Z, `openrouter/deepseek/deepseek-v4-flash`):

- The `highlights` call returned **8 output tokens in 1.9s** for a
  29,390-token input — `{"highlights": []}`. An empty list is schema-valid,
  so the episode was stored with **zero highlights** and the UI shows none
  for any chapter. No warning fired.
- Three `chapter_detail` calls (chapters 4, 10, 16) returned prose instead
  of JSON and silently kept the 1–2 sentence chapters-pass summary
  (161–193 chars).
- Three more `chapter_detail` calls returned **thin but valid** completions:
  `out=54`, `out=67`, `out=89` tokens → chapters 12 (233 chars), 18 (314),
  and 6 "Are We Headed for a Major Drawdown?" (385 chars — covers the host's
  question, omits the guest's entire answer). The prompt asks for 150–300
  words (~800–1800 chars); none was flagged.

Net: 6 of 21 chapters degraded plus the entire highlights pass lost, from a
job whose only log signal was three generic enrichment warnings.

## Root cause

`deepseek-v4-flash` via OpenRouter intermittently returns **degenerate
completions that are not flagged by any error signal**:

1. **Stub completions** — the model emits a few words and stops
   (`finish_reason: "stop"`, not `"length"`). The output is schema-valid
   JSON, so every existing check passes. This also explains the original
   "On GenAI: " highlights: tail-of-list degeneration inside one long
   highlights array.
2. **Format breaks** — raw prose despite strict `json_schema` response
   format. Likely OpenRouter routing to a provider that doesn't honor
   structured outputs.

The pipeline has no defense in depth: `_chat_openrouter` only inspects
`finish_reason == "length"` (and then *repairs and accepts* the truncated
JSON rather than retrying); content quality is never validated; per-chapter
enrichment gives up after a single failure; `llm_call` events don't log
`finish_reason` or the serving provider, so none of this is observable.

## Solution

**Check every LLM response for plausibility, and retry the call when the
check fails.** That is the entire fix; everything else is support for it.
The model's degenerate responses are transient — the same calls succeed on
other attempts — so a bounded retry converts "permanently damaged stored
summary" into "one extra call". Restructuring the pipeline (e.g. per-chapter
highlights) is explicitly *not* the fix: chapter summaries are already
per-chapter and degraded anyway. Architecture doesn't stop a model from
returning garbage; validation does.

All changes land in `summarize.py`:

1. **Plausibility checks** after each call (the detector).
2. **Bounded retry** on any failure — bad JSON, failed checks, or
   `finish_reason == "length"` (the fix).
3. **Provider constraint** on OpenRouter so only providers that honor
   structured outputs serve the requests (kills the prose responses at the
   source).
4. **Observability**: log `finish_reason`/provider per call and a structured
   warning per retry, so degeneracy frequency is measurable in OpenSearch.

Then reprocess the known-affected episodes (95741, 109241) plus whatever a
DB scan for stub patterns turns up.

### 1. Validate content, not just schema (the detector)

After Pydantic validation, run cheap plausibility checks per step:

- **Chapter detail / summary:** minimum length (e.g. ≥ 200 chars for chapter
  detail when the transcript slice is substantial; the prompt asks for
  150–300 words) and text ends in terminal punctuation (`.`, `!`, `?`, `"`,
  `)`). `"Kelsey Hightower articulates a "` fails both.
- **Highlights:** the list itself must have a plausible count — the prompt
  asks for 15–25, so < 5 items on a full-length episode is degenerate
  (episode 109241 stored `[]` from an 8-token completion). Each item ≥ 40
  chars and ends in terminal punctuation; items like `"On GenAI: "` fail.
  If > ~20% of items fail, the whole response is degenerate → retry the
  call; otherwise drop the bad items.
- **Speakers / chapters:** non-empty lists; chapters cover > 1 timestamp.

Validation failure raises a `DegenerateOutputError` carrying the step,
reason, and output size.

### 2. Retry degenerate output (the fix)

Wrap each LLM step in a bounded retry (2 retries, short backoff) that
triggers on: Pydantic validation errors (the prose case), content validation
errors (the stub case), and `finish_reason == "length"`. JSON repair
(`_repair_truncated_json`) becomes a last resort on the final attempt only —
today it *masks* truncation on the first response.

In `enrich_chapters`, the existing "keep the short summary" fallback stays,
but only after retries are exhausted — and it should emit a structured
`chapter_enrichment_fallback` event (it's currently a printf-style warning
with no episode-aggregatable fields).

### 3. Constrain OpenRouter routing

Add `provider: {"require_parameters": true}` to the OpenRouter payload so
requests only route to providers that support `response_format.json_schema`.
This should eliminate most of the prose-instead-of-JSON failures at the
source. Evaluate whether to also pin an allowlist of providers once
`provider` shows up in the logs (step 4).

### 4. Make degeneracy observable

Add to every `llm_call` event: `finish_reason`, OpenRouter's
`native_finish_reason` and `provider`, and the step label (`speakers`,
`summary`, `chapters`, `chapter_detail`, `highlights`). Today a 14-token
degenerate response is indistinguishable from a healthy one in OpenSearch.

Emit one `llm_degenerate_output` warning event per retry trigger (step,
attempt, reason, output_tokens) — an OpenSearch/GlitchTip signal for model
or provider regressions, so "how often does this happen" has an answer that
isn't a truncated page.

### 5. Reprocess affected episodes

After the guards ship: re-summarize 95741 and 109241, and run a one-off DB
scan over `summaries.data` for the stub patterns (empty `highlights`,
highlight text under 40 chars or ending in `:`, chapter summaries ending
mid-word) to find and reprocess the rest of the backlog.

## Considered: per-chapter highlights instead of one episode-wide pass

Raised while triaging 109241 ("should the insights generator be per chapter
like the chapter summaries?"). Chapter summaries already *are* per-chapter
(`enrich_chapters`), and 6 of 21 still degraded — small per-call contexts do
not prevent degenerate completions, so this is not a substitute for the
validation/retry guards above. It's a real option for a later iteration
though, with distinct trade-offs:

- **For:** denser coverage (one episode-wide pass capped at 15–25 items
  spreads thin across ~21 chapters), exact chapter binding (no timestamp
  matching), a degenerate call costs one chapter instead of the episode.
- **Against:** ~21 extra LLM calls per episode, needs cross-chapter dedup
  (the reason insights/speaker-takes were consolidated in 16f3b69), loses
  the global "most memorable in the episode" ranking, and tends to extract
  filler from thin chapters.

Decision: ship the guards first; revisit per-chapter highlights only if
coverage (not degeneracy) is still a complaint afterwards.

## Out of scope

- Switching models/backends. If the logs from step 4 show one provider is
  responsible for most degeneracy, provider pinning (step 3) is the lever.

## Implementation notes

Three deliberate deviations from the plan above, all to avoid *making the
reader's experience worse* while still catching the degeneracy:

1. **`finish_reason == "length"` is not itself a retry trigger.** The content
   checks subsume it: a truncation that actually cuts a sentence fails the
   terminal-punctuation check and is retried; a complete answer that merely hit
   the token cap passes and is accepted. Retrying purely on `length` would burn
   attempts re-truncating genuinely-over-cap responses. JSON repair still runs,
   but only on the final attempt.
2. **Chapter-detail min length is 400 chars (not 200) for substantial slices,
   and terminal punctuation is an always-on check.** The terminal-punctuation
   check is what catches the headline `"Kelsey Hightower articulates a "` stub
   regardless of length. The 400-char floor (gated on a >3000-char transcript
   slice) is what catches the "thin but valid" 233/314/385-char chapters from
   109241 that a 200-char floor would have missed. Genuinely-thin chapters
   (small slice) are exempt, matching the prompt's "1–2 sentences is fine".
3. **Chapter enrichment never downgrades.** On exhausted retries it keeps the
   *longer* of the best enrichment attempt vs. the existing chapters-pass
   summary — so an aggressive validator can't replace a decent enrichment with
   the tiny fallback summary. Episode-level steps (summary/chapters/highlights)
   accept a best-effort result on exhaustion but fail the job (worker retries)
   if nothing ever parsed.

New structured events for step 4 observability: `llm_call` now carries
`finish_reason`/`native_finish_reason`/`provider` and (via a `step` contextvar
bound in `_step`) a `step` label; `llm_degenerate_output` fires per failed
attempt; `llm_degenerate_output_exhausted` when a best-effort result is
accepted; `chapter_enrichment_fallback` (structured, replaces the old printf
warning) records which summary was kept. Retry/validation live in
`_chat_checked` / `_checked_or_fail`; checks are `_check_{speakers,summary,
chapters,highlights}` plus an inline slice-aware check in `_enrich_one_chapter`.

Step 5 (reprocess 95741/109241 + DB scan) is still TODO — it needs the deploy.
The DB scan must exclude legacy episodes that legitimately have empty
`highlights` but populated `insights`/`speaker_takes` (the `effective_highlights`
migration), or every pre-consolidation episode will look degenerate.

## Verification

- Unit tests for the validators (stub text, mid-sentence cut, empty/short
  highlights list, prose-not-JSON, length finish_reason) and for the retry
  path (degenerate first response, clean second).
- Reprocess episodes 95741 and 109241; confirm every chapter summary and
  highlight passes validation — 109241 must come back with a full highlights
  list and substantive summaries for chapters 4, 6, 10, 12, 16, 18.
- After a few days, query OpenSearch for `llm_degenerate_output` /
  `chapter_enrichment_fallback` counts to measure how often retries fire and
  whether they converge.
