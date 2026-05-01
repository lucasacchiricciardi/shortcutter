#!/usr/bin/env python3
"""
analyze_news.py — Pipeline ottimizzata per digest news AI/IT.

Per video tipo "Notizie 29 Aprile 2026" che elencano 8-15 news del giorno.
NO vision (description + audio bastano).

Pipeline:
  1. yt-dlp scarica video + metadata
  2. ffmpeg estrae audio mp3 mono 16kHz
  3. Whisper-medium trascrive (autodetect lingua, initial_prompt AI)
  4. LLM transcript fixer corregge errori Whisper usando description
  5. Reset VRAM (unload completo qwen) per evitare degradazione
  6. Estrae link/repo da description + transcript corretto
  7. qwen3.5:4b (ricaricato pulito) cross-check estrae TUTTE le news
  8. Output JSON unificato + Markdown

Stack:
  - whisper-medium (CPU, ~1.5 GB)
  - qwen3.5:4b (~3.4 GB) per fixer + news extraction (riavviato tra i due)

Tempo medio: ~180-210 secondi per digest da 2-3 minuti.

Changelog v3 (vs v2):
  - Fix A: unload completo qwen tra fixer e extraction (no degradazione)
  - Fix C: prompt fixer rinforzato (no correzioni inventate tipo LEWWM→LEMW)
  - Fix C: prompt cross-check rinforzato (categorie SPECIFICHE, no fallback 'news')
  - Fix C: tools_mentioned tutti inclusi (es. GrillMe + Claude)

Uso:
  python3 analyze_news.py "URL"
  python3 analyze_news.py URL --no-fix --keep-files
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

DEFAULT_TEXT_MODEL = "qwen3.5:4b"
DEFAULT_WHISPER_MODEL = "medium"
DEFAULT_TIMEOUT = 120


# ============================================================
# DATA CLASSES (schema unificato)
# ============================================================

@dataclass
class TranscriptFix:
    original: str = ""
    corrected: str = ""
    reason: str = ""


@dataclass
class NewsItem:
    topic: str = ""
    summary_it: str = ""
    tools_mentioned: list[str] = field(default_factory=list)
    category: str = ""  # tool | model | research | news | course | event | announcement


@dataclass
class AudioData:
    language: str = ""
    language_probability: float = 0.0
    transcript_raw: str = ""
    transcript_corrected: str = ""
    transcript: str = ""  # alias del corrected per backward compat
    fixes_applied: list[TranscriptFix] = field(default_factory=list)
    fixer_used: bool = False
    fixer_duration_sec: float = 0.0
    whisper_model: str = ""
    duration_sec: float = 0.0


@dataclass
class ExtractedLinks:
    urls: list[str] = field(default_factory=list)
    github_repos: list[str] = field(default_factory=list)
    huggingface_models: list[str] = field(default_factory=list)


@dataclass
class VideoAnalysisResult:
    video_url: str = ""
    video_id: str = ""
    video_title: str = ""
    video_description: str = ""
    video_duration_sec: int = 0
    pub_date: str = ""

    analysis_type: str = "news_digest"
    analysis_pipeline: str = "analyze_news.py"
    analysis_started_at: str = ""
    analysis_duration_sec: float = 0.0

    audio: AudioData = field(default_factory=AudioData)
    extracted_links: ExtractedLinks = field(default_factory=ExtractedLinks)

    # Specifico news_digest
    news_items: list[NewsItem] = field(default_factory=list)

    # Sempre presente, vuoto per news_digest
    main_resource: dict = field(default_factory=dict)
    vision_frames: list = field(default_factory=list)

    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)


# ============================================================
# OLLAMA UTILS
# ============================================================

def unload_all_loaded_models(except_model: str | None = None) -> None:
    """Scarica tutti i modelli da VRAM. Se except_model=None, scarica TUTTO."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        if r.status_code != 200:
            return
        loaded = r.json().get("models", [])
        for m in loaded:
            name = m.get("name") or m.get("model", "")
            if not name:
                continue
            if except_model and (name == except_model or name.split(":")[0] == except_model.split(":")[0]):
                continue
            try:
                httpx.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": name, "keep_alive": 0},
                    timeout=10
                )
            except Exception:
                pass
    except Exception:
        pass


