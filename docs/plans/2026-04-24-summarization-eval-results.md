# Summarization Evaluation Results

**Date:** 2026-04-24/25

## Test Setup

- **Hardware:** RTX 5090 (32GB VRAM)
- **Transcript:** Market Huddle ep. 284 (Craig Shapiro), ~136KB, ~43-49K tokens depending on tokenizer
- **Pipeline:** Multi-pass summarization (speaker ID → summary → chapters → insights → speaker takes)
- **Backends tested:** Ollama 0.21.2 (GGUF Q4_K_M), vLLM 0.19.1 (bf16, AWQ)

## TL;DR Recommendation

**Qwen 3.6 35B-A3B MoE on Ollama Q4** is the best option for both RTX 5090 and potentially RTX 4090:
- Opus-quality analytical depth at ~210s per episode
- ~23GB VRAM — fits 5090, tight but potentially viable on 4090
- No vLLM setup needed, simpler operations
- 15-20 episodes/week = ~1 hour total processing time

Fallback: **Gemma 4 E4B bf16 on vLLM** if speed matters more than analysis quality (87s, 22GB).

## Full Model Comparison

| Model | Backend | VRAM | Time | Context | Quality | Summary | Ch | Ins | Takes |
|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 E4B | Ollama Q4 | ~12 GB | ~60s | 65K | Weak | 642 | 19 | 14 | 15 |
| Gemma 4 E4B | vLLM bf16 | ~22 GB | ~87s | 65K | Good (facts) | 3144 | 15 | 16 | 18 |
| Gemma 4 26B MoE | Ollama Q4 | ~15 GB | ~65s | 65K | OK | 771 | 16 | 13 | 12 |
| Qwen 3.6 27B dense | Ollama Q4 | ~19 GB | ~9 min | 65K | Good | 993 | 13 | 15 | 12 |
| **Qwen 3.6 35B MoE** | **Ollama Q4** | **~23 GB** | **~210s** | **65K** | **Excellent** | **3173** | **16** | **15** | **15** |
| Qwen 3.6 35B MoE | vLLM AWQ | ~30 GB | ~200s | 65K | Excellent | 2470 | 19 | 15 | 15 |
| Claude Opus | API | N/A | N/A | N/A | Excellent | 2425 | 16 | 10 | 11 |

### Quality tiers

**Tier 1 — Analytical (explains *why*, actionable insights):**
- Qwen 3.6 35B MoE (Ollama or vLLM) — e.g. "Trump needs 80% dollar depreciation for reshoring but rapid decline triggers commodity inflation"
- Claude Opus — e.g. "silver moved $27 in four days before reversing $16 in three hours"

**Tier 2 — Factual (explains *what*, accurate but surface-level):**
- E4B vLLM bf16 — verbose, covers all topics, but reads like a book report
- Qwen 3.6 27B dense Ollama — good analysis but superseded by MoE variant

**Tier 3 — Basic:**
- Gemma 4 26B Ollama, E4B Ollama — correct facts but shallow, some speaker ID failures

### Speaker identification

| Model/Backend | Identified Kevin Muir? | Speaker detail |
|---|---|---|
| E4B Ollama Q4 | No | Minimal |
| E4B vLLM bf16 | Yes | Basic (name, role) |
| 26B Ollama Q4 | Yes | Basic |
| Qwen 27B Ollama Q4 | Yes | Good (noted diarization artifacts) |
| **Qwen 35B MoE Ollama Q4** | **Yes** | **Best (titles, affiliations, show names)** |
| Qwen 35B MoE vLLM AWQ | Yes | Good |

## Hardware Compatibility

### RTX 4090 (24GB)

| Model | Backend | VRAM | Fits? | Context | Notes |
|---|---|---|---|---|---|
| E4B | vLLM bf16 | 22.4 GB | **Yes** (1.6GB headroom) | 65K, ~26K output room | Confirmed via simulation on 5090 |
| Qwen 35B MoE | Ollama Q4 | ~23 GB | **Tight** — needs testing | 65K | 1GB headroom on paper. May need reduced context. |
| Qwen 35B MoE | vLLM AWQ | ~30 GB | **No** | N/A | |
| Qwen 27B dense | Ollama Q4 | ~19 GB | Yes | 65K | But slow (~9 min) and superseded by MoE |
| 26B MoE | Ollama Q4 | ~15 GB | Yes | 65K | Lower quality |

