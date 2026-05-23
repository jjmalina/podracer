# Nest insights + speaker takes under chapters

**Date:** 2026-05-23
**Status:** Planned
**Promoted from:** `docs/roadmap.md` — "Nest insights + speaker takes under chapters"

## Goal

Restructure the episode detail page so it reads as a single "expandable
table of contents": each chapter followed by the insights and speaker
takes whose timestamps fall inside that chapter's time window. Today
the same information is split across three independent timestamped
lists (`summary.chapters`, `summary.insights`, `summary.speaker_takes`)
and the reader has to cross-reference HH:MM:SS by eye to figure out
which insight came from which chapter.

No schema or LLM changes. This is a route + template refactor.

## Design

### Output shape

A new `chapters_nested` list of dicts built in the route handler:

```python
[
  {
    "chapter": Chapter,              # the existing model, untouched
    "insights": list[Insight],       # items whose ts is in [start, end)
    "takes":    list[SpeakerTake],   # same window
  },
  ...
]
```

Plus two adjacent buckets the template renders separately:

- `pre_chapter` — items whose timestamp is before `chapters[0].timestamp`
  (rare; intro before chapter 1).
- `orphan` — items whose timestamp doesn't fall in any chapter window
  (an LLM-hallucinated timestamp, or a chapter list with gaps). Rendered
  in a fallback "Other" section at the end so nothing gets silently
  dropped.

Timestamps are fixed-width `HH:MM:SS`, so plain string comparison gives
the correct ordering — no need to parse to seconds.

### Files to change

1. **`podracer/web/routes/episodes.py`** (`episode_detail`, ~line 33)
   - After the existing `summary.insights.sort(...)` /
     `summary.speaker_takes.sort(...)` block, build `chapters_nested`,
     `pre_chapter`, and `orphan` from `summary`. Extract into a small
     helper, e.g. `_nest_under_chapters(summary)` at module scope, so
     it's easy to unit-test in isolation without going through the
     route.
   - Pass `chapters_nested`, `pre_chapter`, and `orphan` into the
     template context. Keep `summary` in the context too (the
     Speakers + Summary sections still read off it directly).
   - Guard: if `summary.chapters` is empty (older summaries from
     before chapters existed, or a summary where the LLM returned an
     empty list), skip the binning and have the template fall back to
     the old three-section render. Easiest: pass
     `chapters_nested=None` in that case and key the template on it.

2. **`podracer/web/templates/episodes/detail.html`** (Chapters/Insights/
   Speaker Takes sections, lines 70–98)
   - Replace the three `<section>`s with a single `<section><h2>Chapters
     </h2>` that loops over `chapters_nested`. Per entry: the chapter
     `header` (timestamp + title + summary) followed by indented
     `<details open>` blocks for insights and speaker takes when
     non-empty. Use Pico's existing `article` styling so it sits
     visually under each chapter.
   - Above the loop: render `pre_chapter` insights/takes in a small
     "Intro" block if non-empty.
   - Below the loop: render `orphan` insights/takes in an "Other"
     section if non-empty. Same article styling so it doesn't look like
     an error state — just a "couldn't place these" bucket.
   - Preserve the existing `[HH:MM:SS]` text in headers so the audio
     player feature (separate roadmap entry) can later turn them into
     jump links without retouching this template.

### Nesting algorithm

```python
def _nest_under_chapters(summary: PodcastSummary):
    chapters = summary.chapters
    if not chapters:
        return None, [], []

    # Pre-chapter bucket: anything before the first chapter starts.
    first_ts = chapters[0].timestamp
    pre_insights = [x for x in summary.insights      if x.timestamp <  first_ts]
    pre_takes    = [x for x in summary.speaker_takes if x.timestamp <  first_ts]
    pre_chapter  = {"insights": pre_insights, "takes": pre_takes}

    SENTINEL = "99:99:99"  # sorts after any real HH:MM:SS string
    nested = []
    placed_insights, placed_takes = set(), set()
    for i, ch in enumerate(chapters):
        start = ch.timestamp
        end   = chapters[i + 1].timestamp if i + 1 < len(chapters) else SENTINEL
        ch_insights = [x for x in summary.insights      if start <= x.timestamp < end]
        ch_takes    = [x for x in summary.speaker_takes if start <= x.timestamp < end]
        placed_insights.update(id(x) for x in ch_insights)
        placed_takes.update(id(x)    for x in ch_takes)
        nested.append({"chapter": ch, "insights": ch_insights, "takes": ch_takes})

    # Orphans: items whose timestamp didn't fall inside *any* window
    # (LLM hallucinated a ts, or chapters are non-contiguous).
    orphan_insights = [x for x in summary.insights
                       if x.timestamp >= first_ts and id(x) not in placed_insights]
    orphan_takes    = [x for x in summary.speaker_takes
                       if x.timestamp >= first_ts and id(x) not in placed_takes]
    orphan = {"insights": orphan_insights, "takes": orphan_takes}

    return nested, pre_chapter, orphan
```