# ============================================================
# STEP 1: DOWNLOAD VIDEO + METADATA
# ============================================================

def download_video(url: str, workdir: Path) -> tuple[Path, dict]:
    """Scarica video + metadata. Ritorna (video_path, info_dict)."""
    print(f"📥 Scarico video da: {url}")
    output_template = str(workdir / "video.%(ext)s")
    cmd = [
        "yt-dlp", "-f", "best[height<=720]/best",
        "--no-playlist", "--no-warnings", "--quiet",
        "--write-info-json",
        "--output", output_template, url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Errore yt-dlp: {e.stderr}", file=sys.stderr)
        sys.exit(1)

    video_path = None
    info_path = workdir / "video.info.json"
    for f in workdir.iterdir():
        if f.stem == "video" and f.suffix in (".mp4", ".webm", ".mkv"):
            video_path = f
            break
    if not video_path:
        print("❌ Video non trovato", file=sys.stderr)
        sys.exit(1)

    info = {}
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text())
        except Exception as e:
            print(f"⚠️  Errore parsing info.json: {e}")

    print(f"✅ Video: {video_path.name} ({video_path.stat().st_size / 1e6:.1f} MB)")
    if info.get("title"):
        print(f"   📌 Titolo: {info['title']}")
    if info.get("duration"):
        print(f"   ⏱️  Durata: {info['duration']}s")

    return video_path, info


def extract_audio(video_path: Path, output_dir: Path) -> Path:
    audio_path = output_dir / "audio.mp3"
    print("🎙️  Estraggo audio...")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-q:a", "4",
        str(audio_path),
        "-loglevel", "error",
    ]
    subprocess.run(cmd, check=True)
    print(f"✅ Audio: {audio_path.name} ({audio_path.stat().st_size / 1e3:.0f} KB)")
    return audio_path


# ============================================================
# STEP 2: WHISPER TRANSCRIPTION
# ============================================================

WHISPER_INITIAL_PROMPT = (
    "Notizie tecniche di ZioBudda Labs sull'intelligenza artificiale. "
    "Termini frequenti: Claude, Claude Code, Anthropic, MCP, Karpathy, "
    "OpenAI, Google, Kaggle, NVIDIA, Nemotron, Qwen, Gemini, Python, "
    "GitHub, JEPA, Echo-2, MiniCPM, Whisper, WUPHF, Obscura, "
    "vibe coding, agenti AI, modelli linguistici, LLM, prompt engineering."
)


def transcribe_audio(audio_path: Path, model_size: str) -> AudioData:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("❌ faster-whisper non installato. pip install faster-whisper")
        sys.exit(1)

    print(f"\n🎙️  Trascrizione con whisper-{model_size} (autodetect)...")

    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=WHISPER_INITIAL_PROMPT,
    )
    transcript = " ".join(seg.text.strip() for seg in segments).strip()
    elapsed = time.time() - t0

    print(f"✅ Trascrizione in {elapsed:.1f}s ({len(transcript)} char)")
    print(f"   Lingua: {info.language} (prob: {info.language_probability:.2f})")
    if transcript:
        preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
        print(f"   Testo: {preview}")

    return AudioData(
        language=info.language,
        language_probability=info.language_probability,
        transcript_raw=transcript,
        transcript_corrected=transcript,  # default, sovrascritto dal fixer
        transcript=transcript,             # alias backward compat
        whisper_model=model_size,
        duration_sec=round(elapsed, 1),
    )


# ============================================================
# STEP 2.5: LLM TRANSCRIPT FIXER (rinforzato v3)
# ============================================================

