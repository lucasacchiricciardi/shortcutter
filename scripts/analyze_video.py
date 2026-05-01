#!/usr/bin/env python3
"""
analyze_video.py — Router intelligente per analisi video YouTube.

Scarica solo i metadata del video (no video, no audio), classifica il tipo
e lancia lo script giusto:

  - news_digest → analyze_news.py (~30-50 sec, no vision)
  - tech_short  → analyze_short.py (~3-4 min, con vision)

Logica di classificazione:
  1. Se titolo contiene "Notizie", "News", "AI News" → news_digest (override)
  2. Se durata > 90 sec AND description ha 5+ separatori strutturali → news_digest
  3. Altrimenti → tech_short

Separatori strutturali considerati: " | ", "→ ", "✅ ", "🔵 ", "🔴 ", "⚡ "

Uso:
  python3 analyze_video.py "URL"                 # auto-detect
  python3 analyze_video.py URL --force news      # forza news pipeline
  python3 analyze_video.py URL --force short     # forza short pipeline
  python3 analyze_video.py URL --dry-run         # solo classificazione, no esecuzione
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Path degli script di pipeline (default: stessa directory di questo script)
SCRIPT_DIR = Path(__file__).resolve().parent
NEWS_SCRIPT = SCRIPT_DIR / "analyze_news.py"
SHORT_SCRIPT = SCRIPT_DIR / "analyze_short.py"


# ============================================================
# METADATA FETCH (no download video)
# ============================================================

def fetch_metadata_only(url: str) -> dict:
    """Scarica SOLO metadata via yt-dlp --skip-download."""
    print(f"📥 Fetch metadata: {url}")

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print-json",
        "--no-warnings",
        "--no-playlist",
        url,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Errore yt-dlp: {e.stderr}", file=sys.stderr)
        sys.exit(1)

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"❌ Errore parsing metadata: {e}", file=sys.stderr)
        sys.exit(1)

    return info


# ============================================================
# CLASSIFICAZIONE
# ============================================================

NEWS_TITLE_KEYWORDS = ["notizie ", "news ", "ai news", "aggiornamento ", "rassegna "]


def classify_video(metadata: dict) -> tuple[str, str]:
    """
    Ritorna (analysis_type, motivazione).
    analysis_type ∈ {"news_digest", "tech_short"}
    """
    title = (metadata.get("title") or "").strip()
    title_lower = title.lower()
    description = metadata.get("description") or ""
    duration = int(metadata.get("duration") or 0)

    # Override 1 — Titolo esplicito
    for kw in NEWS_TITLE_KEYWORDS:
        if kw in title_lower:
            return "news_digest", f"titolo contiene '{kw.strip()}'"

    # Conta separatori strutturali nella description
    separators_count = (
        description.count(" | ") +
        description.count("→ ") +
        description.count("✅ ") +
        description.count("🔵 ") +
        description.count("🔴 ") +
        description.count("⚡ ") +
        description.count("🟢 ") +
        description.count("🟡 ") +
        description.count("🟣 ")
    )

    # Regola principale combinata
    if duration > 90 and separators_count >= 5:
        return "news_digest", f"durata {duration}s + {separators_count} separatori strutturali"

    # Default
    return "tech_short", f"durata {duration}s, {separators_count} separatori (sotto soglia)"


# ============================================================
# DISPATCH
# ============================================================

def dispatch(url: str, analysis_type: str, extra_args: list[str]) -> int:
    """Lancia lo script giusto e ritorna il return code."""
    if analysis_type == "news_digest":
        script = NEWS_SCRIPT
        emoji = "📰"
    elif analysis_type == "tech_short":
        script = SHORT_SCRIPT
        emoji = "🎯"
    else:
        print(f"❌ Tipo non riconosciuto: {analysis_type}", file=sys.stderr)
        return 1

    if not script.exists():
        print(f"❌ Script non trovato: {script}", file=sys.stderr)
        return 1

    print(f"\n{emoji} Esecuzione pipeline: {script.name}")
    print("=" * 60)

    cmd = ["python3", str(script), url] + extra_args
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except KeyboardInterrupt:
        print("\n⚠️  Interrotto dall'utente")
        return 130


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Router intelligente per analisi YouTube video",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  %(prog)s "https://www.youtube.com/shorts/P8DbsXCiPEE"
  %(prog)s URL --force news
  %(prog)s URL --dry-run
  %(prog)s URL --keep-files --output-md /tmp/output.md
""",
    )
    parser.add_argument("url", help="URL del video YouTube")
    parser.add_argument("--force", choices=["news", "short"],
                        help="Forza un tipo di analisi, salta classificazione")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo classificazione, non esegue la pipeline")
    parser.add_argument("--news-script", default=str(NEWS_SCRIPT),
                        help="Path custom per analyze_news.py")
    parser.add_argument("--short-script", default=str(SHORT_SCRIPT),
                        help="Path custom per analyze_short.py")


    # Tutti gli altri argomenti vengono passati alla pipeline
    args, extra_args = parser.parse_known_args()

    # Update script paths se override (modifica i moduli globali)
    import sys as _sys
    _module = _sys.modules[__name__]
    _module.NEWS_SCRIPT = Path(args.news_script)
    _module.SHORT_SCRIPT = Path(args.short_script)

    # Verifiche
    if not shutil.which("yt-dlp"):
        print("❌ yt-dlp non trovato in PATH", file=sys.stderr)
        sys.exit(1)

    # Force mode
    if args.force:
        analysis_type = "news_digest" if args.force == "news" else "tech_short"
        reason = f"forzato via --force {args.force}"
        print(f"🎯 Tipo forzato: {analysis_type}")
        print(f"   Motivo: {reason}")

        if args.dry_run:
            print(f"\n🔎 Dry run, nessuna esecuzione")
            return 0

        return dispatch(args.url, analysis_type, extra_args)

    # Auto-detect mode
    metadata = fetch_metadata_only(args.url)

    title = metadata.get("title", "")
    duration = metadata.get("duration", 0)
    description = metadata.get("description", "") or ""
    desc_preview = description[:120] + "..." if len(description) > 120 else description

    print(f"\n📊 METADATA")
    print(f"   📌 Titolo: {title}")
    print(f"   ⏱️  Durata: {duration}s")
    print(f"   📝 Description: {desc_preview}")

    analysis_type, reason = classify_video(metadata)

    print(f"\n🔎 CLASSIFICAZIONE")
    print(f"   Tipo: {analysis_type}")
    print(f"   Motivo: {reason}")

    if args.dry_run:
        print(f"\n🔎 Dry run, nessuna esecuzione")
        return 0

    return dispatch(args.url, analysis_type, extra_args)


if __name__ == "__main__":
    sys.exit(main())
