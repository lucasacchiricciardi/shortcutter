---
title: Architecture
description: How the three scripts are organised, what each one does, and the unified JSON schema that ties them together.
order: 2
updated: 2026-04-30
---

Shortcutter is intentionally split into three Python scripts. Each one has a single responsibility and they communicate through a shared JSON schema.

## The three scripts

### `analyze_video.py` — the router

A small dispatcher (about 180 lines) that:

1. Downloads only the video metadata via `yt-dlp --skip-download --print-json`
2. Classifies the video using a 3-step rule
3. Hands off to the right pipeline

**Classification logic:**

1. **Title override** — if the title contains "notizie", "news", "ai news", "aggiornamento", "rassegna" → `news_digest`
2. **Heuristic** — duration > 90s AND ≥5 separators (` | `, `→ `, `🔵`, `🟣`, `⚡`, etc) → `news_digest`
3. **Default** — everything else → `tech_short`

### `analyze_news.py` — news digest pipeline

For multi-topic news roundups (typically 3-5 minutes long, multiple bullet points in the description). Optimised for extracting individual news items with their associated tools and categories.

Key features:

- Whisper-medium with `initial_prompt` biased for AI/tech terminology
- LLM transcript fixer with adaptive timeout (`150s + 1s per 40 chars`, capped at 360s)
- Python-side filter for ghost fixes (entries where `original == corrected`)
- Sanity check at 30% length difference
- VRAM reset between fixer and extraction (anti-degradation)
- News extraction with `qwen3.5:4b` cross-check

### `analyze_short.py` — thematic short pipeline

For single-resource shorts (typically 60-120 seconds) like tool announcements, repo presentations, or model launches. Optimised for identifying the main resource and validating its canonical URL.

Key features:

- Same Whisper setup as news
- Skip transcript fixer for transcripts under 1,500 characters (small models hallucinate on short inputs)
- Vision analysis with `llava:7b` on cropped frames
- GitHub URL validation via HEAD request (anti-hallucination)
- Confidence penalty when a hallucinated URL is removed

## Unified JSON schema

Both pipelines emit the same root structure. Some fields are pipeline-specific.

```json
{
  "video_url": "...",
  "video_id": "...",
  "video_title": "...",
  "video_description": "...",
  "video_duration_sec": 140,
  "pub_date": "2026-04-30",
  "analysis_type": "news_digest | tech_short",
  "analysis_pipeline": "analyze_news.py | analyze_short.py",
  "analysis_started_at": "2026-04-30T15:24:00Z",
  "analysis_duration_sec": 102.4,

  "audio": {
    "language": "it",
    "language_probability": 0.99,
    "transcript_raw": "...",
    "transcript_corrected": "...",
    "transcript": "...",
    "fixes_applied": [
      { "original": "Cloud Code", "corrected": "Claude Code", "reason": "..." }
    ],
    "fixer_used": true,
    "fixer_duration_sec": 28.5,
    "whisper_model": "medium",
    "duration_sec": 31.2
  },

  "extracted_links": {
    "urls": [],
    "github_repos": [],
    "huggingface_models": []
  },

  "news_items": [
    {
      "topic": "Anthropic bans agriculture company",
      "summary_it": "...",
      "tools_mentioned": ["Anthropic"],
      "category": "news"
    }
  ],

  "main_resource": {
    "name": "ccusage",
    "type": "GitHub repository",
    "canonical_url": "https://github.com/ryoppippi/ccusage",
    "summary_it": "...",
    "tech": ["Claude Code", "npx", "JSON", "MCP server"],
    "readme_sections": ["Installation", "Usage", "..."]
  },

  "vision_frames": [
    {
      "frame_index": 0,
      "timestamp_sec": 0,
      "main_title": "...",
      "keywords": [],
      "is_code": false,
      "is_github": true,
      "elapsed_sec": 7.1,
      "error": ""
    }
  ],

  "confidence": 0.95,
  "errors": []
}
```

The `news_items` array is populated only for `news_digest`. The `main_resource` and `vision_frames` arrays are populated only for `tech_short`. This makes downstream consumers (n8n workflows, databases) trivial to write — same key paths, optional fields.

## Resilience defenses

| Threat                              | Defense                                        |
|-------------------------------------|------------------------------------------------|
| LLM invents GitHub URLs             | HEAD request validation, delete if 404         |
| LLM rewrites transcript with description | 30% length-diff sanity check, fallback to raw |
| Fixer hallucinates on short transcripts | Skip fixer for transcripts < 1500 char     |
| Fixer outputs ghost fixes (`X → X`) | Python-side filter, silent drop                |
| Variable shadowing bug              | Caught by careful code review                  |
| Creator uploads wrong audio         | Pipeline reports incoherence, does not invent  |

## Markdown reports

Each run also produces a human-readable Markdown report. The report is what makes debugging tractable — at a glance you can see the four states of the transcript fixer:

- `(corrected — N fix applied)` — fixer ran, produced N corrections
- `(fixer attivato ma 0 fix — possibile timeout o sanity check)` — fixer ran but produced nothing usable
- `(fixer skip — transcript NNN char < 1500)` — fixer was skipped by design
- `(fixer disattivato)` — fixer was disabled with `--no-fix`

This transparency is intentional. When something looks off in the data, the report tells you which path the pipeline took.