TRANSCRIPT_FIXER_SYSTEM = """Sei un correttore di trascrizioni audio italiane per ZioBudda Labs.

Ricevi:
1. TRASCRIZIONE WHISPER del video (può contenere errori acustici)
2. DESCRIPTION YouTube ufficiale (FONTE DI VERITA' per nomi propri e termini)

Il tuo compito: correggere SOLO errori di trascrizione evidenti, usando la description come riferimento.

REGOLE INDEROGABILI:
- Correggi parole acusticamente simili ma semanticamente sbagliate
  Esempi tipici di errori Whisper su italiano parlato:
  * "misteri manuali" → "mestieri manuali" (contesto: lavoro fisico)
  * "Andrei Carpati" → "Karpathy" (nome proprio)
  * "Cloud Code" → "Claude Code"
  * "Qn3.6" / "Quen 3.6" → "Qwen 3"
  * "perditi di contesto" → "perdita di contesto"
  * "matematica dunne e cruda" → "matematica nuda e cruda"
  * "amici e amici" → "amici"
- Correggi nomi propri sbagliati confrontando con la description
- Correggi acronimi/tool sbagliati confrontando con la description
- NON aggiungere contenuto non presente nel transcript originale
- NON parafrasare frasi che sono già corrette
- NON cambiare lo stile o l'ordine delle parole
- NON tradurre da una lingua all'altra
- Se NON sei sicuro di una correzione, lascia il testo originale ESATTAMENTE COSI'
- IMPORTANTE: NON inventare correzioni "migliori" se non c'è evidenza nella description.
  Esempio: se transcript dice "LEWWM" e description NON cita questo termine,
  LASCIA "LEWWM" invariato. NON cambiarlo in "LEMW", "LEW-M" o varianti.
  Una correzione errata è peggio di nessuna correzione.
- Mantieni l'italiano colloquiale dell'originale

═══════════════════════════════════════════════
REGOLE PER fixes_applied (CRITICHE)
═══════════════════════════════════════════════
- fixes_applied DEVE contenere SOLO entries con original DIVERSO da corrected
- NON includere MAI entries del tipo:
  {"original": "X", "corrected": "X", "reason": "Nessuna correzione necessaria"}
- NON enumerare i termini lasciati invariati
- Se non hai corretto nulla, fixes_applied = [] (array vuoto)
- Se hai corretto 5 cose, fixes_applied avrà esattamente 5 elementi
- Non importa quanti termini hai "controllato": importa solo quanti hai CAMBIATO

ESEMPIO CORRETTO (3 cose corrette su 100 parole):
{
  "transcript_corrected": "...",
  "fixes_applied": [
    {"original": "Cloud", "corrected": "Claude", "reason": "..."},
    {"original": "Carpati", "corrected": "Karpathy", "reason": "..."},
    {"original": "misteri", "corrected": "mestieri", "reason": "..."}
  ]
}

ESEMPIO SBAGLIATO (mai fare così):
{
  "fixes_applied": [
    {"original": "Cloud", "corrected": "Claude", "reason": "ok"},
    {"original": "tenant", "corrected": "tenant", "reason": "Nessuna correzione necessaria"}  ← VIETATO
  ]
}

Rispondi SOLO con JSON valido:
{
  "transcript_corrected": "il transcript completo con correzioni applicate",
  "fixes_applied": [
    {"original": "parola sbagliata", "corrected": "parola giusta", "reason": "motivo breve"}
  ]
}"""


