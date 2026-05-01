#!/bin/bash
# batch_test_news.sh — Validazione robustezza analyze_news.py su 3 digest

VIDEOS=(
  "https://www.youtube.com/watch?v=M9EpVNuDkKI"   # Notizie 30 Aprile 2026 (121s, corto)
  "https://www.youtube.com/watch?v=Vp2zLQ7Trzs"   # Notizie 28 Aprile 2026 (227s, lungo)
  "https://www.youtube.com/watch?v=JwmlSd5FoW4"   # News 09 Aprile 2026 (183s, medio)
)

OUTDIR="/tmp/batch_news"
mkdir -p "$OUTDIR"
> "$OUTDIR/summary.txt"

START_BATCH=$(date +%s)

for url in "${VIDEOS[@]}"; do
  vid_id=$(echo "$url" | sed 's/.*v=//' | cut -d'&' -f1)
  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo "🎬 Processando: $vid_id"
  echo "═══════════════════════════════════════════════════════"

  python3 analyze_news.py "$url" \
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
    "Titolo:          \(.video_title)",
    "Durata video:    \(.video_duration_sec)s",
    "Lingua:          \(.audio.language) (prob \(.audio.language_probability))",
    "Confidence:      \(.confidence)",
    "News count:      \(.news_items | length)",
    "Fix applicati:   \(
