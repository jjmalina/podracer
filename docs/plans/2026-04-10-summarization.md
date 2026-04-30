# Podcast Summarization Model Evaluation

**Date:** 2026-04-10

## Objective

Evaluate local LLMs for podcast transcript summarization, with the constraint that the model must coexist on an RTX 4090 (24GB VRAM) alongside WhisperX (transcription) and an embedding model.

## VRAM Budget (RTX 4090 — 24GB)

| Component | Estimated VRAM |
|---|---|
| WhisperX (large-v3) | ~3 GB |
| Embedding model (nomic-embed-text or bge-small) | ~1 GB |
| LLM (summarization + Q&A) | ~9.6 GB |
| **Remaining headroom** | **~10 GB** |

## Models Tested

### qwen2.5:32b-instruct-q4_K_M — Failed

- **Size:** 19 GB
- **Context window:** 32,768 tokens
- **Result:** Transcript (~40k tokens) exceeded context window. Model truncated input and hallucinated content — fabricated "Bitcoin technical analysis" and "weather conditions" as key topics, missed the actual guest (Craig Shapiro) entirely, and called the named hosts "unspecified."
- **Verdict:** Unusable for this task due to insufficient context window. Would also be too large to coexist with WhisperX + embeddings on the 4090.

### gemma4:e4b — Selected

- **Size:** ~9.6 GB (8B params, 4.5B effective, Q4_K_M)
- **Context window:** 131,072 tokens
- **Generation speed:** ~159 tok/s on RTX 5090
- **Supports:** structured JSON output, tool use, vision, audio, thinking
- **Requires:** ollama >= 0.20.0

#### Summarization Quality

Tested on a ~136KB / 1,560 line / ~40k token transcript of Market Huddle episode 284 (Craig Shapiro interview).

**Strengths:**
- Correctly identified all speakers: Patrick Serezna, Kevin Muir (hosts), Craig Shapiro (guest)
- Accurately captured key topics: precious metals, global macro, dollar weakness, commodity cycles, Fed chair speculation
- Picked up specific predictions: dollar weakening, silver bull case (AI/solar demand), MAG7 overvaluation
- Identified nuanced market views: Japan/China capital rotation, fiat debasement thesis for gold

#### Factual Recall Test

**Question:** "Who helped Craig Shapiro get hired at SAC Capital, and what was unusual about how Steve Cohen hired him?"

**Result:** Correctly identified Matt Grossman (from Plural Investments), the phone call to Steve Cohen, and the fact that Cohen hired Shapiro without ever meeting him. Minor embellishment: called Grossman "a former SAC partner" when the transcript only says he "had worked at SAC prior."

#### Synthesis Test

**Question:** "Craig discusses several reasons why gold has disconnected from its traditional relationship with real rates. What are those reasons, and how does the situation in Venezuela fit into his thesis?"

**Result:** Successfully synthesized arguments spread across multiple sections of the transcript into a coherent thesis — Russia sanctions/expropriation, gold as neutral reserve asset, Venezuela as a case study for US aggression pushing holders away from dollar assets. Missed the Asian overnight demand dynamic and Trump's explicit desire for a weaker dollar as separate contributing factors, but overall demonstrated strong reasoning across the full context.

## Configuration

Ollama defaults to a 4k context window. Must explicitly set `num_ctx` when using the API:

```python
requests.post("http://localhost:11434/api/generate", json={
    "model": "gemma4:e4b",
    "prompt": "...",
    "stream": False,
    "options": {"num_ctx": 65536}
})
```

Structured output is supported via the `format` parameter with a JSON schema:

```python
requests.post("http://localhost:11434/api/generate", json={
    "model": "gemma4:e4b",
    "prompt": "...",
    "stream": False,
    "format": {
        "type": "object",
        "properties": { ... },
        "required": [...]
    },
    "options": {"num_ctx": 65536}
})
```

## Next Steps

- Wire up the full pipeline: audio -> WhisperX transcription -> gemma4:e4b summarization
- Test concurrent VRAM usage on the 4090 with all three models loaded
- Evaluate generation speed on the 4090 (expect slower than 5090 due to lower memory bandwidth)
- Define structured output schema for summaries (topics, speakers, predictions, key quotes)
- Select and test embedding model for RAG/search over transcripts
