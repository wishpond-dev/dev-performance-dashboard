"""Shared fixtures for the tests/e2e/ Playwright suite (TASK-004-016).

Serves the real built `dist/index.html` from a local HTTP server (not a
bare `file://` URL) so E2E-002/E2E-008's network-request assertions observe
genuine HTTP semantics, matching test-plan.md's "open the built
dist/index.html from a local static server" wording. Launches the system
Chromium (`/usr/local/bin/chromium`, `--no-sandbox`) per spec.md S14 --
do NOT `playwright install chromium` in this container, it is already
provisioned at that path.

All tests in this package require `dist/index.html` to exist (run
`python3 site/build.py` first, which itself requires `data/metrics.json`
from `python3 -m collector.collect`) -- they skip cleanly, not error, if
it's absent, mirroring tests/site_build/conftest.py's `real_metrics_path`
pattern.
"""
from __future__ import annotations

import functools
import http.server
import json
import re
import threading
from pathlib import Path

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DIST_INDEX = REPO_ROOT / "dist" / "index.html"
SITE_DIR = REPO_ROOT / "site"
REAL_METRICS_PATH = REPO_ROOT / "data" / "metrics.json"
CHROMIUM_PATH = "/usr/local/bin/chromium"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def site_dir() -> Path:
    return SITE_DIR


@pytest.fixture(scope="session")
def real_metrics_path() -> Path:
    if not REAL_METRICS_PATH.is_file():
        pytest.skip("data/metrics.json not present -- run `python3 -m collector.collect` first")
    return REAL_METRICS_PATH


@pytest.fixture(scope="session")
def dist_index_path() -> Path:
    if not DIST_INDEX.is_file():
        pytest.skip("dist/index.html not present -- run `python3 site/build.py` first")
    return DIST_INDEX


@pytest.fixture(scope="session")
def metrics_data(dist_index_path: Path) -> dict:
    html = dist_index_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="metrics-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert match, "dist/index.html has no #metrics-data script block"
    return json.loads(match.group(1))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 -- stdlib handler signature
        pass  # keep pytest -s output free of per-request access logs


@pytest.fixture(scope="session")
def server_url(dist_index_path: Path):
    handler = functools.partial(_QuietHandler, directory=str(dist_index_path.parent))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    """Module-scoped, not session-scoped: a session-scoped sync_playwright()
    context stays open until the whole pytest session ends, which collides
    with tests/site_build/test_dist_render.py's own independent
    `with sync_playwright()` call when the full suite runs together
    ("Playwright Sync API inside the asyncio loop" error). Module scope
    tears this down as soon as this file's tests finish, before any other
    test file gets a chance to open a second Playwright instance."""
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM_PATH, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def context(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    pg = context.new_page()
    pg.console_errors = []
    pg.page_errors = []
    pg.on(
        "console",
        lambda msg: pg.console_errors.append(msg.text) if msg.type == "error" else None,
    )
    pg.on("pageerror", lambda exc: pg.page_errors.append(str(exc)))
    yield pg
    pg.close()


@pytest.fixture
def loaded_page(page, server_url):
    """A page already navigated to the real built dashboard and settled."""
    page.goto(server_url)
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)
    return page


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: Playwright browser tests against the built dist/index.html (slow)"
    )