def fix_transcript_with_llm(
    transcript_raw: str,
    video_description: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, list[TranscriptFix], float]:
    """
    Corregge la trascrizione Whisper usando description come riferimento.
    Ritorna (transcript_corrected, fixes_list, elapsed_sec).
    Se fallisce, ritorna il transcript originale invariato.
    """
    if not transcript_raw or not transcript_raw.strip():
        return transcript_raw, [], 0.0

    print(f"\n🔧 Transcript fixer con: {model}")

    desc_truncated = video_description[:2000] if video_description else ""

    user_prompt = f"""=== DESCRIPTION YOUTUBE (riferimento per nomi/termini) ===
{desc_truncated or '(non disponibile)'}

=== TRASCRIZIONE WHISPER (da correggere) ===
{transcript_raw}

Correggi SOLO errori evidenti. Produci il JSON richiesto."""

    payload = {
        "model": model,
        "prompt": user_prompt,
        "system": TRANSCRIPT_FIXER_SYSTEM,
        "stream": False,
        "think": False,
        "keep_alive": "2m",
        "options": {
            "temperature": 0.1,
            "num_predict": 4096,
            "top_p": 0.9,
        },
    }

    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        elapsed = time.time() - t0
        print(f"❌ Errore fixer: {e}, uso transcript originale")
        return transcript_raw, [], round(elapsed, 1)

    response_text = data.get("response", "").strip()
    elapsed = time.time() - t0

    parsed = extract_json_robust(response_text)
    if parsed is None:
        print(f"⚠️  Fixer non ha prodotto JSON valido. Uso transcript originale.")
        return transcript_raw, [], round(elapsed, 1)

    corrected = str(parsed.get("transcript_corrected", "")).strip()
    if not corrected:
        print(f"⚠️  Fixer ha restituito transcript vuoto. Uso transcript originale.")
        return transcript_raw, [], round(elapsed, 1)

    # Sanity check: il transcript corretto non deve essere drasticamente diverso (allucinazione)
    ratio_change = abs(len(corrected) - len(transcript_raw)) / max(len(transcript_raw), 1)
    if ratio_change > 0.30:
        print(f"⚠️  Transcript corretto differisce {ratio_change*100:.0f}% in lunghezza. Sospetto, uso originale.")
        return transcript_raw, [], round(elapsed, 1)

    # Rinomino la variabile per chiarezza (era 'corrected', collideva con il loop sotto)
    transcript_corrected = corrected

    fixes_raw = parsed.get("fixes_applied", [])
    fixes: list[TranscriptFix] = []
    ghost_fixes_count = 0
    if isinstance(fixes_raw, list):
        for fix in fixes_raw:
            if not isinstance(fix, dict):
                continue
            fix_original = str(fix.get("original", "")).strip()
            fix_corrected = str(fix.get("corrected", "")).strip()
            # FIX C: scarta non-fix (entries dove original == corrected)
            # Il LLM da 4B a volte enumera anche i termini lasciati invariati
            # nonostante il prompt vieti questo comportamento.
            if fix_original == fix_corrected:
                ghost_fixes_count += 1
                continue
            # Scarta entries vuote
            if not fix_original or not fix_corrected:
                continue
            fixes.append(TranscriptFix(
                original=fix_original,
                corrected=fix_corrected,
                reason=str(fix.get("reason", "")).strip(),
            ))

    if ghost_fixes_count > 0:
        print(f"   🧹 Filtrati {ghost_fixes_count} non-fix (original == corrected)")

    print(f"✅ Fixer in {elapsed:.1f}s — {len(fixes)} correzioni reali")
    for fix in fixes[:5]:
        print(f"   '{fix.original}' → '{fix.corrected}' ({fix.reason})")
    if len(fixes) > 5:
        print(f"   ... + altre {len(fixes) - 5}")

    return transcript_corrected, fixes, round(elapsed, 1)


# ============================================================
# STEP 3: LINK EXTRACTION
# ============================================================

