#!/usr/bin/env bash
# Install the free terminal-recording pipeline (no accounts, no payment):
#   asciinema 2.4 (PyPI, pure Python)  -> records the terminal session to a .cast (text)
#   agg (asciinema gif generator, one static binary) -> .cast -> .gif
#   ffmpeg (system package)             -> .gif -> .mp4 (H.264, yuv420p)
# asciinema goes into demo-video/.tools (gitignored, rebuilt by this script). agg and ffmpeg must be on PATH.
set -euo pipefail
cd "$(dirname "$0")"
TOOLS="$PWD/.tools"
if [ ! -x "$TOOLS/bin/asciinema" ]; then
  uv venv --quiet --python 3.13 "$TOOLS"
  uv pip install --quiet --python "$TOOLS/bin/python" "asciinema==2.4.0"
fi
# Pillow draws the title/architecture cards of the final video (demo-video/final/cards.py)
"$TOOLS/bin/python" -c "import PIL" 2>/dev/null || uv pip install --quiet --python "$TOOLS/bin/python" pillow
"$TOOLS/bin/asciinema" --version
for t in agg ffmpeg; do
  command -v "$t" >/dev/null || { echo "missing $t: install it (agg = asciinema's gif generator, a single prebuilt binary placed on PATH, e.g. ~/.local/bin; ffmpeg = distro package)" >&2; exit 1; }
done
agg --version
ffmpeg -version | head -1
