#!/usr/bin/env bash
# Rebuild the precompiled Tailwind stylesheet for the claude-logging webui.
#
# WHY THIS EXISTS (task-4114): the satellite serves a *precompiled* tailwind.css
# instead of the Tailwind Play CDN (the CDN compiled utilities in-browser → ~2s
# load + needed network). The tradeoff: any time you add/change a Tailwind class
# in static/index.html you MUST rebuild, or the new class won't exist in the CSS
# and the layout silently breaks. Run this after editing classes.
#
# Usage:
#   ./build.sh          # one-shot rebuild
#   ./build.sh --watch  # rebuild on every save (dev)
set -euo pipefail
cd "$(dirname "$0")"

ARGS=(-c .twbuild/tailwind.config.js -i .twbuild/input.css -o static/tailwind.css --minify)
if [[ "${1:-}" == "--watch" ]]; then
  echo "tailwind: watching static/index.html → static/tailwind.css (Ctrl+C to stop)"
  exec npx -y tailwindcss@3 "${ARGS[@]}" --watch
fi

npx -y tailwindcss@3 "${ARGS[@]}"
echo "tailwind: rebuilt static/tailwind.css ($(wc -c < static/tailwind.css) bytes)"