def extract_links_and_repos(text: str) -> ExtractedLinks:
    if not text:
        return ExtractedLinks()

    url_pattern = r'https?://[^\s<>"\'`)\]]+'
    urls = [u.rstrip('.,;:!?') for u in re.findall(url_pattern, text)]

    github_pattern = r'(?:github\.com|GitHub\.com)/([\w\-]+)/([\w\-./]+)'
    github_repos = []
    for user, repo in re.findall(github_pattern, text, re.IGNORECASE):
        clean = repo.rstrip('.,;:!?/')
        if clean:
            github_repos.append(f"{user}/{clean}")

    hf_pattern = r'huggingface\.co/([\w\-]+)/([\w\-./]+)'
    hf_models = []
    for user, model in re.findall(hf_pattern, text, re.IGNORECASE):
        clean = model.rstrip('.,;:!?/')
        if clean:
            hf_models.append(f"{user}/{clean}")

    def dedup(items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return ExtractedLinks(
        urls=dedup(urls),
        github_repos=dedup(github_repos),
        huggingface_models=dedup(hf_models),
    )


# ============================================================
# STEP 4: NEWS EXTRACTION (rinforzato v3)
# ============================================================

NEWS_CROSSCHECK_SYSTEM = """Sei un analizzatore di digest news AI/IT in italiano per ZioBudda Labs.

Ricevi:
1. TITOLO del video YouTube
2. DESCRIPTION YouTube (solitamente lista strutturata di news)
3. LINK ESTRATTI (URL/repo dalla description)
4. TRASCRIZIONE AUDIO (già corretta da uno step precedente)

Il tuo compito: estrarre TUTTE le news menzionate, una per una.

REGOLE INDEROGABILI:
- Una news = un tool/modello/evento/notizia distinto
- Includi anche le news menzionate solo all'inizio della description, prima dei separatori |
- NON sintetizzare: se la description elenca 12 news + 1 in apertura, devi produrne 13
- Per ogni news, scrivi un summary di 1-2 frasi in italiano
- Estrai i tool/modelli/aziende specifici menzionati
- Categoria: una tra "tool | model | research | news | course | event | announcement"
  IMPORTANTE: scegli la categoria SPECIFICA, NON ricadere su 'news' generico.
  - Se è un tool/strumento concreto → "tool"
  - Se è un modello AI rilasciato → "model"
  - Se è ricerca/paper/tecnica → "research"
  - Se è un corso/formazione → "course"
  - 'news' è SOLO per notizie macro di settore senza tool specifico
    (es. licenziamenti, trend, dichiarazioni CEO, dati di mercato)
- Usa SOLO informazioni presenti nei dati forniti
- NON inventare news non presenti
- Per tools_mentioned, includi TUTTI i tool/aziende/persone citati nella news
  (es. se la news parla di "GrillMe interroga Claude", includi sia 'GrillMe' che 'Claude')

CATEGORIE (definizioni precise):
- tool: strumento concreto da usare (es. Claude Code, Whisper, Obscura, GrillMe)
- model: rilascio modello AI (es. Nemotron, MiniCPM, Echo-2, Qwen 3)
- research: paper/ricerca pura (es. modello 15M parametri capisce fisica, Karpathy training engine, ottimizzazione token)
- news: notizia generica del settore (es. licenziamenti, trend macro, dichiarazioni)
- course: formazione/educazione (es. corso Google su vibe coding)
- event: evento con data (es. hackathon, conferenza)
- announcement: annuncio strategico aziendale (es. partnership, rilascio batch modelli)

Rispondi SOLO con JSON valido, schema:
{
  "news_items": [
    {
      "topic": "titolo breve della news",
      "summary_it": "1-2 frasi in italiano",
      "tools_mentioned": ["lista TUTTI tool/aziende/persone citate"],
      "category": "tool|model|research|news|course|event|announcement"
    }
  ],
  "audio_language": "it|en",
  "confidence": 0.0-1.0
}"""


def extract_json_robust(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text.strip())
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i+1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                    continue
    return None


def extract_news_items(
    transcript: str,
    detected_lang: str,
    video_title: str,
    video_description: str,
    extracted_links: ExtractedLinks,
    model: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[NewsItem], float]:
    """Estrae lista news dal video. Ritorna (lista, confidence)."""
    print(f"\n🤝 Estrazione news con: {model}")

    desc_truncated = video_description[:4000] if video_description else ""

    user_prompt = f"""=== TITOLO VIDEO YOUTUBE ===
{video_title or '(non disponibile)'}

=== DESCRIPTION YOUTUBE ===
{desc_truncated or '(non disponibile)'}

=== LINK ESTRATTI ===
URL espliciti: {extracted_links.urls}
Repository GitHub: {extracted_links.github_repos}
HuggingFace models: {extracted_links.huggingface_models}

=== TRASCRIZIONE AUDIO (lingua: {detected_lang}) ===
{transcript or '(audio non disponibile)'}

Estrai TUTTE le news menzionate. Produci il JSON richiesto."""

    payload = {
        "model": model,
        "prompt": user_prompt,
        "system": NEWS_CROSSCHECK_SYSTEM,
        "stream": False,
        "think": False,
        "keep_alive": "2m",
        "options": {
            "temperature": 0.2,
            "num_predict": 2048,
            "top_p": 0.9,
        },
    }

    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"❌ Errore extraction: {e}")
        return [], 0.0

    response_text = data.get("response", "").strip()
    elapsed = time.time() - t0
    print(f"✅ Extraction in {elapsed:.1f}s")

    parsed = extract_json_robust(response_text)
    if parsed is None:
        print(f"⚠️  LLM non ha prodotto JSON. Raw: {response_text[:200]}")
        return [], 0.0

    raw_items = parsed.get("news_items", [])
    items = []
    for ri in raw_items:
        if not isinstance(ri, dict):
            continue
        items.append(NewsItem(
            topic=str(ri.get("topic", "")).strip(),
            summary_it=str(ri.get("summary_it", "")).strip(),
            tools_mentioned=ri.get("tools_mentioned", []) if isinstance(ri.get("tools_mentioned"), list) else [],
            category=str(ri.get("category", "news")).strip(),
        ))

    confidence = float(parsed.get("confidence", 0.0))
    print(f"   📰 News estratte: {len(items)}")
    return items, confidence


