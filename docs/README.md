# Podracer

A local-first podcast and video transcription platform with speaker diarization, semantic search, and AI-powered insights.

## The Problem

I consume ~20 hours of podcasts per week, plus YouTube content. That's a massive amount of information that's locked in audio format:

- **Can't search it** - "What did that guest say about interest rates?" requires scrubbing through hours of audio
- **Can't reference it** - No way to quote or link to specific moments
- **Can't connect ideas** - Insights across episodes stay siloed in my memory
- **Cloud APIs are expensive** - At 80+ hours/month, transcription services cost $20-100+/month
- **Privacy concerns** - Sending all my listening habits to third parties

## The Solution

Run the heavy lifting locally, use APIs only for the "smart" tasks:

- **Transcription** with speaker diarization (local GPU)
- **Embeddings** for semantic search (local GPU)
- **Summarization** with local LLM (local GPU)
- **Searchable archive** of everything I've listened to
- **AI chat/agents** via Claude API (only when needed)

**Cost model**: Heavy batch processing (transcription, embedding, summarization) runs locally on a dual-GPU server (RTX 4090 + RTX 5060 Ti) for ~$7/month electricity. API calls only for interactive queries and complex reasoning - minimal compared to running everything through cloud APIs.

## Current MVP

A CLI tool that transcribes audio files with speaker diarization using WhisperX and pyannote.

### Features

- Local transcription using Whisper (small model by default)
- Speaker diarization - identifies different speakers
- Word-level timestamps
- GPU acceleration (CUDA) or CPU fallback
- Outputs structured text with speaker labels

### Usage

```bash
# With speaker diarization
uv run transcribe podcast.mp3 -o transcript.txt

# Without diarization (faster)
uv run transcribe podcast.mp3 --no-diarize -o transcript.txt

# On CPU (for machines without NVIDIA GPU)
uv run transcribe podcast.mp3 --device cpu --compute-type int8
```

### Performance

On an RTX 5090:
- 2-hour podcast: ~7-8 minutes to transcribe with diarization
- ~15x realtime speed

### Requirements

- Python 3.10-3.13
- NVIDIA GPU with CUDA (or CPU fallback)
- ~2GB disk for models (downloaded on first run)
- HuggingFace token for diarization (free, requires accepting model license)

### Setup

```bash
# Install dependencies
uv sync

# Add HuggingFace token for diarization
mkdir -p .credentials
echo "hf_your_token_here" > .credentials/hf_token

# Accept model licenses at:
# - https://huggingface.co/pyannote/speaker-diarization-3.1
# - https://huggingface.co/pyannote/segmentation-3.0
```

## The Vision

Podracer will evolve into a full knowledge management platform for audio/video content:

### Planned Features

1. **Automatic ingestion**
   - Monitor podcast RSS feeds
   - Watch YouTube subscriptions
   - Auto-download new episodes

2. **Processing pipeline**
   - Transcription with diarization
   - Generate embeddings for semantic search
   - AI summarization (key points, topics, quotes)
   - Entity extraction (people, companies, concepts)

3. **Search & discovery**
   - Full-text search across all transcripts
   - Semantic search ("episodes about startup fundraising")
   - Filter by speaker, show, date, topic

4. **Web interface**
   - Browse transcripts with audio player sync
   - Highlight and annotate
   - Export quotes and clips

5. **AI integration**
   - Chat with your podcast archive
   - "What has Patrick O'Shaughnessy said about value investing?"
   - MCP server for Claude/AI agent access

## Why Local?

### Cost comparison (at 80 hours/month)

#### Transcription + Diarization only

| Service | Rate | Monthly (80 hrs) |
|---------|------|------------------|
| AWS Transcribe | $0.024/min | ~$115 |
| Google Speech-to-Text | $0.016/min + diarization | ~$95 |
| Deepgram | $0.0043/min + features | ~$25-40 |
| AssemblyAI | $0.15/hr + $0.02/hr diarization | **~$13.60** |
| **Local (electricity only)** | ~450W × 8min per 2hr file | **~$0.40** |

AssemblyAI is surprisingly cheap for transcription alone: **$13.60/month** for 80 hours.

#### But the full pipeline costs more

Podracer isn't just transcription - it's transcription + embeddings + summarization + search. Here's what the full cloud stack would cost:

| Service | What | Rate | Monthly (80 hrs) |
|---------|------|------|------------------|
| AssemblyAI | Transcription + diarization | $0.17/hr | $13.60 |
| OpenAI Embeddings | text-embedding-3-small | $0.02/1M tokens | ~$2-4 |
| Claude/GPT-4 | Summarization (~2K tokens out per episode) | $15/1M output tokens | ~$12-15 |
| Vector DB (cloud) | Pinecone/Weaviate hosted | $25-70/month | ~$25 |
| **Total cloud stack** | | | **~$55-110/month** |

