# Phase 1c: Summarization CLI + Eval

## Goal

Build a CLI that summarizes podcast transcripts into structured JSON using a local LLM, plus an eval harness using Claude as a judge.

## Prior Work

Model evaluation is documented in [2026-04-10-summarization.md](2026-04-10-summarization.md):
- **Selected model**: gemma4:e4b via Ollama (128k context, ~9.6GB VRAM)
- **Tested on**: Market Huddle Ep 284 (Craig Shapiro) — ~40k tokens
- **Results**: Accurate speaker identification, topic extraction, factual recall, and cross-section synthesis
- **Config**: Must set `num_ctx: 65536` (Ollama defaults to 4k)

## Summarization CLI

### Usage

```
$ podracer summarize transcript.txt
$ podracer summarize transcript.txt --output summary.json
$ podracer summarize transcript.txt --json   # stdout as JSON (for agent consumption)
```

### Output Schema

```json
{
  "schema_version": "1.0",
  "source_file": "transcript.txt",
  "model": "gemma4:e4b",
  "generated_at": "2026-04-14T12:00:00Z",
  "show": {
    "title": "The Market Huddle",
    "episode": "Ep 284"
  },
  "speakers": [
    {
      "label": "SPEAKER_00",
      "name": "Patrick Ceresna",
      "role": "host"
    },
    {
      "label": "SPEAKER_01",
      "name": "Kevin Muir",
      "role": "host"
    },
    {
      "label": "SPEAKER_02",
      "name": "Craig Shapiro",
      "role": "guest"
    }
  ],
  "summary": "One paragraph executive summary of the episode.",
  "topics": [
    {
      "name": "Gold and Precious Metals",
      "summary": "Discussion of gold's disconnect from real rates...",
      "speakers": ["Craig Shapiro", "Kevin Muir"]
    }
  ],
  "key_quotes": [
    {
      "speaker": "Craig Shapiro",
      "quote": "The exact quote from the transcript.",
      "context": "Why this quote matters."
    }
  ],
  "predictions": [
    {
      "speaker": "Craig Shapiro",
      "prediction": "Dollar will weaken significantly over next 12 months",
      "reasoning": "Brief explanation of the reasoning given."
    }
  ],
  "entities": {
    "people": ["Craig Shapiro", "Steve Cohen", "Matt Grossman"],
    "companies": ["SAC Capital", "Plural Investments"],
    "concepts": ["fiat debasement", "commodity supercycle", "MAG7 overvaluation"]
  }
}
```

Use Ollama's structured output (`format` parameter with JSON schema) to enforce this shape.

### Implementation

```
podracer/
  summarize.py    # Ollama client, prompt construction, structured output parsing
```

Key functions:
- `summarize_transcript(transcript_text, model, ollama_url, num_ctx)` — sends prompt to Ollama, returns structured dict
- `build_prompt(transcript_text)` — system prompt + transcript, instructs model on output schema
- CLI entrypoint wired to `podracer summarize`

### Prompt Strategy

System prompt defines the task and output schema. The transcript is passed as user content. Key instructions:
- Identify speakers by name when possible (from context clues in the transcript)
- Extract direct quotes verbatim — do not paraphrase
- Predictions must include the reasoning given by the speaker
- Topics should cover all major subjects discussed, not just the first few

## Eval: Claude as Judge

### Approach

For each eval sample, Claude receives the source transcript + the generated summary and grades it on multiple dimensions.

### Eval Dataset Format

```
eval/summarization/
  sample-01/
    transcript.txt       # Source transcript
    golden_summary.json  # (optional) Hand-verified reference summary
    metadata.json        # { "source": "Market Huddle Ep 284", "duration": "1h42m" }
  sample-02/
    ...
```

### Grading Dimensions

Claude scores each summary 1-5 on:

