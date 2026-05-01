#!/bin/bash
# batch_test_short_v2.sh — Validazione finale analyze_short.py su 2 short tematici puliti

VIDEOS=(
  "https://www.youtube.com/shorts/Sawb7xEtYVE"   # CCusage (74s, tool con repo GitHub)
  "https://www.youtube.com/shorts/zVg2IgxHkAk"   # LLMSizer (58s, tool web in bio)
)

OUTDIR="/tmp/batch_short_v2"
mkdir -p "$OUTDIR"
> "$OUTDIR/summary.txt"

START_BATCH=$(date +%s)

for url in "${VIDEOS[@]}"; do
  vid_id=$(echo "$url" | sed 's|.*/||')
  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "🎬 Processando: $vid_id"
  echo "═══════════════════════════════════════════════════════"

  python3 analyze_short.py "$url" \
    --output-md "$OUTDIR/${vid_id}.md" \
    --output-json "$OUTDIR/${vid_id}.json" \
    > "$OUTDIR/${vid_id}.log" 2>&1

  echo "✅ $vid_id completato"
done

ELAPSED_BATCH=$(($(date +%s) - START_BATCH))
echo ""
echo "═══════════════════════════════════════════════════════"
echo "📊 RIEPILOGO BATCH (totale: ${ELAPSED_BATCH}s)"
echo "═══════════════════════════════════════════════════════"

for json in "$OUTDIR"/*.json; do
  vid=$(basename "$json" .json)
  echo ""
  echo "─── $vid ───"
  jq -r '
    "Titolo:          " + .video_title,
    "Durata video:    " + (.video_duration_sec | tostring) + "s",
    "Lingua audio:    " + .audio.language + " (prob " + (.audio.language_probability | tostring) + ")",
    "Confidence:      " + (.confidence | tostring),
    "Tempo analisi:   " + (.analysis_duration_sec | tostring) + "s",
    "Risorsa:         " + .main_resource.name,
    "Tipo:            " + .main_resource.type,
    "URL canonico:    " + .main_resource.canonical_url,
    "Tech:            " + (.main_resource.tech | join(", ")),
    "Sezioni README:  " + (.main_resource.readme_sections | length | tostring),
    "Frame OK:        " + ([.vision_frames[] | select(.error == "")] | length | tostring) + "/" + (.vision_frames | length | tostring),
    "Transcript inizio: " + (.audio.transcript[:120])
  ' "$json" 2>/dev/null
done | tee "$OUTDIR/summary.txt"

echo ""
echo "📁 File generati in $OUTDIR/"
ls -la "$OUTDIR/"