#### Hardware investment vs cloud

| | Upfront | Monthly | Break-even vs full cloud |
|--|---------|---------|--------------------------|
| Full cloud stack | $0 | ~$55-110 | - |
| Local hardware (dual GPU) | ~$2,050 | ~$7 (electricity) | **17-42 months** |

If you only needed transcription, AssemblyAI at $13.60/month is hard to beat - break-even would be 12+ years.

But for the full knowledge platform (transcription + embeddings + summarization + vector search + privacy + no rate limits), local hardware pays for itself in **~2-3 years** and then runs at just the cost of electricity (~$7/month). The same hardware also serves OpenClaw (local AI agent), voice assistant, and other inference workloads -- amortizing the cost further.

#### What the numbers don't capture

- **Privacy**: Your listening habits never leave your network
- **No vendor lock-in**: APIs change pricing, get deprecated, or add restrictions
- **Unlimited usage**: No per-request costs means you can re-process, experiment, run larger models
- **Learning value**: Building real K8s infrastructure skills
- **Resale value**: Hardware retains value; API spend is gone forever
- **Multi-use**: Same hardware runs other workloads (local LLMs, image gen, game server, etc.)

### Other benefits

- **Privacy** - Content never leaves my network
- **No rate limits** - Process as much as I want
- **No API dependencies** - Works offline, no service changes
- **Learning opportunity** - Build real infrastructure skills (K8s, distributed systems)
- **Reusable hardware** - Same GPUs can run local LLMs, image generation, etc.