**4090 recommendation:** Test Qwen 35B MoE on Ollama first. If it OOMs, fall back to E4B vLLM bf16.

### RTX 5090 (32GB)

| Model | Backend | VRAM | Context | Status |
|---|---|---|---|---|
| **Qwen 35B MoE** | **Ollama Q4** | **~23 GB** | **65K** | **Recommended — best quality/VRAM ratio** |
| Qwen 35B MoE | vLLM AWQ | ~30 GB | 65K | Working — better for batched serving |
| E4B | vLLM bf16 | ~22 GB | 131K | Working — fastest option |
| Qwen 27B dense | Any vLLM quant | 25-28 GB | 0-24K | Does not fit with usable context |

## Latency Breakdown

### Qwen 3.6 35B-A3B MoE — Ollama Q4 (~210s total)

| Pass | Time | Notes |
|---|---|---|
| Speaker ID | 32s | First pass, no cache |
| Summary | 38s | |
| Chapters | 45s | Slowest content pass |
| Insights | 56s | Most analytical pass |
| Speaker takes | 40s | |

Thinking overhead is included — can't disable thinking for Qwen on Ollama due to bug #14645. Once fixed, expect ~150-170s.

With parallel passes (vLLM batching), passes 2-5 could run concurrently: ~32s (speakers) + ~56s (slowest parallel pass) = ~88s total.

### Gemma 4 E4B — vLLM bf16 (~87s total)

| Pass | Time | Notes |
|---|---|---|
| Speaker ID | 12s | Fast — 4B model |
| Summary | 12s | |
| Chapters | 31s | |
| Insights | 20s | |
| Speaker takes | 17s | |

### Queue throughput (15-20 episodes/week)

| Config | Per episode | Weekly (20 eps) | Notes |
|---|---|---|---|
| Qwen MoE Ollama | ~210s | ~70 min | Sequential, single request |
| Qwen MoE vLLM (parallel passes) | ~88s | ~30 min | With concurrent passes after speaker ID |
| E4B vLLM bf16 | ~87s | ~29 min | Sequential, could batch further |

## Context Window Analysis

Our transcript produces different token counts per tokenizer:

| Tokenizer | Input tokens | Available output (65K ctx) |
|---|---|---|
| Gemma 4 | ~43K | ~22K |
| Qwen 3.6 | ~47-49K | ~16-18K |

Qwen's tokenizer is less efficient for English text (~3.1 chars/token vs Gemma's ~3.6 chars/token). This means Qwen needs more context for the same transcript. All tested configs had enough output headroom for our 5-pass pipeline (each pass outputs 500-2000 tokens).

For longer transcripts (3+ hour episodes), tokenizer-based chunking would be needed.

## vLLM vs Ollama: When to use which

