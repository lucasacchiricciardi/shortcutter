---
title: Hardware setup
description: The exact mini PC this pipeline runs on — a Minisforum AI X1-255 upgraded to 64 GB — with the real ROCm and Ollama configuration that makes its integrated GPU usable for local LLMs.
order: 3
updated: "2026-04-30"
---

This pipeline was built and tested on a single specific machine: a [Minisforum AI X1-255](https://www.amazon.it/MINISFORUM-AI-X1-255-Oculink-Support/dp/B0F8HCZMB9), upgraded from the stock 32 GB to 64 GB of RAM. What follows is the exact hardware, the exact configuration, and the exact performance you can expect to reproduce.

## The reference machine

**Minisforum AI X1-255** — a 0.67 kg mini PC, ~130 × 126 × 47 mm, that fits in one hand and runs the entire pipeline with no external GPU.

| Component | Detail |
|-----------|--------|
| **Model** | Minisforum AI X1-255 ([Amazon IT — €775 stock 32 GB / 1 TB](https://www.amazon.it/MINISFORUM-AI-X1-255-Oculink-Support/dp/B0F8HCZMB9), [Minisforum store](https://store.minisforum.com/products/minisforum-ai-x1-mini-pc)) |
| **CPU** | AMD Ryzen 7 255 — 8 cores / 16 threads, Zen 4 |
| CPU base / boost clock | 3.8 GHz / 4.9 GHz |
| CPU L3 cache | 16 MB |
| CPU TDP | up to 65 W (configurable) |
| **GPU** | AMD Radeon 780M iGPU (RDNA 3, gfx1103) |
| GPU compute units | 12 CUs |
| GPU clock | up to 2700 MHz |
| **RAM (upgraded)** | **64 GB DDR5 SODIMM @ 5600 MT/s, dual-channel** (2× 32 GB, CHANNEL A + B) |
| RAM configuration verified | `dmidecode -t 17` confirms two 32 GB modules in P0 CHANNEL A and P0 CHANNEL B |
| **Storage** | 1 TB M.2 2280 PCIe 4.0 NVMe SSD (second slot free, max 8 TB total) |
| **Network** | 2.5 GbE RJ45 + Wi-Fi 7 + Bluetooth 5.4 |
| **Display I/O** | HDMI 2.1 + DP 2.0 + 2× USB4 (4K@120 Hz, supports 4 displays) |
| **eGPU** | OCuLink SFF8611 (PCIe 4.0 ×4) — supports external GPU dock |
| **OS** | Ubuntu 24.04 LTS |
| **Form factor** | Mini PC, 130 × 126 × 47.2 mm, 0.67 kg |
| **PSU** | 19 V / 6.32 A — 120 W external brick |

The X1-255 ships from Amazon in 32 GB / 1 TB, 64 GB / 1 TB, or barebone (no RAM, no SSD) configurations. The reference machine is the 32 GB SKU upgraded post-purchase to 64 GB by swapping the single 32 GB SODIMM for a 2× 32 GB matched dual-channel kit. **Upgrading is strongly recommended for LLM workloads** — see the channel verification section below for why.

The X1-255 is the entry-level variant in Minisforum's AI X1 line. Higher-end siblings (AI X1 Pro with Ryzen AI 9 HX 370, Radeon 890M, 80 TOPS NPU) exist for users who need dedicated AI silicon — but for this pipeline, the 255 is enough.

## Why a mini PC for LLMs

Three reasons this form factor works for local AI:

- **Always-on, low power.** ~35 W idle, ~85 W under sustained inference. A traditional workstation idles at 120 W just sitting there. Run 24/7 without the power bill spike.
- **Fits anywhere.** Lives on a shelf next to the router. Quiet under normal load, ~45 dB at full sustained tilt.
- **Upgrade path via OCuLink.** When the 780M iGPU stops being enough, the OCuLink port (PCIe 4.0 ×4) accepts an external GPU dock with a desktop-class card. Same machine, dramatically more compute. No need to replace the box.

The trade-off is honest: the 780M is an integrated GPU. It is slow compared to a discrete RTX 4070 or even a dedicated RX 7600. But it is **enough** to run 4B and 7B models locally if you treat the memory budget carefully (more on this below).

## How the iGPU sees memory (UMA + GTT)

The 780M is an integrated GPU. **It has no dedicated VRAM.** It shares system memory with the CPU through what AMD calls UMA (Unified Memory Architecture). On Linux there are two distinct pools the GPU can draw from:

1. **UMA Frame Buffer** — a static slice of RAM that the BIOS/UEFI reserves for the GPU at boot. Set in BIOS under `Advanced → AMD CBS → NBIO → GFX Configuration`. On the reference machine this is small (~10 GB carved out of the 64 GB), which is why `free -h` reports `54Gi` total even though physical DDR5 is 64 GB.
2. **GTT (Graphics Translation Table)** — a dynamic pool of system RAM the GPU can map on-demand at runtime. This is where the bulk of the "VRAM" for LLM inference comes from. Sized roughly half of total RAM by default on Linux.

For LLM workloads, **GTT is what matters**. With 64 GB physical DDR5 in this machine, the GPU can address up to ~46 GB of GTT-allocated memory dynamically — far more than the largest model in the pipeline (llava 7B at ~5.5 GB loaded), with comfortable headroom for keeping multiple models warm and large KV caches.

### The math, transparently

| Total physical RAM | 64 GB |
|---|---|
| BIOS UMA Frame Buffer (reserved at boot) | ~10 GB |
| Visible to Linux kernel (`free -h` total) | ~54 GiB |
| GTT pool exposed to GPU (max) | ~46 GB |
| Largest model loaded (llava 7B) | ~5.5 GB |
| Headroom for KV caches + 2 warm models | comfortable |

This is **why upgrading from stock 32 GB to 64 GB matters**. With only 32 GB physical, the GTT pool would max out around ~22 GB — still enough for any single model, but tight when keeping multiple models warm via `OLLAMA_MAX_LOADED_MODELS`.

### Verify what your system actually exposes

```bash
free -h
# Mem:  54Gi total — this is the effective system view (RAM minus UMA Frame Buffer)

rocm-smi
# Shows the GPU as gfx1103, with VRAM% reflecting current GTT usage

sudo dmidecode -t 17 | grep -E 'Size|Locator|Configured'
# Confirms physical SODIMM configuration
```

You don't need to enlarge the BIOS UMA Frame Buffer. Linux + ROCm + Ollama handle the dynamic allocation through GTT cleanly, as long as Ollama is told the upper bound (next section).

## ROCm and Ollama setup (real config)

The 780M is **not an officially-supported ROCm target**. AMD's HIP runtime targets dGPUs and a subset of APUs. To get the iGPU working, override the architecture identifier and pass several stability flags. This is the actual `override.conf` running on the reference machine:

```ini
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"
Environment="HSA_ENABLE_SDMA=0"
Environment="AMD_SERIALIZE_KERNEL=1"
Environment="OLLAMA_MAX_VRAM=49392123904"
Environment="OLLAMA_INTEL_GPU=0"
Environment="OLLAMA_HOST=0.0.0.0"
Environment="OLLAMA_MAX_LOADED_MODELS=6"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

Reload and restart after editing:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

### What each variable does

- `HSA_OVERRIDE_GFX_VERSION=11.0.0` — tells ROCm to treat the 780M (gfx1103) as if it were a supported gfx1100 (RDNA 3 dGPU). This is the single most important line.
- `HSA_ENABLE_SDMA=0` — disables SDMA (System DMA) engine. On 780M iGPUs SDMA can hang under sustained load; turning it off trades a small amount of throughput for stability.
- `AMD_SERIALIZE_KERNEL=1` — forces sequential kernel execution. Eliminates a class of race conditions specific to integrated GPUs.
- `OLLAMA_MAX_VRAM=49392123904` — caps the VRAM Ollama reports as available to ~46 GB (the value is in bytes). This sets the GTT pool ceiling Ollama is allowed to address. With 64 GB of physical RAM and ~10 GB reserved by the UMA Frame Buffer, ~46 GB is the realistic upper bound. The kernel still arbitrates real-time allocation between CPU and GPU; this cap just prevents Ollama from artificially refusing to load larger contexts.
- `OLLAMA_INTEL_GPU=0` — disables Intel GPU detection (irrelevant on AMD).
- `OLLAMA_HOST=0.0.0.0` — exposes Ollama on all interfaces. Drop this back to `127.0.0.1` if you don't need network access.
- `OLLAMA_MAX_LOADED_MODELS=6` — keeps up to 6 models warm in memory. Speeds up multi-stage pipelines significantly. Only feasible with 64 GB physical RAM.
- `OLLAMA_NUM_PARALLEL=2` — allows 2 parallel inference requests. Useful for hybrid workflows but pure pipeline use rarely benefits.
- `OLLAMA_KV_CACHE_TYPE=q8_0` — quantizes the KV cache to 8-bit integers. Roughly halves the memory footprint of long contexts with negligible quality loss for the model sizes used here.

### Verify the setup

```bash
rocm-smi
```

Expected output:

```
GPU  Temp     AvgPwr  SCLK  MCLK     Fan  Perf  PwrCap       VRAM%  GPU%
0    25.0c    3.182W  None  2800Mhz  0%   auto  Unsupported  1%     0%
```

The `Exception caught: map::at` and `GPU[0] : sclk clock is unsupported` messages are normal on the 780M — they reflect telemetry features that the iGPU doesn't expose. Inference works fine despite them.

For real-time monitoring use `radeontop` or `amdgpu_top`:

```bash
sudo apt install radeontop
sudo radeontop
```

You'll see the 780M working at 95–100% during transcription and inference.

## Model memory footprint

The pipeline uses three models. With 64 GB of physical RAM and a generous GTT cap, all three can coexist warm in memory, but the pipeline still unloads explicitly between stages to avoid contention:

| Model | Disk size | Approx loaded |
|-------|-----------|---------------|
| `whisper-medium` (faster-whisper, int8) | ~1.5 GB | ~2 GB |
| `qwen2.5:4b` (or `qwen3.5:4b`) | ~2.4 GB | ~3.5 GB |
| `llava:7b` | ~4.7 GB | ~5.5 GB |

The pipeline manages this through Ollama's API:

```python
unload_all_loaded_models(except_model=args.text_model)
time.sleep(2)
```

The 2-second sleep gives the kernel time to release memory before the next allocation. With `OLLAMA_KV_CACHE_TYPE=q8_0` and a generous `OLLAMA_MAX_VRAM`, OOM errors during model swap are rare in practice on this 64 GB configuration.

## Performance benchmarks (this exact machine)

Measured on the Minisforum AI X1-255 with 64 GB DDR5 dual-channel and the configuration above, ROCm 6.x, Ollama 0.21.2:

| Stage | Short (60–120 s video) | News digest (180–280 s video) |
|-------|------------------------|-------------------------------|
| yt-dlp download | 1–3 s | 1–3 s |
| Whisper-medium transcription | 30–50 s | 80–100 s |
| Transcript fixer (qwen 4B) | skipped if < 1500 chars | 120–180 s |
| Vision (llava 7B, ~10 frames) | 50–90 s | n/a |
| Cross-check (qwen 4B) | 15–25 s | 50–70 s |
| **Total wall time** | **~100 s** | **~270 s** |

A discrete NVIDIA RTX 4070 or AMD RX 7800 XT would roughly halve every GPU stage. A datacenter GPU (A100, H100) would obliterate these times. But: those cost 10× more, draw 5× the power, and don't fit on a shelf.

For a personal automation pipeline that runs nightly on YouTube updates, ~100 seconds per video is fine.

## What if you have NVIDIA?

No special configuration needed. Ollama auto-detects CUDA and uses it. A 12 GB+ card (RTX 3060 12 GB, 4070, 4070 Ti, etc.) is comfortable; 8 GB cards work but you may need to keep the explicit unload calls. The Python pipeline code is GPU-agnostic — it talks to Ollama via HTTP, and Ollama handles the rest.

## What if you have a different AMD GPU?

- **Discrete AMD card (RX 6000/7000 series)**: drop the `HSA_OVERRIDE_GFX_VERSION` line, the `HSA_ENABLE_SDMA` line, and the `AMD_SERIALIZE_KERNEL` line. Officially supported targets work without these workarounds.
- **Different RDNA 3 APU (880M, 890M, Steam Deck OLED's iGPU)**: same `11.0.0` override should work. The other stability flags are recommended but optional.
- **Older RDNA 2 APUs (680M, original Steam Deck)**: use `HSA_OVERRIDE_GFX_VERSION=10.3.0` instead. Performance will be lower; consider whether running 7B models locally is realistic.
- **Pre-RDNA AMD APUs (Vega-based)**: not realistic for this workload. CPU-only inference is faster than fighting unsupported ROCm targets.

## Why local at all

The whole pipeline could run via paid APIs (Whisper API, Claude API, GPT-4 Vision). It would be faster, more accurate, and zero-config. But:

- **No recurring cost.** A 30-video-per-month digest at API prices adds up quickly.
- **No rate limits.** Run as many times as you want, parallelise freely, retry without anxiety.
- **Privacy.** Whatever you analyse stays on your machine.
- **Offline-friendly.** Works on a laptop on a train.
- **No vendor lock-in.** Swap models, tweak prompts, fork freely.
- **Learning value.** Running these models locally teaches you what they actually do — what they're good at, what they hallucinate on, where they break. That intuition is worth more than the API bill it replaces.

The trade-off is also honest: local 4B and 7B models are not as capable as frontier APIs. The project compensates with engineering — sanity checks, validators, threshold-based routing, fallback paths. The [lessons learned page](/shortcutter/docs/lessons-learned) documents what specifically had to be designed to make small models reliable.

## Software stack

Everything runs on Ubuntu 24.04 LTS. Versions current at the time of writing:

```text
Ubuntu          24.04 LTS
Linux kernel    6.8+
ROCm            6.x
Ollama          0.21.2
yt-dlp          latest pip
ffmpeg          6.x (apt)
Python          3.12+
faster-whisper  1.0+
httpx           0.27+
```

A `requirements.txt` lives in the repo root for the Python dependencies. ffmpeg, Ollama, and ROCm are installed system-wide.

## A note on the "AI" branding

The X1-255 has no NPU. The "AI" in "Minisforum AI X1" refers to the product line, not to dedicated AI silicon — that is on the X1 Pro variants (Ryzen AI 9 HX 370 with XDNA 2 NPU and Radeon 890M).

For LLM inference workloads like this one, the NPU would not help anyway: current Ollama and llama.cpp pipelines target GPU compute, not the NPU's INT8 matrix engine. The 780M's CU array does the actual work.

If you're shopping for a similar machine and want headroom for future eGPU expansion or stronger built-in graphics, the **X1 Pro-370** (with Radeon 890M and OCuLink) is the natural next step up from the 255.

## Buying advice: verify the dual-channel configuration on arrival

A 1-star review on the Italian Amazon listing reports that one unit was shipped with a single 32 GB SODIMM (single-channel) instead of 2× 16 GB or 2× 32 GB (dual-channel) — a non-trivial issue, since memory bandwidth significantly affects iGPU inference performance.

**Check your unit immediately** with:

```bash
sudo dmidecode -t 17 | grep -E 'Size|Locator|Bank Locator|Configured'
```

A correct dual-channel result should look like this:

```
Size: 32 GB
Locator: DIMM 0
Bank Locator: P0 CHANNEL A
Configured Memory Speed: 5600 MT/s
...
Size: 32 GB
Locator: DIMM 0
Bank Locator: P0 CHANNEL B
Configured Memory Speed: 5600 MT/s
```

The key line is `Bank Locator`: you must see **CHANNEL A and CHANNEL B both populated** with same-size SODIMMs at the same speed. If you see only one entry, or two entries both on the same channel, you're running single-channel and should contact the seller.

For LLM workloads on an iGPU, single-channel can cost 30–40% in throughput on memory-bound stages. Worth verifying within the 30-day return window.

### Upgrading from stock 32 GB to 64 GB

The stock listing ships with 32 GB DDR5 SODIMM. To run multiple LLMs warm simultaneously (`OLLAMA_MAX_LOADED_MODELS=6` is comfortable on 64 GB, marginal on 32 GB), upgrade to a 2× 32 GB matched DDR5 SODIMM kit at 5600 MT/s. The X1-255 supports up to 64 GB officially.

After installing, verify with `dmidecode` as above and check `free -h` — you should see ~54 GiB visible to Linux (64 GB physical minus the BIOS UMA Frame Buffer reservation).