| Dimension | What it measures |
|-----------|-----------------|
| **Factual accuracy** | Are all claims in the summary actually stated in the transcript? No hallucinations? |
| **Completeness** | Does the summary cover all major topics discussed? Missing anything important? |
| **Speaker attribution** | Are quotes and views attributed to the correct speakers? |
| **Conciseness** | Is the summary appropriately concise without losing important detail? |
| **Quote accuracy** | Are "key quotes" actually verbatim from the transcript? |

### Eval Output

```
$ podracer eval-summarize --dataset eval/summarization/

Summarization Eval (judge: claude-sonnet-4-20250514)
════════════════════════════════════════════

  Sample      Accuracy  Complete  Speakers  Concise  Quotes  Avg
  ──────────  ────────  ────────  ────────  ───────  ──────  ────
  sample-01   5/5       4/5       5/5       4/5      3/5     4.2
  sample-02   4/5       4/5       4/5       5/5      4/5     4.2
  ──────────  ────────  ────────  ────────  ───────  ──────  ────
  Aggregate   4.5       4.0       4.5       4.5      3.5     4.2

Run with --json for machine-readable output.
```

With `--json`: full scores + Claude's reasoning per dimension per sample.

### Golden Summary Comparison

When a `golden_summary.json` exists, also compute:
- **Topic overlap**: what % of golden topics appear in generated summary
- **Speaker match**: did we identify the same speakers
- **Entity recall**: what % of golden entities were extracted

This gives a fast, API-free regression check alongside the Claude judge scores.

### Implementation

```
podracer/
  eval/
    summarization.py   # Load dataset, run summarization, call Claude judge, report
```

Key functions:
- `load_eval_dataset(path)` — reads sample dirs
- `judge_summary(transcript, summary, model="claude-sonnet-4-20250514")` — calls Claude API, returns scores + reasoning
- `compare_to_golden(generated, golden)` — topic/speaker/entity overlap metrics
- `run_eval(dataset_path, ollama_model, judge_model)` — orchestrate

### Claude API Usage

- Use `anthropic` Python SDK
- Model: `claude-sonnet-4-20250514` (good balance of quality and cost for judging)
- Structured output via tool use or JSON mode
- Cost estimate: ~$0.01-0.05 per eval sample (small transcript excerpts in the prompt)

## Model Provider Abstraction

Summarization must work across any LLM provider — Ollama (local), OpenAI, Anthropic, Gemini, OpenRouter, etc. For Phase 1, we talk to Ollama directly. When we generalize in Phase 2, the leading option is:

**Pydantic AI** — lightweight library by the Pydantic team. Provides a unified interface across providers with structured output via Pydantic models. Natural fit since we already define Pydantic models for the summary schema — the LLM call returns a validated `PodcastSummary` object regardless of which provider generated it.

Other options considered:
- **LiteLLM** — unified `completion()` across 100+ providers. Lower-level (no structured output validation), so you'd pair it with Pydantic yourself.
- **Instructor** — patches provider SDKs to return Pydantic models. Similar to Pydantic AI, different approach.
- **LangChain** — heavy dependency tree, too much abstraction for this use case.

**Phase 1**: Ollama directly via `requests`, output validated with Pydantic models.
**Phase 2**: Swap to Pydantic AI (or similar), keeping the same Pydantic output schemas. The provider becomes a config value (`summarization.provider` + `summarization.model`).

## Dependencies

### Phase 1

| Package | Purpose |
|---------|---------|
| `requests` | Ollama HTTP API |
| `pydantic` | Output schema validation |
| `anthropic` | Claude API for eval judging |

### Phase 2 (provider abstraction)

| Package | Purpose |
|---------|---------|
| `pydantic-ai` | Unified LLM calls with structured output across all providers |

## Acceptance Criteria

- [ ] `podracer summarize transcript.txt` produces valid structured JSON
- [ ] Output matches the defined schema (all fields present, correct types)
- [ ] `--json` flag outputs to stdout for agent consumption
- [ ] Eval dataset with 3+ samples exists
- [ ] `podracer eval-summarize` runs Claude judge and prints scorecard
- [ ] `--json` flag on eval outputs machine-readable results
- [ ] Structured output from Ollama is enforced (not just hoped for)
