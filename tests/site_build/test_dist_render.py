"""Playwright smoke test for the built dist/index.html (TASK-004-015):
loads the real, self-contained dashboard build in the system Chromium
(/usr/local/bin/chromium, --no-sandbox, per spec.md S14) and confirms it
renders without console errors and makes zero JS-initiated (fetch/xhr)
network requests after the page's `load` event. This is the practical
proof of spec.md S5/S9's "single static page, zero runtime fetches"
requirement, ahead of the full interaction sweep test-plan.md defers to
TASK-004-016's dedicated E2E suite (tests/e2e/, still unimplemented).

The Chart.js CDN <script> resource load is a browser-initiated page
resource, not a JS runtime fetch, and normally resolves before or very
close to `load` -- it is intentionally excluded from the post-load
request assertion below to avoid coupling this build-script test to
script-loading timing, which is not build.py's concern. Fonts (Inter,
JetBrains Mono) are self-hosted and base64-inlined into the CSS at build
time, so they never trigger a separate network request at all.
"""
from __future__ import annotations

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright

CHROMIUM_PATH = "/usr/local/bin/chromium"


def test_dist_index_html_renders_without_console_errors_or_post_load_fetches(
    build_module, site_dir, real_metrics_path, tmp_path
):
    out = tmp_path / "dist" / "index.html"
    build_module.build(site_dir, real_metrics_path, out)

    console_errors = []
    page_errors = []
    fetches_after_load = []
    state = {"loaded": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH, args=["--no-sandbox"])
        page = browser.new_page()

        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("load", lambda: state.__setitem__("loaded", True))

        def _on_request(request):
            if state["loaded"] and request.resource_type in ("fetch", "xhr"):
                fetches_after_load.append(request.url)

        page.on("request", _on_request)

        page.goto(out.resolve().as_uri())
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)

        browser.close()

    assert console_errors == []
    assert page_errors == []
    assert fetches_after_load == []
