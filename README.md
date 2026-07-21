# Development Performance Dashboard

A static dashboard reporting monthly developer activity for the 9-person
roster across 8 in-scope repositories, covering **Jul 2025 - Jun 2026**.
Metrics are built from git history (commits, code churn) and the GitHub API
(pull requests, reviews, CI check runs) and rendered as a single
self-contained HTML file with no backend and no runtime dependencies.

See `implementation-plan.md` and `spec.md` in the originating run for the
full design rationale.

## What this is

- **Scope**: 9 people x 8 repos x monthly buckets from 2025-07 through
  2026-06.
- **Sources**: local git clones (commits, authorship, churn) plus the GitHub
  REST API (PRs, reviews, CI check runs) via `collector/github_source.py`.
- **Output**: one static, shareable HTML dashboard — open it in a browser,
  or serve it from any static host. No server, database, or JS build step
  required to view it.

## Architecture

```
collector/   Python collector: reads repo git history + GitHub API
             -> writes data/metrics.json
site/        Dashboard shell (index.html, dashboard.css, dashboard.js)
             + build.py, which inlines CSS/JS/metrics.json into one file
dist/        Build output: dist/index.html (self-contained, deployable)
config/      repos.json (8 repos), identity-map.json (9-person roster +
             aliases/bots/departed), score-weights.json
data/        Collector output: metrics.json, per-repo/month raw JSON,
             Markdown summaries
tests/       tests/collector/, tests/site_build/, tests/e2e/ (Playwright)
```

Pipeline: **collector -> `data/metrics.json` -> `site/build.py` ->
`dist/index.html`**. Each stage is independently runnable and the whole
chain is idempotent — re-running it from a clean `data/metrics.json`
reproduces the same dashboard.

## Setup

```bash
pip install -r requirements.txt
```

`requirements.txt` installs `requests`, `python-dateutil` (collector),
`pytest` (unit/integration tests), and `playwright` (E2E tests).

The collector needs GitHub API access for PR/review/CI data:

```bash
export GH_TOKEN=ghp_your_personal_access_token
```

If `GH_TOKEN` is not set, `collector/github_source.py` falls back to a
token embedded in each repo's own git remote URL (extracted from
`git config --get remote.origin.url` inside that repo's local clone,
never logged or printed). This fallback exists so the collector can run
unattended against repos that already have an authenticated remote
configured — setting `GH_TOKEN` explicitly is still the preferred,
predictable path for local/CI runs.

## Running the collector

From the project root:

```bash
python3 -m collector.collect
```

This reads `config/repos.json` (the 8 repos), `config/identity-map.json`
(the 9-person roster and email/handle aliases), and
`config/score-weights.json`, walks each repo's git history and the GitHub
API for the Jul 2025 - Jun 2026 window, and writes `data/metrics.json`
plus per-repo/month raw JSON under `data/raw/`.

Optional flags (all default to the paths above, resolved relative to the
current working directory — run from the project root):

```bash
python3 -m collector.collect \
  --repos config/repos.json \
  --identity-map config/identity-map.json \
  --weights config/score-weights.json \
  --data-dir data
```

## Building the dashboard

```bash
python3 site/build.py
```

Reads `site/index.html`, `site/dashboard.css`, `site/dashboard.js`, and
`data/metrics.json`, and writes a single self-contained `dist/index.html`
with the CSS and JS inlined and the metrics JSON embedded directly in the
page. Run this after the collector — `build.py` exits with an error if
`data/metrics.json` doesn't exist yet, pointing you back to
`python3 -m collector.collect`. This script can be run from any working
directory; its default paths resolve relative to the repo root, not the cwd.

Optional flags:

```bash
python3 site/build.py --metrics data/metrics.json --site-dir site --out dist/index.html
```

## Running tests

135 tests total, all passing:

```bash
pytest                # everything (collector + site_build + e2e), ~46s
pytest -m "not e2e"   # collector + site_build only, fast (~99 tests)
pytest -m e2e         # Playwright E2E suite only (~36 tests)
```

The `e2e` marker is defined in `pytest.ini` and covers `tests/e2e/`, which
drives the real built `dist/index.html` in a headless Chromium browser via
Playwright. There's also a dedicated script that checks `dist/index.html`
exists first:

```bash
./scripts/run_e2e.sh
```

Before running the E2E suite (directly or via the script), make sure
`data/metrics.json` and `dist/index.html` are up to date:

```bash
python3 -m collector.collect
python3 site/build.py
./scripts/run_e2e.sh
```

Do not run `playwright install chromium` — E2E tests are hardcoded to use
this container's existing Chromium binary.

## Deployment

`dist/index.html` is a single self-contained static file — once built, it
has **zero runtime dependencies**: no external CSS/JS/data fetches, no API
calls, no backend, no external font requests (Inter and JetBrains Mono are
self-hosted under `site/fonts/` and base64-inlined into the CSS at build
time). (The only external reference is the Chart.js CDN `<script>` tag,
loaded directly by the browser, same as any static page.) That means
deployment is just "put this file somewhere a browser can reach it." See `DEPLOYMENT.md` for the
GitHub Pages and static-hosting options in detail.

## Re-running for a new month

The Jul 2025 - Jun 2026 reporting window is defined as constants in
`collector/git_source.py` (`WINDOW_START_MONTH`, `WINDOW_END_MONTH`), not
in a JSON config file. To extend the window for a new month, update those
two constants, then re-run the pipeline:

```bash
python3 -m collector.collect
python3 site/build.py
```

Both scripts are idempotent — re-running them from scratch regenerates
`data/metrics.json` and `dist/index.html` from current repo state, with no
manual cleanup required. The 8-repo list (`config/repos.json`) and the
9-person roster (`config/identity-map.json`) are separate config files and
don't need to change just to add a month.

## Layout

- `collector/` — Python collector (git + GitHub sources)
- `config/` — `repos.json`, `identity-map.json`, `score-weights.json`
- `data/` — collector output: raw per-repo/month JSON, `metrics.json`
- `site/` — dashboard shell (HTML/CSS/JS) and `build.py`
- `dist/` — built static output (`index.html`), deployable as-is
- `tests/` — pytest (collector, site_build) and Playwright (e2e) suites
