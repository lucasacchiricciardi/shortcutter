# Changelog

## 1.0.0 - 2026-04-30

### Added
- Initial public release.
- `analyze_video.py` router with title/duration/separator-based classification.
- `analyze_news.py` pipeline for news digest videos.
- `analyze_short.py` pipeline for thematic short videos.
- LLM transcript fixer with adaptive timeout (`150 + len/40`, capped at 360s).
- Python-side filter for ghost fixes (entries where `original == corrected`).
- Sanity check: discard fixer output if length differs > 30% from raw.
- VRAM reset between fixer and extraction stages (anti-degradation).
- GitHub URL validation via HEAD request (anti-hallucination).
- Confidence penalty (max 0.7) when a hallucinated URL is removed.
- Skip transcript fixer for short transcripts (< 1500 char) — model collapses.
- Unified JSON schema across pipelines.
- Markdown reports with 4-state fixer transparency:
  `corrected | fixer-attivato-0-fix | fixer-skip-brevità | fixer-disattivato`.
- Sample n8n workflow for daily digest automation (anonymized).
- Documentation: ARCHITECTURE, HARDWARE, PROMPTS, LESSONS_LEARNED.

### Fixed
- Variable shadowing bug in fixer's loop that reduced full transcripts to a
  single word. Renamed inner-loop variables (`fix_original`, `fix_corrected`)
  to avoid collision with outer `corrected`.

### Tested
- 4 news digests (8-9 news extracted, 100% accuracy vs description).
- 3 thematic shorts (correct GitHub URLs, README sections, classifications).
- 1 patological case (creator uploaded video with wrong audio): pipeline
  reported incoherence honestly without hallucinating a summary.