## Target Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           K8s Cluster (k3s)                              │
├────────────────────────────┬─────────────────────────────────────────────┤
│  SER9 Max (CPU node)       │  GPU Tower (Dual GPU node, Proxmox + K8s)  │
│  ├── Control plane         │  ┌─ RTX 4090 (24GB) ────────────────────┐  │
│  ├── Feed watcher          │  │  ├── LLM inference (Ollama)          │  │
│  ├── Download workers      │  │  │   ├── Summarization (Qwen3 14B)   │  │
│  ├── Vector DB (Qdrant)    │  │  │   ├── OpenClaw agent              │  │
│  ├── Search (Meilisearch)  │  │  │   └── Voice assistant             │  │
│  ├── Web app               │  │  └── Heavy batch inference           │  │
│  └── API gateway ──────────│──│──────────────────────► Claude API     │  │
│                            │  ├─ RTX 5060 Ti (16GB) ─────────────────┤  │
│                            │  │  ├── Transcription (WhisperX)        │  │
│                            │  │  ├── Diarization (pyannote)          │  │
│                            │  │  ├── Embeddings (BGE/nomic)          │  │
│                            │  │  └── Light LLM fallback (8B)         │  │
│                            │  └──────────────────────────────────────┘  │
├────────────────────────────┴─────────────────────────────────────────────┤
│                            Mini NAS (NFS)                                │
│  ├── /media      - raw audio/video files                                │
│  ├── /transcripts - processed output                                    │
│  └── /backups                                                           │
└──────────────────────────────────────────────────────────────────────────┘
```

### Hardware

| Node | Specs | Role |
|------|-------|------|
| Beelink SER9 Max | Ryzen 7, 32GB DDR5, AMD 780M | K8s control plane + general services |
| GPU Tower | Ryzen 9 9900X, 32GB DDR5, RTX 4090 + RTX 5060 Ti 16GB | Dual GPU workloads (Proxmox + K8s) |
| Mini NAS | 2TB storage | Shared filesystem (NFS) |
| Main workstation | RTX 5090, 10GbE | Development, gaming (not part of cluster) |

#### GPU Tower Build

Full ATX dual-GPU build. The RTX 4090 handles LLM inference (Ollama) while the RTX 5060 Ti handles transcription, embeddings, and light inference. Runs Proxmox as the hypervisor with K8s in a VM for workload scheduling. Microcenter bundle deal makes this cost-effective despite DDR5 shortage pricing.

| Part | Spec | Price |
|------|------|-------|
| CPU + Mobo + RAM | Ryzen 9 9900X + X870E ATX + 32GB DDR5 (bundle) | ~$650-700 |
| Case | ATX mid-tower (Fractal Meshify 2 or similar) | ~$100-130 |
| Storage | Samsung 990 Pro 2TB NVMe | ~$300 |
| Cooler | Noctua NH-D15 | ~$133 |
| PSU | Corsair RM1200x 1200W | ~$250 |
| GPU 1 | RTX 4090 (already owned) | - |
| GPU 2 | RTX 5060 Ti 16GB (Gigabyte Gaming OC) | ~$550 |
| **Total** | | **~$2,000-2,050** |

The 9900X (12C/24T) is overkill for a GPU worker node, but the bundle pricing makes it worthwhile. Side benefit: doubles as a backup gaming/workstation if needed. The 1200W PSU provides comfortable headroom for dual-GPU transient power spikes (4090 TDP 450W + 5060 Ti TDP 180W + system ~170W = ~800W sustained, up to ~1,030W transient).

### Networking

| Device | Network speed |
|--------|---------------|
| 10GbE Switch | Core network |
| Main workstation | 10 Gbps |
| Beelink SER9 Max | 10 Gbps |
| GPU Tower | 10 Gbps (add 10GbE NIC or use motherboard 2.5GbE) |
| Mini NAS | 2x 2.5 Gbps (5 Gbps bonded) |

The 10GbE backbone means GPU node ↔ storage transfers won't bottleneck on network. A 2-hour podcast (~200MB) transfers in under a second between the fast nodes. The NAS is the slowest link at 5Gbps bonded, but still fast enough that storage I/O won't be the limiting factor for audio/transcript files.

### AI Model Strategy

With dual GPUs, each card runs dedicated workloads without contention:

#### RTX 4090 (24GB) -- LLM inference

| Model | VRAM | Purpose |
|-------|------|---------|
| Qwen3 14B (Q4_K_M) | ~11GB | Summarization, key points, entity extraction |
| Qwen3-Coder 32B (Q4_K_M) | ~22GB | OpenClaw agent, agentic workflows |
| Qwen3 14B (Q4_K_M) | ~11GB | Voice assistant (when not running 32B) |

The 4090 stays loaded with an LLM via Ollama 24/7, serving OpenClaw cron jobs, voice assistant queries, and podcast summarization. Qwen3 14B is the sweet spot -- near-GPT-4 tool-calling reliability (0.971 F1) with room for large context windows. Swap to Qwen3-Coder 32B for heavier agentic work.

#### RTX 5060 Ti (16GB) -- Transcription + embeddings

| Model | VRAM | Purpose |
|-------|------|---------|
| WhisperX (large-v3) | ~6GB | Transcription + diarization |
| pyannote 3.1 | ~1GB | Speaker diarization (runs alongside Whisper) |
| BGE / nomic-embed | ~0.5-1GB | Embedding for vector search |
| Qwen3 8B (Q4_K_M) | ~6GB | Light fallback LLM if 4090 is busy |

The 5060 Ti's 16GB VRAM and Blackwell FP4 support make it ideal for the batch pipeline. Whisper + embeddings fit simultaneously with room to spare.

#### Runs via API (Claude)

| Task | Why API? |
|------|----------|
| Chat with archive | Quality matters more than cost for interactive use |
| Complex analysis | "Compare how guests X and Y think about topic Z" |
| Multi-step reasoning | When local models aren't confident enough |

Cloud APIs are reserved for interactive queries and complex reasoning where frontier model quality matters. With Qwen3 14B handling summarization and tool-calling locally, API usage is minimal.

#### Concurrent dual-GPU pipeline

With two GPUs, the batch pipeline no longer needs to sequence model loads:

1. **RTX 5060 Ti**: Transcribe audio (WhisperX) → generate embeddings (BGE)
2. **RTX 4090**: Summarize transcript (Qwen3 14B) → extract entities
3. Both run in parallel -- the 5060 Ti can start transcribing the next episode while the 4090 summarizes the previous one

The 4090 also handles OpenClaw and voice assistant requests between summarization jobs, since LLM inference is bursty and leaves plenty of idle time.

Each 2-hour podcast takes ~10-15 minutes through the full pipeline. With parallel processing, throughput is nearly doubled vs the single-GPU sequential approach.

### Why K8s?

- Multiple services that need to communicate
- Job queue for async processing (transcription jobs)
- GPU scheduling with taints/tolerations
- Learning opportunity - real project to build K8s skills
- Future scalability (add more nodes if needed)

## Project Status

- [x] MVP transcription CLI with diarization
- [x] Docker support
- [ ] Job queue for batch processing
- [ ] Feed watcher (RSS/YouTube)
- [ ] Vector search integration
- [ ] Web UI
- [ ] K8s deployment manifests
- [ ] AI agent API (MCP server)

## Development

```bash
# Run locally
uv run transcribe audio.mp3 -o transcript.txt

# Build Docker image
docker build -t podracer .

# Run in Docker (with GPU)
docker run --gpus all \
  -v $(pwd):/data \
  -e HF_TOKEN=$(cat .credentials/hf_token) \
  podracer /data/audio.mp3 -o /data/transcript.txt
```