| Factor | Ollama | vLLM |
|---|---|---|
| VRAM efficiency | Better (Q4 GGUF stays compressed) | Worse (runtime overhead) |
| Output quality at same quant | Similar | Similar |
| Structured output enforcement | Advisory (model can ignore) | Grammar-enforced |
| Batched serving | No (single request) | Yes (continuous batching) |
| Prefix caching | No | Yes (transcript KV reused across passes) |
| Setup complexity | Simple (`ollama pull`) | Complex (HF downloads, torch.compile) |
| Thinking model support | Broken for Qwen 3.x (#14645) | Works via `chat_template_kwargs` |

**For the podcast summarization pipeline:** Ollama is sufficient since we process one episode at a time and Qwen MoE Q4 quality matches vLLM AWQ. Switch to vLLM when batching multiple podcasts concurrently becomes a priority.

## Known Issues

### Ollama: Qwen 3.x thinking + structured output (issue #14645)
`think=false` + `format` schema is broken for Qwen 3.x on Ollama 0.21.2 (fixed for Gemma 4 in 0.21.1). Workaround: let Qwen think (don't send `think: false`). Adds ~20% latency but structured output works correctly.

### vLLM: Gemma 4 26B AWQ incompatibility
Community AWQ quants use `compressed-tensors` format that vLLM's Gemma 4 MoE loader can't parse (`KeyError: 'layers.0.moe.experts.0.down_proj_packed'`). Classic AWQ format may work when available.

### vLLM: Qwen 3.6 GDN memory inflation
Qwen 3.6's hybrid Gated Delta Network architecture inflates loaded model size well beyond the on-disk checkpoint size. NVFP4 quants advertised at ~19.7GB load at 27.38GB. This affects both dense 27B and MoE 35B variants.

### vLLM: E4B verbosity / truncation
E4B on vLLM generates very long outputs that can exceed `max_tokens`. Fixed with dynamic token counting — each request preflight-counts actual input tokens via a 1-token API call, then sets `max_tokens` to fill remaining context.

## Backend Architecture

Implemented `Backend` dataclass in `summarize.py` with factory methods:

```python
backend = Backend.ollama("qwen3.6:35b-a3b")
backend = Backend.vllm("QuantTrio/Qwen3.6-35B-A3B-AWQ")
backend = Backend.vllm("google/gemma-4-E4B-it", base_url="http://localhost:8000")
```

CLI: `summarize transcript.txt --backend ollama|vllm --model <name> --base-url <url> --json`

## Serve Commands

```bash
# Ollama — Qwen 3.6 35B MoE (recommended)
ollama pull qwen3.6:35b-a3b
summarize data/transcript.txt --model qwen3.6:35b-a3b --json

# vLLM — Qwen 3.6 35B MoE AWQ (5090 only, batched serving)
.venv/bin/vllm serve QuantTrio/Qwen3.6-35B-A3B-AWQ \
  --max-model-len 65536 --enforce-eager --max-num-seqs 1 \
  --gpu-memory-utilization 0.95 --port 8000

# vLLM — Gemma 4 E4B bf16 (fast, 4090+5090)
.venv/bin/vllm serve google/gemma-4-E4B-it \
  --dtype auto --max-model-len 65536 \
  --gpu-memory-utilization 0.70 --enforce-eager --max-num-seqs 1 --port 8000
```

Requires `transformers>=5.6.2` for Gemma 4 architecture support.

## Next Steps

- [ ] **Test Qwen 35B MoE Ollama Q4 on RTX 4090** — confirm ~23GB fits in 24GB
- [ ] **Run multi-transcript eval** — test across 3-5 diverse episodes to confirm quality rankings hold
- [ ] Build eval framework with Claude as judge to score summaries systematically
- [ ] Parallelize summarization passes 2-5 on vLLM (concurrent after speaker ID)
- [ ] Add tokenizer-based transcript chunking for 3+ hour episodes
- [ ] Revisit Ollama Qwen structured output when #14645 is fixed (expect ~20% speed improvement)
- [ ] Explore `--kv-cache-dtype fp8_e4m3` + CUDA toolkit for more vLLM context headroom

## Conclusion

We tested 6 model/backend combinations on a single RTX 5090 (32GB) for podcast transcript summarization. The core finding: **quantization precision matters more than parameter count**. E4B at bf16 (vLLM) outperformed 26B MoE at Q4 (Ollama) — a 4B model beating a 26B model because full-precision weights preserve the nuance needed for speaker identification and analytical reasoning.

The quality hierarchy is clear: Qwen 3.6 35B MoE and Claude Opus produce genuinely analytical summaries that explain *why* things matter, while smaller or more aggressively quantized models produce accurate but surface-level recaps.

**The VRAM wall is the hard constraint.** 27B+ dense models simply don't fit on 32GB at any precision that vLLM can serve with usable context. MoE architectures help (35B total / 3B active), but even those are tight. This is a single-GPU limitation — the k3s cluster with multiple GPUs or 48GB+ cards would change the calculus entirely.

**For production today:** Qwen 3.6 35B MoE on Ollama Q4 (~210s/episode, ~23GB) delivers the best quality. E4B on vLLM bf16 (~87s/episode, ~22GB) is the speed/batching option. Both need multi-transcript eval before a final call — one podcast isn't enough to judge, and different episode types (multi-guest panels, technical deep-dives, poor diarization) may shift the rankings.
