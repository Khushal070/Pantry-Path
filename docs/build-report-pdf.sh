#!/usr/bin/env bash
# Build the PDF of a report in docs/ with pandoc and headless Chrome, in the look of
# docs/data-pipeline-report.pdf (stylesheet: docs/report-pdf.css). Figures are embedded,
# so the PDF stands on its own. The PDF is written next to the Markdown file.
#   bash docs/build-report-pdf.sh                    # docs/model-pipeline-report.md
#   bash docs/build-report-pdf.sh docs/<report>.md "Pantry Path <name> report"
# Needs: pandoc 3+, and Google Chrome or Chromium 131+ (CSS page margin boxes draw the
# footer). Set CHROME to the browser's path if it isn't found.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="${1:-$here/model-pipeline-report.md}"
footer="${2:-Pantry Path model pipeline report}"
[ -f "$src" ] || { echo "No such report: $src" >&2; exit 1; }
src_dir="$(cd "$(dirname "$src")" && pwd)"
out="$src_dir/$(basename "${src%.md}").pdf"
title="$(sed -n 's/^# //p' "$src" | head -n 1)"

command -v pandoc >/dev/null || { echo "pandoc not found" >&2; exit 1; }
chrome="${CHROME:-}"
if [ -z "$chrome" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           google-chrome google-chrome-stable chromium chromium-browser; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then chrome="$c"; break; fi
  done
fi
[ -n "$chrome" ] || { echo "Chrome or Chromium not found; set CHROME" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# the footer differs per report, so it is added here rather than in the stylesheet
printf '<style>@page { @bottom-center { content: "%s · " counter(page); } }</style>\n' \
  "$footer" > "$tmp/footer.html"
# GFM input: an image's alt text stays alt text, not a second caption under the figure
pandoc "$src" -f gfm -t html5 -s --embed-resources --resource-path="$src_dir" \
  --css "$here/report-pdf.css" --include-in-header "$tmp/footer.html" \
  --metadata pagetitle="$title" -o "$tmp/report.html"
rm -f "$out"
"$chrome" --headless --disable-gpu --no-pdf-header-footer \
  --run-all-compositor-stages-before-draw --virtual-time-budget=10000 \
  --print-to-pdf="$out" "file://$tmp/report.html" 2>/dev/null
[ -s "$out" ] || { echo "Chrome wrote no PDF" >&2; exit 1; }
echo "$out"
