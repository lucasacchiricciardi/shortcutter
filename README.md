# Shortcutter

> Self-hosted AI pipeline that downloads YouTube videos, transcribes audio, analyzes frames with vision models, and produces structured digests — all running on local LLMs without external APIs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Ollama](https://img.shields.io/badge/Ollama-required-green.svg)](https://ollama.com/)

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/lucasacchi/)
[![Website](https://img.shields.io/badge/lucasacchi.net-Visit-181717?logo=googlechrome&logoColor=white)](https://lucasacchi.net)
[![YouTube](https://img.shields.io/badge/YouTube-%40LucaSacchiNet-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@LucaSacchiNet)

> 👋 **Like this project?** Connect with me on [LinkedIn](https://www.linkedin.com/in/lucasacchi/) — I share the technical stories behind tools like this one.

## What is this?

Shortcutter is a 3-script pipeline that turns YouTube videos into structured analyses. It was born from a concrete problem: I follow a YouTube channel that publishes daily AI news as 2-minute shorts, but the links to mentioned tools/repos aren't in the description, in the channel bio, or anywhere clickable — they're only spoken aloud.

So I built this to:
- Download videos with `yt-dlp`
- Transcribe audio with `Whisper-medium`
- Analyze frames with `llava:7b` (vision OCR for screenshots)
- Cross-check with `qwen3.5:4b` (text reasoning for resource identification)
- Validate GitHub URLs via HEAD requests (LLMs love hallucinating URLs)
- Output unified JSON + readable Markdown reports

**Self-hosted, local LLMs only, zero API costs.**

## Pipeline architecture

```
┌──────────────────────┐
│  analyze_video.py    │  Router: classifies the video
│  (~1-2s)             │  Inspects title + duration + description
└──────────┬───────────┘
           │
     ┌─────┴─────┐
     │           │
     ▼           ▼
┌─────────┐  ┌─────────┐
│  NEWS   │  │  SHORT  │
│ digest  │  │ tematic │
└────┬────┘  └────┬────┘
     │            │
     ▼            ▼
analyze_news.py   analyze_short.py
(~270s/video)     (~100s/video)

  • Whisper           • Whisper
  • Transcript fixer  • Skip fixer if <1500 char
  • Link extraction   • Vision (llava OCR)
  • News extraction   • GitHub URL validation
                      • Resource identification

         ↓                    ↓
  unified JSON schema + Markdown report
```

### Classification logic

The router classifies videos using a 3-step rule:

1. **Title override**: contains "notizie", "news", "ai news", etc → `news_digest`
2. **Heuristic**: duration > 90s AND ≥5 separators (` | `, `→ `, `🔵`, `🟣`, etc) → `news_digest`
3. **Default**: everything else → `tech_short`

## Quick start

### Prerequisites

- **Python 3.12+**
- **[Ollama](https://ollama.com/)** running locally
- **ffmpeg** + **yt-dlp** in PATH
- ~10 GB VRAM (or GTT/RAM with ROCm fallback)

### Install models

```bash
ollama pull qwen3.5:4b      # ~3.4 GB
ollama pull llava:7b        # ~5 GB (only needed for shorts)
```

### Install Python dependencies

```bash
git clone https://github.com/<your-username>/shortcutter
cd shortcutter
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
# Auto-classifies the video and dispatches to the right pipeline
python scripts/analyze_video.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Or run pipelines directly
python scripts/analyze_news.py "URL" --output-md /tmp/news.md --output-json /tmp/news.json
python scripts/analyze_short.py "URL" --output-md /tmp/short.md --output-json /tmp/short.json
```

## Output schema

Both pipelines produce JSON with the same root structure:

```json
{
  "video_url": "...",
  "video_id": "...",
  "video_title": "...",
  "video_description": "...",
  "video_duration_sec": 140,
  "analysis_type": "news_digest | tech_short",
  "audio": {
    "language": "it",
    "language_probability": 0.99,
    "transcript_raw": "...",
    "transcript_corrected": "...",
    "fixes_applied": [...],
    "fixer_used": true
  },
  "extracted_links": {
    "urls": [],
    "github_repos": [],
    "huggingface_models": []
  },
  "news_items": [...],          // populated for news_digest
  "main_resource": {...},        // populated for tech_short
  "vision_frames": [...],        // populated for tech_short
  "confidence": 0.95
}
```

Both pipelines emit the same root structure. The `news_items` array is populated for news digests, while `main_resource` and `vision_frames` are populated for tech shorts.

## Resilience by design

LLMs hallucinate. This pipeline assumes they will and protects against it:

| Threat                              | Defense                                        |
|-------------------------------------|------------------------------------------------|
| LLM invents GitHub URLs             | HEAD request validation, delete if 404         |
| LLM rewrites transcript with description | 30% length-diff sanity check, fallback to raw |
| Fixer hallucinates on short transcripts | Skip fixer for transcripts < 1500 char     |
| Fixer outputs ghost fixes (`X → X`) | Python-side filter, silent drop                |
| Variable shadowing bug              | Found by careful code review (lesson learned!) |
| Creator uploads wrong audio         | Pipeline reports incoherence, doesn't invent   |

## Hardware tested

- **CPU**: AMD Ryzen 7 (Zen 4)
- **GPU**: AMD Radeon 780M iGPU (8 GB VRAM dedicated, GTT 49 GB)
- **OS**: Ubuntu 24.04
- **ROCm**: with `HSA_OVERRIDE_GFX_VERSION=11.0.0`

The code is hardware-agnostic but ROCm-specific tweaks are noted in the scripts. NVIDIA users should be able to run as-is (Ollama handles GPU detection).

## n8n integration (planned)

The pipelines are designed to be invoked by an n8n workflow that:
- Reads YouTube channel uploads via Data API v3
- Calls the analyze pipelines on new uploads
- Sends a digest email at a scheduled time

A sample workflow JSON will be added in a future release.

## Lessons learned

I documented [5 hard-won lessons](docs/LESSONS_LEARNED.md) from building this. Highlights:

1. Small LLMs (4B) hallucinate predictably. Validate, don't trust.
2. The same fixer that's gold on long transcripts is poison on short ones.
3. A 5-line sanity check saved the day more than once.
4. Sometimes the bug is upstream (creator-side), not in your code.
5. **Sacchi's 3 rules**: 🛡️ safety first · 🔁 little often · 👁️ double check.

## Why "Shortcutter"?

Originally built to extract data from YouTube *shorts*. Took a *shortcut* through the video to grab the structured info I needed without watching all 30 daily uploads.

## Acknowledgments

- **[ZioBudda Labs](https://www.youtube.com/@ziobuddalabs)** for the daily AI news content that motivated this project
- **[Ollama](https://ollama.com/)** for making local LLMs accessible
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** for fast CPU transcription
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** for the YouTube heavy lifting

## License

MIT — see [LICENSE](LICENSE).

## Author

**Luca Sacchi** — Linux/Unix sysadmin and system architect with 25+ years of experience. Based in Milan, Italy. Active educator across multiple Italian higher-education and vocational training programs (Cisco CCNA, ITS, IFTS).

### Let's connect

If this project resonates with you — whether you're into local AI, network engineering, automation, or just want to follow along as I build more — **let's connect on LinkedIn**:

👉 **[linkedin.com/in/lucasacchi](https://www.linkedin.com/in/lucasacchi/)**

You'll find my full background, certifications, teaching history, and frequent posts on the technical adventures behind tools like this one.

Other places to find me:

- 🌐 [lucasacchi.net](https://lucasacchi.net)
- 📺 [YouTube — @LucaSacchiNet](https://www.youtube.com/@LucaSacchiNet)
- 🏆 [Credly profile](https://www.credly.com/users/luca-sacchi-ricciardi)

If this project is useful to you, a star on the repo is the simplest way to say thanks. PRs and issues are welcome.

---

*Built following the 3 rules of Sacchi: safety first, little often, double check.*
