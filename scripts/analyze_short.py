#!/usr/bin/env python3
"""
analyze_short.py — Pipeline completa per shorts tematici AI/IT.

Per video tipo "Agent-skills", "CCusage", "LLMSizer" che presentano UN tool/repo
e mostrano screenshot della README scrollata.

Pipeline:
  1. yt-dlp scarica video + metadata
  2. ffmpeg estrae frame croppati e ridotti a 256px (parte superiore)
  3. ffmpeg estrae audio mp3 mono 16kHz
  4. Whisper-medium trascrive (autodetect lingua, initial_prompt AI)
  5. Transcript fixer LLM corregge errori Whisper usando description
  6. llava:7b estrae JSON minimale da ogni frame (OCR vincolato)
  7. Estrae link/repo da description + transcript
  8. qwen3.5:4b cross-check identifica risorsa principale + sezioni README
  9. Validazione URL GitHub via HEAD request (anti-allucinazione)
  10. Output JSON unificato + Markdown

Stack:
  - llava:7b (~5 GB, vision)
  - Whisper API service (192.168.254.115:5001, remote)
  - qwen3.5:4b (~3.4 GB)

Tempo medio: ~2-3 minuti per short da 60-90 secondi (no local Whisper model).

Uso:
  python3 analyze_short.py "URL"
  python3 analyze_short.py URL --frames-interval 10 --keep-files
"""

from __future__ import annotations

import argparse
import base64
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
WHISPER_API_URL = os.environ.get("WHISPER_API_URL", "http://192.168.254.115:5001")

DEFAULT_VISION_MODEL = "llava:7b"
DEFAULT_TEXT_MODEL = "qwen3.5:4b"
DEFAULT_WHISPER_MODEL = "medium"
DEFAULT_FRAMES_INTERVAL = 10
DEFAULT_CROP_RATIO = 0.5
DEFAULT_FRAME_WIDTH = 256
DEFAULT_TIMEOUT = 90


# ============================================================
# DATA CLASSES (schema unificato)
# ============================================================

@dataclass
class TranscriptFix:
    original: str = ""
    corrected: str = ""
    reason: str = ""


@dataclass
class FrameAnalysis:
    frame_index: int
    timestamp_sec: float
    main_title: str = ""
    keywords: list[str] = field(default_factory=list)
    is_code: bool = False
    is_github: bool = False
    elapsed_sec: float = 0.0
    error: str = ""


@dataclass
class MainResource:
    name: str = ""
    type: str = ""  # GitHub repository | website | tool | documentation
    canonical_url: str = ""
    summary_it: str = ""
    tech: list[str] = field(default_factory=list)
    readme_sections: list[str] = field(default_factory=list)


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

    analysis_type: str = "tech_short"
    analysis_pipeline: str = "analyze_short.py"
    analysis_started_at: str = ""
    analysis_duration_sec: float = 0.0

    audio: AudioData = field(default_factory=AudioData)
    extracted_links: ExtractedLinks = field(default_factory=ExtractedLinks)

    # Sempre presente, vuoto per tech_short
    news_items: list = field(default_factory=list)

    # Specifico tech_short
    main_resource: MainResource = field(default_factory=MainResource)
    vision_frames: list[FrameAnalysis] = field(default_factory=list)

    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)


# ============================================================
# OLLAMA UTILS
# ============================================================

def unload_model(model: str) -> None:
    try:
        httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=10
        )
    except Exception:
        pass


def unload_all_loaded_models(except_model: str | None = None) -> None:
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
            unload_model(name)
    except Exception:
        pass


# ============================================================
# STEP 1: DOWNLOAD VIDEO + METADATA
# ============================================================

def download_video(url: str, workdir: Path) -> tuple[Path, dict]:
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

    return video_path, info


def extract_audio(video_path: Path, output_dir: Path) -> Path:
    audio_path = output_dir / "audio.mp3"
    print("🎙️  Estraggo audio...")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3lame",
        "-ar", "16000", "-ac", "1", "-q:a", "4",
        str(audio_path), "-loglevel", "error",
    ]
    subprocess.run(cmd, check=True)
    print(f"✅ Audio: {audio_path.name} ({audio_path.stat().st_size / 1e3:.0f} KB)")
    return audio_path


# ============================================================
# STEP 2: FRAME EXTRACTION
# ============================================================

def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    w, h = map(int, out.split("x"))
    return w, h


