# 🏆 Best Practices: LLM Backend Setup

Vishva works with any OpenAI-compatible backend (llama.cpp, Ollama, LM Studio).
For the best experience, two separate model servers are strongly recommended.

## Recommended: two servers

| Server | Model | Hardware | Purpose |
|---|---|---|---|
| **Main** (`:8080`) | 20–30B reasoning model | full GPU offload | chat, tool calls, code, vision |
| **Meta** (`:8090`) | small 3–4B model | CPU only (`-ngl 0`) | RAG enrichment/extraction, dedup checks, loop-rescue summaries |

**Why this split?**

- The meta model only processes tiny helper prompts (a few hundred tokens) —
  it needs neither GPU nor large context.
- Running it on CPU with low priority (`Nice=10`) means it never competes with
  the main model for VRAM or GPU cycles.
- A small CPU model responds to helper prompts in milliseconds while the GPU
  stays 100% free for your actual answers.

## Example systemd services (llama.cpp)

Adjust all paths (`<...>`) to your setup before installing.

`~/.config/systemd/user/llama-server.service` (main, full GPU):

    [Unit]
    Description=Llama Server (Vishva Main Model)
    After=network.target

    [Service]
    Type=simple
    WorkingDirectory=<path-to-llama.cpp>
    LimitMEMLOCK=infinity
    MemoryMax=32G
    MemorySwapMax=0
    ExecStart=<path-to-llama.cpp>/llama-server \
      -m <path-to-models>/main.gguf \
      --mmproj <path-to-models>/mmproj-BF16.gguf \
      -fa on \
      -c 32768 \
      -b 512 \
      -np 1 \
      -cb \
      -ngl 99 \
      --cache-type-k q8_0 \
      --cache-type-v q8_0 \
      --jinja \
      --sleep-idle-seconds 200
    Restart=on-failure
    RestartSec=45

    [Install]
    WantedBy=default.target

`~/.config/systemd/user/llama-server-meta.service` (meta, CPU-only):

    [Unit]
    Description=Vishva Meta-Model (CPU-only)
    After=network.target

    [Service]
    Nice=10
    WorkingDirectory=<path-to-llama.cpp>
    ExecStartPre=/usr/bin/sleep 3
    ExecStart=<path-to-llama.cpp>/llama-server \
      -m <path-to-models>/meta.gguf \
      -ngl 0 \
      -c 4096 \
      -t 16 \
      --port 8090 \
      --jinja \
      --sleep-idle-seconds 5
    Restart=on-failure
    RestartSec=45

    [Install]
    WantedBy=default.target

Matching config:

    {
      "base_url": "http://127.0.0.1:8080/v1",
      "meta_model_url": "http://127.0.0.1:8090/v1",
      "meta_model_name": "meta",
      "meta_context_size": 4096
    }

## No separate meta model? Disable the helpers

If you run only one server, do not point the meta tasks at your main
model (latency, thinking-token overhead, VRAM churn). Instead, disable the
meta-driven features completely:

    {
      "meta_model_url": "",
      "rag_meta_enrichment_enabled": false,
      "rag_meta_extraction_enabled": false
    }

Effect: no automatic RAG injection decisions and no background knowledge
extraction after turns. Manual memory still works (`rag_save`, `rag_search`,
`rag_update`), and dedup falls back to pure vector similarity. Loop-rescue
degrades gracefully to raw tool-trace summaries.

## Same setup with Ollama

Ollama can host both models in one instance — the meta model is pinned to
CPU via its Modelfile:

    # MetaModelfile
    FROM qwen3:4b
    PARAMETER num_gpu 0
    PARAMETER num_ctx 4096

    ollama create vishva-meta -f MetaModelfile

    {
      "base_url": "http://127.0.0.1:11434/v1",
      "meta_model_url": "http://127.0.0.1:11434/v1",
      "meta_model_name": "vishva-meta"
    }

**Alternative (strict isolation):** run a second Ollama instance on `:8090`
with GPUs hidden:

    OLLAMA_HOST=127.0.0.1:8090 CUDA_VISIBLE_DEVICES= ROCR_VISIBLE_DEVICES= ollama serve

Tip: set `OLLAMA_KEEP_ALIVE=5m` if VRAM/RAM is tight, so idle models unload.

## Same setup with LM Studio

LM Studio (0.3+) can serve multiple models simultaneously:

1. Load your main model, GPU offload = max, start a server on port `8080`.
2. Load a small model (e.g. Qwen3-4B), GPU offload = 0 (CPU), start a
   second server instance on port `8090`.
3. Point Vishva at both:

    {
      "base_url": "http://127.0.0.1:8080/v1",
      "meta_model_url": "http://127.0.0.1:8090/v1",
      "meta_model_name": "<loaded-small-model>"
    }

If your machine can't hold two models, use the single-server route and disable
the meta helpers as shown above.

## 🧪 Reference Setup (Author's Hardware)

A proven, production-tested configuration that runs Vishva fully locally.

### Hardware

| Component | Specification |
|---|---|
| CPU | AMD Ryzen 9 9900X (24 cores) @ 5.66 GHz |
| GPU | AMD Radeon RX 9070 XT (16 GB VRAM) |
| RAM | 64 GB |
| OS | CachyOS (Arch-based) |
| Backend | llama-server (Vulkan build) |

### Models

- **Main:** `gemma-4-26B-A4B-APEX-I-Compact.gguf`
  ([mudler/gemma-4-26B-A4B-it-APEX-GGUF](https://huggingface.co/mudler/gemma-4-26B-A4B-it-APEX-GGUF)),
  vision adapter `mmproj-BF16.gguf`, speculative draft model
  `mtp-gemma-4-26B-A4B-it-Q4_0_unsloth.gguf` enabled via
  `--draft-model <file> --spec-type draft-mtp`
- **Meta:** `Qwen3.5-4B-IQ4_NL.gguf`
  ([unsloth/Qwen3-4B-GGUF](https://huggingface.co/unsloth/Qwen3-4B-GGUF)),
  CPU-only (`-ngl 0`), `Nice=10`

### Performance

| Scenario | Tokens/s |
|---|---|
| Simple chat, short answers | ~90–100 t/s |
| Tool-heavy tasks (multiple bash/file calls) | ~70–85 t/s |
| Long reasoning / complex code generation | ~60–75 t/s |

Without a speculative draft model (`--draft-model`), expect roughly
40–55 t/s on the same hardware.

### Tips for similar setups

- **AMD GPUs:** use Vulkan/ROCm builds of llama-server; CUDA builds won't work.
- 32k context fits 16 GB VRAM with `q8_0` KV-cache; for 64k drop to `q4_0`.
- Pin the meta model to dedicated cores with `taskset` if it steals CPU cycles.
- `--sleep-idle-seconds` is critical — otherwise the big model holds VRAM forever.
- `temperature: 0.9` + `top_p: 0.95` gives the best creativity/tool-use balance
  for Gemma 4.

### Matching config

    {
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "<path-to>/gemma-4-26B-A4B-APEX-I-Compact.gguf",
      "context_size": 32768,
      "compression_threshold": 24576,
      "meta_model_url": "http://127.0.0.1:8090/v1",
      "meta_model_name": "Qwen3.5-4B-IQ4_NL",
      "meta_model_timeout": 60,
      "api_timeout": 600,
      "temperature": 0.9,
      "top_p": 0.95,
      "top_k": 64,
      "max_tool_turns": 18,
      "loop_rescue_reserve": 4000
    }
