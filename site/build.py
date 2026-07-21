#!/usr/bin/env python3
"""Build the self-contained Development Performance Dashboard.

Usage:
    python3 site/build.py [--metrics data/metrics.json] [--site-dir site] [--out dist/index.html]

Reads site/index.html, site/dashboard.css, site/dashboard.js, and the
collector's data/metrics.json, then produces a single self-contained
dist/index.html: dashboard.css is inlined into a <style> tag, dashboard.js
into a <script> tag, and metrics.json into the #metrics-data script tag,
replacing the placeholder fixture. The Inter and JetBrains Mono fonts are
self-hosted as woff2 files under site/fonts/ and are base64-inlined into
the CSS's @font-face src: url(...) references before the CSS is embedded,
so the built page makes zero font network requests. The only remaining
external reference is the Chart.js CDN <script> tag -- no runtime fetch()
calls remain.

Deterministic: running this script twice against unchanged inputs produces
a byte-identical dist/index.html (same inputs -> same bytes; the build step
itself introduces no timestamps or randomness).

Run the collector first if data/metrics.json does not exist yet:
    python3 -m collector.collect
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_CSS_LINK_RE = re.compile(r'<link rel="stylesheet" href="dashboard\.css">')
_JS_SCRIPT_RE = re.compile(r'<script src="dashboard\.js"></script>')
_METRICS_SCRIPT_RE = re.compile(
    r'(<script id="metrics-data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)
_FONT_URL_RE = re.compile(r"url\('fonts/([^']+\.woff2)'\)")


class BuildError(RuntimeError):
    """Raised when a required build input or html anchor is missing."""


def _escape_for_inline_script(json_text: str) -> str:
    # Prevent a "</script" sequence inside free-text data (commit messages,
    # PR titles) from prematurely closing the <script> tag.
    return json_text.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")


def _inline_fonts(css: str, site_dir: Path) -> str:
    # Replace each url('fonts/<name>.woff2') reference with a base64 data
    # URI so the built CSS makes zero font network requests.
    def _replace(match: "re.Match[str]") -> str:
        font_name = match.group(1)
        font_path = site_dir / "fonts" / font_name
        if not font_path.is_file():
            raise BuildError(f"font file not found: {font_path}")
        encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        return f"url('data:font/woff2;base64,{encoded}')"

    return _FONT_URL_RE.sub(_replace, css)


def build(site_dir: Path, metrics_path: Path, out_path: Path) -> str:
    if not metrics_path.is_file():
        raise BuildError(
            f"{metrics_path} not found -- run the collector first: python3 -m collector.collect"
        )

    index_html = (site_dir / "index.html").read_text(encoding="utf-8")
    css = (site_dir / "dashboard.css").read_text(encoding="utf-8")
    js = (site_dir / "dashboard.js").read_text(encoding="utf-8")
    metrics_text = metrics_path.read_text(encoding="utf-8").strip()
    json.loads(metrics_text)  # fail fast on malformed metrics.json

    if not _CSS_LINK_RE.search(index_html):
        raise BuildError("dashboard.css <link> tag not found in index.html")
    if not _JS_SCRIPT_RE.search(index_html):
        raise BuildError("dashboard.js <script> tag not found in index.html")
    if not _METRICS_SCRIPT_RE.search(index_html):
        raise BuildError("#metrics-data script tag not found in index.html")

    css = _inline_fonts(css, site_dir)

    html = _CSS_LINK_RE.sub(lambda _m: f"<style>\n{css}\n</style>", index_html, count=1)
    html = _JS_SCRIPT_RE.sub(lambda _m: f"<script>\n{js}\n</script>", html, count=1)
    inlined = _escape_for_inline_script(metrics_text)
    html = _METRICS_SCRIPT_RE.sub(
        lambda m: f"{m.group(1)}\n{inlined}\n{m.group(3)}", html, count=1
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return html


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--site-dir", type=Path, default=REPO_ROOT / "site")
    parser.add_argument("--metrics", type=Path, default=REPO_ROOT / "data" / "metrics.json")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist" / "index.html")
    args = parser.parse_args(argv)

    try:
        build(args.site_dir, args.metrics, args.out)
    except BuildError as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 1

    print(f"Built {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