def extract_frames(
    video_path: Path,
    output_dir: Path,
    interval_sec: int,
    crop_ratio: float,
    target_width: int,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    w, h = get_video_dimensions(video_path)
    crop_h = int(h * crop_ratio)
    print(f"📐 Video {w}x{h} → crop {w}x{crop_h} → scale {target_width}px")
    print(f"⏱️  Estrazione 1 frame ogni {interval_sec}s...")

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"fps=1/{interval_sec},crop={w}:{crop_h}:0:0,scale={target_width}:-2",
        "-q:v", "5",
        str(output_dir / "frame_%03d.jpg"),
        "-loglevel", "error",
    ]
    subprocess.run(cmd, check=True)
    frames = sorted(output_dir.glob("frame_*.jpg"))
    sizes = [f.stat().st_size // 1024 for f in frames]
    print(f"✅ {len(frames)} frame estratti, KB: {sizes}")
    return frames


# ============================================================
# STEP 3: WHISPER TRANSCRIPTION
# ============================================================

WHISPER_INITIAL_PROMPT = (
    "Notizie tecniche di ZioBudda Labs sull'intelligenza artificiale. "
    "Termini frequenti: Claude, Claude Code, Anthropic, MCP, Karpathy, "
    "OpenAI, Google, Kaggle, NVIDIA, Nemotron, Qwen, Gemini, Python, "
    "GitHub, JEPA, Echo-2, MiniCPM, Whisper, WUPHF, Obscura, "
    "vibe coding, agenti AI, modelli linguistici, LLM, prompt engineering."
)


def transcribe_audio(
    audio_path: Path,
    model_size: str,
    whisper_api_url: str = WHISPER_API_URL,
) -> AudioData:
    print(f"\n🎙️  Trascrizione con whisper-{model_size} (via API {whisper_api_url})...")
    t0 = time.time()

    try:
        with open(audio_path, "rb") as f:
            r = httpx.post(
                f"{whisper_api_url}/transcribe",
                files={"audio": (audio_path.name, f)},
                data={
                    "model": model_size,
                    "language": "",
                    "initial_prompt": WHISPER_INITIAL_PROMPT,
                },
                timeout=300,
            )
        r.raise_for_status()
        result = r.json()
    except Exception as e:
        print(f"❌ Errore Whisper API: {e}")
        sys.exit(1)

    elapsed = time.time() - t0
    transcript = result.get("text", "").strip()
    language = result.get("language", "unknown")
    language_prob = result.get("language_probability", 0.0)

    print(f"✅ Trascrizione in {elapsed:.1f}s ({len(transcript)} char)")
    print(f"   Lingua: {language} (prob: {language_prob:.2f})")
    if transcript:
        preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
        print(f"   Testo: {preview}")

    return AudioData(
        language=language,
        language_probability=language_prob,
        transcript_raw=transcript,
        transcript_corrected=transcript,
        transcript=transcript,
        whisper_model=model_size,
        duration_sec=round(elapsed, 1),
    )


# ============================================================
# STEP 3.5: LLM TRANSCRIPT FIXER (rinforzato v8 da analyze_news.py)
# ============================================================

TRANSCRIPT_FIXER_SYSTEM = """Sei un correttore di trascrizioni audio italiane per ZioBudda Labs.

Ricevi:
1. TRASCRIZIONE WHISPER del video (può contenere errori acustici)
2. DESCRIPTION YouTube ufficiale (FONTE DI VERITA' per nomi propri e termini)

Il tuo compito: correggere SOLO errori di trascrizione evidenti, usando la description come riferimento.

REGOLE INDEROGABILI:
- Correggi parole acusticamente simili ma semanticamente sbagliate
  Esempi tipici di errori Whisper su italiano parlato:
  * "C-usage" / "Sì-usage" → "ccusage" (nome tool)
  * "LLM Scissor" → "LLMSizer" (nome tool)
  * "Cloud Code" → "Claude Code"
  * "Qn3.6" / "Quen 3.6" → "Qwen 3"
  * "Andrei Carpati" → "Karpathy" (nome proprio)
  * "tool CLA" → "tool CLI"
  * "amici e amici" → "amici"
- Correggi nomi propri sbagliati confrontando con la description
- Correggi acronimi/tool sbagliati confrontando con la description
- NON aggiungere contenuto non presente nel transcript originale
- NON parafrasare frasi che sono già corrette
- NON cambiare lo stile o l'ordine delle parole
- NON tradurre da una lingua all'altra
- Se NON sei sicuro di una correzione, lascia il testo originale ESATTAMENTE COSI'
- IMPORTANTE: NON inventare correzioni "migliori" se non c'è evidenza nella description.
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

    # Variabile esterna separata per non collidere con il loop sotto
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
            # Scarta non-fix (entries dove original == corrected)
            if fix_original == fix_corrected:
                ghost_fixes_count += 1
                continue
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
# STEP 4: VISION ANALYSIS
# ============================================================

VISION_PROMPT = """Look at this screenshot from a YouTube video. Output ONLY this JSON, nothing else.
If you cannot read text clearly, use empty string for main_title and empty array for keywords.
Do NOT copy the schema field descriptions as values.

{"main_title": "the actual largest visible text in the image, or empty string", "keywords": ["actual visible keywords, or empty"], "is_code": true_or_false, "is_github": true_or_false}"""


def encode_image_b64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


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


def is_placeholder_response(title: str, keywords: list[str]) -> bool:
    placeholder_phrases = [
        "the largest visible text",
        "the actual largest visible text",
        "the visible text",
        "actual visible keywords",
        "up to 4 keywords",
        "keywords visible",
    ]
    title_lower = title.lower()
    if any(p in title_lower for p in placeholder_phrases):
        return True
    if keywords:
        kw_text = " ".join(str(k).lower() for k in keywords)
        if any(p in kw_text for p in placeholder_phrases):
            return True
    return False


def call_llava_vision(image_path: Path, model: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "prompt": VISION_PROMPT,
        "images": [encode_image_b64(image_path)],
        "stream": False,
        "keep_alive": "5m",
        "options": {
            "temperature": 0.0,
            "num_predict": 200,
            "top_p": 0.9,
        },
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"HTTP: {e}"}
    except Exception as e:
        return {"error": f"Generic: {e}"}

    response_text = data.get("response", "").strip()
    if not response_text:
        return {"error": f"empty response (done_reason={data.get('done_reason', '')})"}

    parsed = extract_json_robust(response_text)
    if parsed is None:
        return {"error": "no JSON parsable"}

    title = str(parsed.get("main_title", ""))
    kws = parsed.get("keywords", []) if isinstance(parsed.get("keywords"), list) else []
    if is_placeholder_response(title, kws):
        return {"error": "placeholder response"}

    return parsed


def analyze_frame_with_retry(image_path: Path, model: str, timeout: int) -> dict:
    data = call_llava_vision(image_path, model, timeout)
    err = data.get("error", "") if "error" in data else ""
    should_retry = (
        "empty response" in err or "500" in err or "502" in err
        or "timeout" in err.lower() or "placeholder" in err
    )
    if should_retry:
        time.sleep(2.0)
        data = call_llava_vision(image_path, model, timeout)
    return data


def analyze_all_frames(
    frames: list[Path],
    model: str,
    interval_sec: int,
) -> list[FrameAnalysis]:
    print(f"\n🔍 Vision analysis con {model}")
    results: list[FrameAnalysis] = []

    for i, frame in enumerate(frames):
        timestamp = i * interval_sec
        size_kb = frame.stat().st_size // 1024
        print(f"   Frame {i+1}/{len(frames)} (t={timestamp}s, {size_kb}KB)...", end=" ", flush=True)

        t0 = time.time()
        data = analyze_frame_with_retry(frame, model, DEFAULT_TIMEOUT)
        elapsed = time.time() - t0

        if "error" in data:
            print(f"❌ [{elapsed:.1f}s] {data['error'][:50]}")
            results.append(FrameAnalysis(
                frame_index=i, timestamp_sec=timestamp,
                error=data["error"], elapsed_sec=elapsed,
            ))
            continue

        analysis = FrameAnalysis(
            frame_index=i,
            timestamp_sec=timestamp,
            main_title=str(data.get("main_title", "")).strip(),
            keywords=data.get("keywords", []) if isinstance(data.get("keywords"), list) else [],
            is_code=bool(data.get("is_code", False)),
            is_github=bool(data.get("is_github", False)),
            elapsed_sec=elapsed,
        )
        results.append(analysis)
        flags = []
        if analysis.is_github:
            flags.append("github")
        if analysis.is_code:
            flags.append("code")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        title_display = analysis.main_title[:50] if analysis.main_title else "(empty)"
        print(f"✅ [{elapsed:.1f}s] {title_display}{flag_str}")

    success = sum(1 for r in results if not r.error)
    avg_time = sum(r.elapsed_sec for r in results if not r.error) / success if success else 0
    print(f"\n📊 Frame OK: {success}/{len(results)} | Tempo medio: {avg_time:.1f}s")
    return results


# ============================================================
# STEP 5: LINK EXTRACTION
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
# STEP 6: CROSS-CHECK LLM
# ============================================================

CROSSCHECK_SYSTEM = """Sei un analizzatore di YouTube shorts tematici per ZioBudda Labs.

Ricevi:
1. TITOLO del video YouTube
2. DESCRIPTION YouTube (spesso contiene URL canonici)
3. LINK ESTRATTI da description + audio
4. TRASCRIZIONE AUDIO (italiano o inglese)
5. COSE VISTE A VIDEO (lista titoli/keyword da analisi vision)

Il tuo compito:
- Identificare la RISORSA PRINCIPALE che il video promuove (1 sola)
- Distinguere il soggetto principale dai contenuti della README mostrata
- Costruire un summary del video IN ITALIANO (anche se l'audio è in inglese)
- Estrarre l'URL canonico:
  * Se c'è un repo github nei LINK ESTRATTI, usa "https://github.com/user/repo"
  * Se c'è un URL diretto in description, usalo
  * Se nessun URL è disponibile, lascia stringa vuota — NON inventare

REGOLE INDEROGABILI:
- Usa SOLO informazioni presenti nei dati forniti
- Non inventare URL o nomi
- Per il summary, scrivi SEMPRE in italiano in 2-3 frasi
- Le sezioni della README mostrate vanno raggruppate come metadata
- Se il TITOLO YouTube è chiaro (es. "agent-skills"), priorità a quello

Rispondi SOLO con JSON valido, schema:
{
  "main_resource": {
    "name": "nome del progetto/repo principale",
    "type": "GitHub repository | website | tool | documentation",
    "canonical_url": "URL canonico, vuoto se non determinabile",
    "summary_it": "2-3 frasi in italiano",
    "tech": ["principali tech menzionate"],
    "readme_sections": ["titoli di capitoli/sezioni mostrati nel video"]
  },
  "audio_language": "it o en",
  "confidence": 0.0-1.0
}"""


def cross_check_with_llm(
    transcript: str,
    detected_lang: str,
    frame_analyses: list[FrameAnalysis],
    video_title: str,
    video_description: str,
    extracted_links: ExtractedLinks,
    model: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[MainResource, float]:
    print(f"\n🤝 Cross-check con: {model}")

    vision_data = []
    for a in frame_analyses:
        if a.error or not a.main_title:
            continue
        vision_data.append({
            "t": a.timestamp_sec,
            "title": a.main_title,
            "keywords": a.keywords,
            "is_github": a.is_github,
            "is_code": a.is_code,
        })

    desc_truncated = video_description[:2000] if video_description else ""

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

=== COSE VISTE A VIDEO ===
{json.dumps(vision_data, ensure_ascii=False, indent=2)}

Identifica la risorsa principale. Se nei LINK ESTRATTI c'è un repo github, usa "https://github.com/user/repo" come canonical_url. Produci il JSON richiesto."""

    payload = {
        "model": model,
        "prompt": user_prompt,
        "system": CROSSCHECK_SYSTEM,
        "stream": False,
        "think": False,
        "keep_alive": "2m",
        "options": {
            "temperature": 0.2,
            "num_predict": 1024,
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
        print(f"❌ Errore cross-check: {e}")
        return MainResource(), 0.0

    response_text = data.get("response", "").strip()
    elapsed = time.time() - t0
    print(f"✅ Cross-check in {elapsed:.1f}s")

    parsed = extract_json_robust(response_text)
    if parsed is None:
        print(f"⚠️  LLM non ha prodotto JSON. Raw: {response_text[:200]}")
        return MainResource(), 0.0

    mr_data = parsed.get("main_resource", {})
    if not isinstance(mr_data, dict):
        mr_data = {}

    main_resource = MainResource(
        name=str(mr_data.get("name", "")).strip(),
        type=str(mr_data.get("type", "")).strip(),
        canonical_url=str(mr_data.get("canonical_url", "")).strip(),
        summary_it=str(mr_data.get("summary_it", "")).strip(),
        tech=mr_data.get("tech", []) if isinstance(mr_data.get("tech"), list) else [],
        readme_sections=mr_data.get("readme_sections", []) if isinstance(mr_data.get("readme_sections"), list) else [],
    )
    confidence = float(parsed.get("confidence", 0.0))

    # FIX 1: validazione URL GitHub via HEAD request
    # Il LLM allucina URL del tipo github.com/anthropics/<tool> quando non li trova
    # nella description. Verifichiamo che esistano davvero, altrimenti li cancelliamo.
    if main_resource.canonical_url and "github.com" in main_resource.canonical_url:
        validated_url = _validate_github_url(main_resource.canonical_url)
        if validated_url != main_resource.canonical_url:
            print(f"⚠️  URL GitHub allucinato: {main_resource.canonical_url} non esiste, cancellato")
            main_resource.canonical_url = validated_url
            # Penalizza la confidence: il LLM ha inventato un dato critico
            confidence = min(confidence, 0.7)

    return main_resource, confidence


def _validate_github_url(url: str) -> str:
    """
    Verifica che un URL github.com/user/repo esista davvero (HEAD request).
    Ritorna l'URL se 200/30x, altrimenti stringa vuota.
    Tollerante: in caso di errore di rete, ritorna l'URL invariato (no false negatives).
    """
    if not url or "github.com" not in url:
        return url
    # Estrai user/repo (gestisce trailing slash, query, fragment)
    match = re.search(r"github\.com/([^/]+)/([^/?#\s]+)", url)
    if not match:
        return url
    user, repo = match.group(1), match.group(2).rstrip("/")
    canonical = f"https://github.com/{user}/{repo}"
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            r = client.head(canonical)
            if r.status_code == 200:
                return canonical
            if r.status_code == 404:
                return ""  # repo inesistente, allucinazione confermata
            # Altri status (rate limit, ecc): tollerante, ritorna URL originale
            return url
    except Exception as e:
        print(f"   ⚠️  Validazione GitHub fallita ({e}), tengo URL originale")
        return url


# ============================================================
# STEP 7: REPORT
# ============================================================

def build_markdown_report(result: VideoAnalysisResult) -> str:
    md = f"# 🎯 Short Analysis: {result.video_title}\n\n"
    md += f"**URL:** {result.video_url}\n"
    md += f"**Pubblicato:** {result.pub_date}\n"
    md += f"**Durata:** {result.video_duration_sec}s | "
    md += f"**Lingua:** {result.audio.language} | "
    md += f"**Confidence:** {result.confidence:.2f}\n\n"
    md += "---\n\n"

    mr = result.main_resource
    if mr.name:
        md += "## 🎯 Risorsa principale\n\n"
        md += f"### {mr.name}\n"
        if mr.type:
            md += f"- **Tipo:** {mr.type}\n"
        if mr.canonical_url:
            md += f"- **URL canonico:** {mr.canonical_url}\n"
        if mr.tech:
            md += f"- **Tech:** {', '.join(mr.tech)}\n"
        md += "\n"
        if mr.summary_it:
            md += f"### 📝 Summary\n{mr.summary_it}\n\n"
        if mr.readme_sections:
            md += f"### 📚 Sezioni README mostrate ({len(mr.readme_sections)})\n"
            for s in mr.readme_sections:
                md += f"- {s}\n"
            md += "\n"
    else:
        md += "_⚠️ Nessuna risorsa principale identificata._\n\n"

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
        # Distingui i 4 stati del fixer per debug visibile nel report
        if result.audio.fixer_used and result.audio.fixes_applied:
            md += f"## 🎙️ Trascrizione audio (corretta — {len(result.audio.fixes_applied)} fix applicati)\n\n"
        elif result.audio.fixer_used:
            md += "## 🎙️ Trascrizione audio (⚠️ fixer attivato ma 0 fix — possibile timeout o sanity check)\n\n"
        elif result.audio.transcript_raw and len(result.audio.transcript_raw) < 1500:
            md += f"## 🎙️ Trascrizione audio (fixer skip — transcript {len(result.audio.transcript_raw)} char < 1500)\n\n"
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

    md += "## 📋 Dettaglio frame\n\n"
    for a in result.vision_frames:
        if a.error:
            md += f"- **t={a.timestamp_sec}s**: ❌ {a.error[:50]}\n"
        elif a.main_title:
            kw = ", ".join(a.keywords[:3]) if a.keywords else ""
            md += f"- **t={a.timestamp_sec}s** ({a.elapsed_sec:.1f}s): {a.main_title}"
            if kw:
                md += f" — _{kw}_"
            md += "\n"
        else:
            md += f"- **t={a.timestamp_sec}s**: _vuoto_\n"

    md += f"\n---\n_Analizzato in {result.analysis_duration_sec:.1f}s da {result.analysis_pipeline}_\n"
    return md


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Analizza short tematico (vision + audio)")
    parser.add_argument("url", help="URL del video YouTube")
    parser.add_argument("--frames-interval", type=int, default=DEFAULT_FRAMES_INTERVAL)
    parser.add_argument("--crop-ratio", type=float, default=DEFAULT_CROP_RATIO)
    parser.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_WIDTH)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--text-model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL,
                        choices=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"])
    parser.add_argument("--whisper-api-url", default=WHISPER_API_URL,
                        help=f"URL Whisper API service (default: {WHISPER_API_URL})")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-fix", action="store_true",
                        help="disabilita il transcript fixer LLM (più veloce, transcript Whisper raw)")
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

    workdir = Path(tempfile.mkdtemp(prefix="short_analysis_"))
    print(f"📂 Working directory: {workdir}\n")

    pipeline_start = time.time()
    result = VideoAnalysisResult(
        video_url=args.url,
        analysis_started_at=datetime.utcnow().isoformat() + "Z",
    )

    try:
        # 1. Download
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

        # 2. Frame
        frames_dir = workdir / "frames"
        frames = extract_frames(
            video, frames_dir,
            args.frames_interval, args.crop_ratio, args.frame_width
        )
        if not frames:
            print("❌ Nessun frame estratto", file=sys.stderr)
            sys.exit(1)

        # 3. Audio + Whisper
        if not args.no_audio:
            audio = extract_audio(video, workdir)
            result.audio = transcribe_audio(audio, args.whisper_model, args.whisper_api_url)

        # 3.5. Transcript fixer LLM (corregge errori Whisper usando description)
        # Skip per transcript corti: il modello 4B su short brevi tende ad
        # allucinare e il sanity check scatta comunque. Tempo sprecato.
        FIXER_MIN_LEN = 1500
        if not args.no_audio and not args.no_fix and result.audio.transcript_raw:
            transcript_len = len(result.audio.transcript_raw)
            if transcript_len < FIXER_MIN_LEN:
                print(f"\n⏭️  Transcript {transcript_len} char < {FIXER_MIN_LEN}: skip fixer")
                print(f"   (su short brevi il fixer 4B allucina, raw è più affidabile)")
            else:
                print("\n🧹 Pulizia VRAM prima del transcript fixer...")
                unload_all_loaded_models(except_model=args.text_model)
                time.sleep(2)

                # Timeout adattivo: 150s base + 1s ogni 40 char, capped 360s
                # (base 150 invece di 120 per gestire cold start del modello su short corti)
                adaptive_timeout = min(150 + (transcript_len // 40), 360)
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

        # 4. Vision
        print("\n🧹 Pulizia VRAM prima del vision...")
        unload_all_loaded_models(except_model=args.vision_model)
        time.sleep(2)
        result.vision_frames = analyze_all_frames(frames, args.vision_model, args.frames_interval)

        # 5. Link extraction
        combined_text = (result.video_description or "") + " " + (result.audio.transcript or "")
        result.extracted_links = extract_links_and_repos(combined_text)
        total_links = (
            len(result.extracted_links.urls) +
            len(result.extracted_links.github_repos) +
            len(result.extracted_links.huggingface_models)
        )
        if total_links > 0:
            print(f"\n🔗 Link estratti: {total_links}")

        # 6. Cross-check
        print("\n🧹 Pulizia VRAM prima del cross-check...")
        unload_model(args.vision_model)
        time.sleep(2)
        main_resource, confidence = cross_check_with_llm(
            result.audio.transcript,
            result.audio.language,
            result.vision_frames,
            result.video_title,
            result.video_description,
            result.extracted_links,
            args.text_model,
        )
        result.main_resource = main_resource
        result.confidence = confidence

        result.analysis_duration_sec = round(time.time() - pipeline_start, 1)

        # 7. Output
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
