# Inference Backend Evaluation: Ollama vs vLLM vs llama.cpp

**Date:** 2026-04-23
**Updated:** 2026-04-24

## Objective

Evaluate and implement multiple inference backends for the summarization pipeline. The production target is an RTX 4090 (24GB VRAM) running gemma4:e4b alongside WhisperX and an embedding model. Development is on an RTX 5090 (32GB).

## Status

- **Ollama:** Fully implemented, working with all models (gemma4:e4b, gemma4:26b, Qwen 3.6 27B)
- **vLLM:** Implemented and tested with gemma4:e4b. Qwen 3.6 27B in progress.
- **llama.cpp:** Not started. Deprioritized — Ollama uses llama.cpp under the hood, so the benefit is marginal unless we need batched serving with GGUF quants.

## Findings

### Ollama

**Pros:**
- Simple setup, model management built in (`ollama pull`)
- Fastest single-request inference for small models (GGUF Q4 = less memory bandwidth)
- Structured output works reliably after Ollama 0.21.1+ (fixed `think=false` + `format` conflict)
- Low VRAM footprint with Q4 quants — fits 26B MoE in 32GB with 65K context

**Cons:**
- Q4 quantization hurts quality noticeably (E4B missed speaker identification that bf16 got right)
- No batched serving — one request at a time
- No prefix caching across requests
- Limited control over memory allocation

**Verdict:** Best for development, single-request use, and models that don't fit in VRAM at higher precision.

### vLLM

**Pros:**
- Dramatically better output quality at bf16 (same model, 5x more detailed summary)
- OpenAI-compatible API — minimal client code
- Schema-enforced structured output via guided decoding
- Prefix caching across requests (transcript KV state reused across passes)
- Continuous batching for concurrent requests (critical for podcast queue)
- Native FP8 on RTX 5090 — near-bf16 quality at half memory

**Cons:**
- Heavy VRAM overhead — pre-allocates aggressively for KV cache
- 26B MoE at FP8 only gets ~24K context on 32GB (not enough for transcripts)
- Requires transformers>=5.6.2 for Gemma 4 support
- More complex setup (HuggingFace downloads, torch.compile warmup)
- `max_tokens` truncation can produce invalid JSON — needed repair logic

**Verdict:** Best for production serving. Quality uplift from bf16 is significant. Use for models that fit in VRAM at FP8+.

### llama.cpp (llama-server)

**Potential role:** Middle ground between Ollama and vLLM — GGUF memory efficiency with OpenAI-compatible batched serving and grammar-constrained JSON output.

**When to revisit:**
- If we need batched serving for GGUF quants (podcast queue on 4090)
- If Ollama's single-request limitation becomes the bottleneck
- The OpenAI-compatible API means our `--backend vllm` code works as-is with `--base-url`

## Implementation

Backend abstraction in `summarize.py`:

```python
@dataclass
class Backend:
    name: str       # "ollama" or "vllm"
    base_url: str
    model: str

    @staticmethod
    def ollama(model, base_url="http://localhost:11434"): ...

    @staticmethod
    def vllm(model, base_url="http://localhost:8000"): ...
```

CLI:
```bash
summarize transcript.txt --backend ollama --model gemma4:e4b
summarize transcript.txt --backend vllm --model google/gemma-4-E4B-it
summarize transcript.txt --backend vllm --base-url http://localhost:9000 --model Qwen/Qwen3.6-27B
```

### Thinking model handling

| Backend | Mechanism |
|---|---|
| Ollama | `"think": false` in request payload |
| vLLM | `"chat_template_kwargs": {"thinking": false}` in request (confirmed working) |

Models in thinking set: `qwen3`, `qwen3.5`, `qwen3.6`, `gemma4`

### Truncation handling

vLLM can truncate responses at `max_tokens`, producing invalid JSON. Added `_repair_truncated_json()` that finds the last complete JSON object in the response and closes any open brackets/braces.

## VRAM Planning

### RTX 5090 (32GB) — Development

| Model | Format | Weights | Max Context | Status |
|---|---|---|---|---|
| Gemma 4 E4B | bf16 | ~8 GB | 131K | Working |
| Gemma 4 E4B | Q4 GGUF | ~2.5 GB | 131K | Working (Ollama) |
| Gemma 4 26B | Q4 GGUF | ~13 GB | 65K | Working (Ollama) |
| Gemma 4 26B | FP8 | ~26 GB | ~24K | Too small for transcripts |
| Qwen 3.6 27B | Q4 GGUF | ~14 GB | 65K | Working (Ollama) |
| Qwen 3.6 27B | FP8 | ~28 GB | N/A | **OOM** — weights fill VRAM, no room for KV cache |

### RTX 4090 (24GB) — Production

| Component | Estimated VRAM |
|---|---|
| WhisperX (large-v3) | ~3 GB |
| Embedding model | ~1 GB |
| Gemma 4 E4B (bf16 via vLLM) | ~8 GB |
| KV cache (65K context) | ~10 GB |
| **Total** | **~22 GB** |

E4B at bf16 should fit on 4090 alongside WhisperX. This is the recommended production config — full model quality with enough context for transcripts.

## Key Takeaway

27B-class models do not fit on a single 32GB GPU via vLLM at any quantization level (bf16, FP8, or AWQ INT4) with usable context for long transcripts. vLLM's runtime overhead (CUDA graphs, profiling buffers, KV pre-allocation) is designed for datacenter GPUs and consumes memory that Ollama/llama.cpp avoids with purpose-built consumer GPU kernels.

**E4B on vLLM bf16 is the single-GPU production path** — it fits on both 5090 and 4090, delivers significantly better quality than Ollama Q4, and supports batched serving for the podcast queue.

### Multi-GPU options for 27B on vLLM

| Option | Total VRAM | Cost | Qwen 3.6 27B bf16? | Notes |
|---|---|---|---|---|
| 2x RTX 3090 (used) | 48 GB | ~$1,500 | FP8 yes, bf16 tight | No NVLink, PCIe tensor parallel |
| 2x RTX 5090 FE | 64 GB | ~$4,000 | Yes | No NVLink on consumer cards |
| 5090 + 4090 (existing) | 56 GB | Already owned | Possibly | Needs heterogeneous TP (experimental) |

The 5090 + 4090 path via the k3s GPU tower is the most cost-effective. Standard `--tensor-parallel-size 2` requires identical GPUs, but vLLM is working on heterogeneous support. Revisit when the cluster is up.

**Decision gate:** Run the multi-transcript eval first (E4B bf16 vs 26B Q4 vs Qwen Q4 across 3-5 episodes). If E4B bf16 is good enough, no hardware purchase needed.

## Remaining Work

- [ ] **Multi-transcript eval** — determine if E4B bf16 quality justifies staying single-GPU or if 27B warrants hardware investment
- [ ] Parallelize summarization passes 2-5 (independent after speaker ID) — vLLM batching makes this free
- [ ] Add tokenizer-based context chunking for models with limited context (enables 26B on vLLM for agent tasks)
- [ ] Test full stack on RTX 4090 (vLLM E4B bf16 + WhisperX + embeddings)
- [ ] Revisit 5090 + 4090 heterogeneous tensor parallel when vLLM support matures
- [ ] Evaluate llama.cpp server if batched GGUF serving is needed for 27B models
