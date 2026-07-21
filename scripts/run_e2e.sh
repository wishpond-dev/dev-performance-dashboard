#!/usr/bin/env bash
# Runs the Playwright E2E suite (tests/e2e/) against the real built
# dist/index.html, using this container's system Chromium.
#
# Prerequisites (run once, or whenever data/site changes):
#   python3 -m collector.collect   # writes data/metrics.json
#   python3 site/build.py          # writes dist/index.html
#
# Do NOT run `playwright install chromium` -- this container already
# provides one at /usr/local/bin/chromium; the suite is hardcoded to use
# it (see tests/e2e/conftest.py: CHROMIUM_PATH).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f dist/index.html ]; then
  echo "dist/index.html not found -- run: python3 site/build.py" >&2
  exit 1
fi

python3 -m pytest tests/e2e/ -v -m e2e "$@"
