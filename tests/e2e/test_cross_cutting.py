"""Cross-cutting E2E-* tests -- test-plan.md's "End-to-end dashboard
experience" area, spec.md S14. These tie every panel together and do not
re-test any single panel's internals (see test_shell.py, test_overview.py,
test_devpanel.py, test_repofilter.py for panel-scoped coverage).

Covers: E2E-SMOKE-001, E2E-002 .. E2E-012.
"""
from __future__ import annotations

import re

import pytest

from constants import BOT_AND_NON_ROSTER_MARKERS, ROSTER_NAMES, SECTION_IDS

pytestmark = pytest.mark.e2e


def test_e2e_smoke_001_full_page_load_all_panels_zero_console_errors(loaded_page):
    page = loaded_page
    for section_id in SECTION_IDS:
        page.locator(f"#{section_id}").scroll_into_view_if_needed()
        page.wait_for_timeout(50)
    assert page.console_errors == []
    assert page.page_errors == []


def test_e2e_002_zero_runtime_data_fetches_after_load(context, server_url):
    """The only allowed request after `load` fires is none at all -- the
    Chart.js CDN <script> tag is a browser-initiated page resource that
    loads at/near `load`, not a JS-driven fetch/xhr, so it's excluded from
    this assertion by resource_type (matches tests/site_build's precedent)."""
    fetches_after_load = []
    state = {"loaded": False}
    page = context.new_page()
    page.on("load", lambda: state.__setitem__("loaded", True))

    def _on_request(request):
        if state["loaded"] and request.resource_type in ("fetch", "xhr"):
            fetches_after_load.append(request.url)

    page.on("request", _on_request)
    page.goto(server_url)
    page.wait_for_load_state("load")
    page.wait_for_timeout(1500)
    page.close()

    assert fetches_after_load == []


def test_e2e_003_metrics_data_blob_matches_documented_contract(metrics_data):
    assert len(metrics_data["roster"]) == 8
    assert [p["name"] for p in metrics_data["roster"]] == ROSTER_NAMES
    assert len(metrics_data["window"]["months"]) == 12
    assert metrics_data["team"]["dora"]["mttr"] is None


def test_e2e_004_dist_is_fully_self_contained(dist_index_path):
    html = dist_index_path.read_text(encoding="utf-8")
    assert 'href="dashboard.css"' not in html
    assert 'src="dashboard.js"' not in html
    external_scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert len(external_scripts) == 1, f"expected exactly 1 external <script src>, got {external_scripts}"
    assert "chart" in external_scripts[0].lower()


def test_e2e_005_and_010_roster_integrity_page_wide(loaded_page):
    """Exactly 8 scorecards render, and no bot/non-roster marker leaks into
    ANY panel's rendered text (E2E-005 scope) or free-text field (E2E-010)."""
    page = loaded_page
    assert page.locator(".scorecard").count() == 8
    body_text = page.locator("body").inner_text()
    for marker in BOT_AND_NON_ROSTER_MARKERS:
        assert marker not in body_text, f"bot/non-roster marker {marker!r} leaked into rendered page"
    for name in ROSTER_NAMES:
        assert name in body_text, f"roster member {name!r} missing from rendered page"


def test_e2e_006_full_interaction_sweep_zero_console_errors(loaded_page):
    page = loaded_page
    page.click("#activityToggle")
    page.wait_for_timeout(100)
    page.click("#activityToggle")

    first_card = page.locator(".scorecard").first
    first_card.click()
    page.wait_for_timeout(100)
    first_card.click()  # clear
    page.wait_for_timeout(100)

    for section_id in SECTION_IDS:
        page.locator(f"#{section_id}").hover()

    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(100)
    page.set_viewport_size({"width": 1280, "height": 900})

    assert page.console_errors == []
    assert page.page_errors == []


def test_e2e_007_all_8_panels_present_and_nonempty(loaded_page):
    page = loaded_page
    assert page.locator("#statRoster").inner_text() != "—"
    for section_id in SECTION_IDS:
        assert page.locator(f"#{section_id}").count() == 1, f"section #{section_id} missing"
    assert page.locator("#scorecardGrid .scorecard").count() == 8
    assert page.locator("#reviewList .review-row").count() == 8
    assert page.locator("#doraGrid .dora-box").count() == 4
    assert page.locator("#repoTableBody .repo-row").count() == 13
    assert page.locator("#h1h2Grid").locator("> *").count() >= 4


def test_e2e_008_still_functional_after_all_network_blocked(loaded_page, context):
    page = loaded_page
    context.route("**/*", lambda route: route.abort())

    page.click("#activityToggle")
    page.wait_for_timeout(100)
    first_card = page.locator(".scorecard").first
    first_card.click()
    page.wait_for_timeout(100)
    first_card.click()
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(100)

    assert page.console_errors == []
    assert page.page_errors == []


def test_e2e_009_build_twice_is_byte_identical(site_dir, real_metrics_path, tmp_path):
    """Build-level determinism, exercised against the REAL data/metrics.json
    (not a synthetic fixture) so this suite doesn't just take
    tests/site_build/test_build.py's synthetic-fixture coverage on faith."""
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("site_build", site_dir / "build.py")
    build_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_module)

    out1 = tmp_path / "run1" / "index.html"
    out2 = tmp_path / "run2" / "index.html"
    build_module.build(site_dir, real_metrics_path, out1)
    build_module.build(site_dir, real_metrics_path, out2)

    assert hashlib.sha256(out1.read_bytes()).digest() == hashlib.sha256(out2.read_bytes()).digest()


def test_e2e_012_dist_artifact_is_well_formed_html5(dist_index_path):
    html = dist_index_path.read_text(encoding="utf-8")
    assert dist_index_path.stat().st_size > 0
    assert html.strip().lower().startswith("<!doctype html>")
    assert "<html" in html and "<head" in html and "<body" in html and "</html>" in html