Notes:

- String compare works because the format is fixed-width `HH:MM:SS`.
  If `LLM` ever returns a `H:MM:SS` (no leading zero — has happened),
  add a one-line normalization step (`ts.zfill(8)`) before binning.
- The `placed_*` sets are belt-and-suspenders. With contiguous chapter
  windows every in-range item lands in exactly one bucket, so orphans
  only appear when the LLM puts an item past the last chapter's window
  (which the `SENTINEL = "99:99:99"` already covers as long as the
  timestamp is well-formed). Still cheaper than auditing the LLM.

### Edge cases — explicit handling

| Case | Behavior |
|---|---|
| `summary.chapters == []` | Pass `chapters_nested=None`, template falls back to current three-section render. |
| All insights/takes inside chapters, none outside | `pre_chapter` and `orphan` are empty; template just shows the nested loop. |
| Item ts before first chapter | Lands in `pre_chapter`, rendered as "Intro" above the loop. |
| Item ts inside a chapter window | Nested under that chapter. |
| Item ts past the last chapter | Caught by the `SENTINEL` end-time, nested under the last chapter. |
| Malformed timestamp (e.g. `"1:23"`) | Sorts weirdly under string compare; would end up in `pre_chapter` or `orphan`. Acceptable for v1; if it shows up, add `ts.zfill(8)` normalization. |
| Older summaries with no `chapters` field | `summary.chapters` is `[]` (Pydantic default for `list[Chapter]` is required, but the saved JSON always has the key today). Falls into the empty-chapters branch. |

## Tests

Add `tests/test_chapter_nesting.py` covering the helper in isolation —
no FastAPI client needed, since the helper is pure:

- Happy path: 3 chapters, insights and takes spread across them, all
  end up in the right bucket and `pre_chapter` / `orphan` are empty.
- Pre-chapter: an insight at `00:00:30` when the first chapter starts
  at `00:01:00` lands in `pre_chapter`.
- Last-chapter inclusion: an item past the last chapter ts lands under
  the last chapter (covered by the `SENTINEL`).
- Empty chapters: helper returns `(None, [], [])` so the route can
  signal the template to fall back.
- No insights / no takes: nested entries have empty lists, doesn't
  crash, template just renders the chapter headers.

The existing test fixture (`tests/conftest.py`) builds an in-memory
DB; no new fixtures needed for the helper tests since they take a
`PodcastSummary` directly. If we add a route-level smoke test, reuse
the existing TestClient pattern and just assert that the rendered
page contains the chapter title and a nested insight in the right
order.

Run before committing:

```bash
.venv/bin/ruff check podracer/ tests/
.venv/bin/ty check podracer/ tests/
.venv/bin/pytest
```

## Out of scope / follow-ups

- **Anchor jumps per chapter.** `id="ch-{i}"` on each chapter header
  plus a small `↻` link is cheap, but the real payoff is when the
  audio-player roadmap entry lands (clickable jump points). Deferring
  to that entry so the anchors get designed alongside the player.
- **`<details>` collapsibles per chapter.** The roadmap sketch
  mentions these as an option for "heavy" chapters. Default to
  always-expanded (`<details open>`) so the page still reads
  top-to-bottom; revisit if any chapter ends up dense enough that it
  buries the next chapter heading.
- **Reflowing the Speakers + Summary sections.** Those stay where they
  are above Chapters — they're episode-level, not chapter-level.
- **Touching `summarize.py`.** Out of scope. The LLM still returns the
  same three flat lists; nesting is a render-time concern.

## Phasing

1. Add `_nest_under_chapters` helper + unit tests. Land in one
   commit; no UI change yet.
2. Wire it into `episode_detail` and update the template. Verify
   manually against a real episode (Macro Voices or Odd Lots in the
   local DB — both have chapters + insights + speaker takes
   populated).
3. Decide on `<details open>` vs always-expanded after seeing one or
   two real episodes rendered; tweak the template if any chapter
   feels too dense.