# ============================================================
# STEP 5: REPORT MARKDOWN
# ============================================================

CATEGORY_EMOJI = {
    "tool": "🛠️",
    "model": "🧠",
    "research": "🔬",
    "news": "📰",
    "course": "🎓",
    "event": "📅",
    "announcement": "📢",
}


def build_markdown_report(result: VideoAnalysisResult) -> str:
    md = f"# 📰 News Digest: {result.video_title}\n\n"
    md += f"**URL:** {result.video_url}\n"
    md += f"**Pubblicato:** {result.pub_date}\n"
    md += f"**Durata:** {result.video_duration_sec}s | "
    md += f"**Lingua:** {result.audio.language} | "
    md += f"**Confidence:** {result.confidence:.2f}\n"
    md += f"**News estratte:** {len(result.news_items)}\n\n"
    md += "---\n\n"

    if result.news_items:
        md += "## 📋 News del giorno\n\n"
        for i, item in enumerate(result.news_items, 1):
            emoji = CATEGORY_EMOJI.get(item.category, "📌")
            md += f"### {emoji} {i}. {item.topic}\n"
            if item.summary_it:
                md += f"{item.summary_it}\n\n"
            if item.tools_mentioned:
                md += f"_**Tool/Aziende:** {', '.join(item.tools_mentioned)}_\n\n"
            md += f"_**Categoria:** {item.category}_\n\n"
    else:
        md += "_⚠️ Nessuna news estratta._\n\n"

    has_links = (
        result.extracted_links.urls or
        result.extracted_links.github_repos or
        result.extracted_links.huggingface_models
    )
    if has_links:
        md += "## 🔗 Link estratti\n\n"
        for u in result.extracted_links.urls:
            md += f"- {u}\n"
        for r in result.extracted_links.github_repos:
            md += f"- https://github.com/{r}\n"
        for m in result.extracted_links.huggingface_models:
            md += f"- https://huggingface.co/{m}\n"
        md += "\n"

    if result.audio.transcript:
        # FIX B: distingui i 3 stati del fixer per debug visibile nel report
        if result.audio.fixer_used and result.audio.fixes_applied:
            md += f"## 🎙️ Trascrizione audio (corretta — {len(result.audio.fixes_applied)} fix applicati)\n\n"
        elif result.audio.fixer_used:
            md += "## 🎙️ Trascrizione audio (⚠️ fixer attivato ma 0 fix — possibile timeout o transcript già pulito)\n\n"
        else:
            md += "## 🎙️ Trascrizione audio (fixer disattivato)\n\n"
        md += f"> {result.audio.transcript}\n\n"

        if result.audio.fixer_used and result.audio.fixes_applied:
            md += "### 🔧 Correzioni applicate\n\n"
            for fix in result.audio.fixes_applied:
                md += f"- **`{fix.original}`** → **`{fix.corrected}`** _({fix.reason})_\n"
            md += "\n"
            md += "<details><summary>Trascrizione originale (raw)</summary>\n\n"
            md += f"> {result.audio.transcript_raw}\n\n"
            md += "</details>\n\n"

    if result.video_description:
        md += "## 📝 Description YouTube\n\n"
        desc_show = result.video_description[:1500]
        if len(result.video_description) > 1500:
            desc_show += "..."
        md += f"```\n{desc_show}\n```\n\n"

    md += f"---\n_Analizzato in {result.analysis_duration_sec:.1f}s da {result.analysis_pipeline}_\n"
    return md


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Analizza digest news AI/IT (no vision)")
    parser.add_argument("url", help="URL del video YouTube")
    parser.add_argument("--text-model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL,
                        choices=["tiny", "base", "small", "medium", "large-v3"])
    parser.add_argument("--no-audio", action="store_true",
                        help="Salta trascrizione audio")
    parser.add_argument("--no-fix", action="store_true",
                        help="Salta lo step LLM transcript fixer")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    args = parser.parse_args()

    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"❌ {tool} non in PATH", file=sys.stderr)
            sys.exit(1)

    try:
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5).raise_for_status()
    except Exception as e:
        print(f"❌ Ollama non raggiungibile: {e}", file=sys.stderr)
        sys.exit(1)

    workdir = Path(tempfile.mkdtemp(prefix="news_analysis_"))
    print(f"📂 Working directory: {workdir}\n")

    pipeline_start = time.time()
    result = VideoAnalysisResult(
        video_url=args.url,
        analysis_started_at=datetime.utcnow().isoformat() + "Z",
    )

    try:
        # 1. Download video + metadata
        video, info = download_video(args.url, workdir)
        result.video_id = info.get("id", "")
        result.video_title = info.get("title", "")
        result.video_description = info.get("description", "")
        result.video_duration_sec = int(info.get("duration", 0) or 0)
        result.pub_date = info.get("upload_date", "")
        if len(result.pub_date) == 8 and result.pub_date.isdigit():
            try:
                d = datetime.strptime(result.pub_date, "%Y%m%d")
                result.pub_date = d.isoformat() + "Z"
            except ValueError:
                pass

        # 2. Audio + Whisper (transcript_raw)
        if not args.no_audio:
            audio = extract_audio(video, workdir)
            result.audio = transcribe_audio(audio, args.whisper_model)

        # 2.5. Transcript fixer LLM (corregge errori Whisper usando description)
        if not args.no_audio and not args.no_fix and result.audio.transcript_raw:
            print("\n🧹 Pulizia VRAM prima del transcript fixer...")
            unload_all_loaded_models(except_model=args.text_model)
            time.sleep(2)

            # FIX A: timeout adattivo basato su lunghezza transcript
            # Base 120s + 1s ogni 40 char, capped a 360s
            # (alzato in v6 dopo timeout su prompt fixer più severo)
            transcript_len = len(result.audio.transcript_raw)
            adaptive_timeout = min(120 + (transcript_len // 40), 360)
            print(f"   ⏱️  Timeout fixer adattivo: {adaptive_timeout}s (transcript {transcript_len} char)")

            corrected, fixes, fix_elapsed = fix_transcript_with_llm(
                result.audio.transcript_raw,
                result.video_description,
                args.text_model,
                timeout=adaptive_timeout,
            )
            result.audio.transcript_corrected = corrected
            result.audio.transcript = corrected  # alias usato downstream
            result.audio.fixes_applied = fixes
            result.audio.fixer_used = True
            result.audio.fixer_duration_sec = fix_elapsed

        # 3. Link extraction (su transcript corretto)
        combined_text = (result.video_description or "") + " " + (result.audio.transcript or "")
        result.extracted_links = extract_links_and_repos(combined_text)
        total_links = (
            len(result.extracted_links.urls) +
            len(result.extracted_links.github_repos) +
            len(result.extracted_links.huggingface_models)
        )
        if total_links > 0:
            print(f"\n🔗 Link estratti: {total_links}")

        # 4. News extraction (LLM)
        # FIX A: forza unload+reload del modello qwen per resettare lo stato dopo il fixer
        # (evita degradazione delle categorie e tools_mentioned osservata in v2)
        print("\n🧹 Reset del modello qwen prima dell'extraction (fix degradazione)...")
        unload_all_loaded_models(except_model=None)  # scarica TUTTI inclusi qwen
        time.sleep(3)

        news_items, confidence = extract_news_items(
            result.audio.transcript,
            result.audio.language,
            result.video_title,
            result.video_description,
            result.extracted_links,
            args.text_model,
        )
        result.news_items = news_items
        result.confidence = confidence

        result.analysis_duration_sec = round(time.time() - pipeline_start, 1)

        # 5. Output
        print("\n" + "=" * 60)
        md_report = build_markdown_report(result)
        print(md_report)

        if args.output_json:
            data = asdict(result)
            Path(args.output_json).write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"💾 JSON: {args.output_json}")

        if args.output_md:
            Path(args.output_md).write_text(md_report)
            print(f"💾 Markdown: {args.output_md}")

    finally:
        if not args.keep_files:
            shutil.rmtree(workdir, ignore_errors=True)
            print(f"\n🧹 Cleanup di {workdir}")
        else:
            print(f"\n📂 File mantenuti in: {workdir}")


if __name__ == "__main__":
    main()

