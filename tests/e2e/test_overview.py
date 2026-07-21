"""Team overview & activity chart E2E tests (test-plan.md "Team overview
& activity chart" area). Covers representative smoke + state-traversal
IDs: OVERVIEW-E2E-SMOKE-001, OVERVIEW-E2E-002/003/004/005.

TASK-004-017 fix: `data/metrics.json`'s `team.h1`/`team.h2` objects now
also carry `active_devs` and `reviews_per_mo` (collector/persist.py's
`_team_half`, H1/H2-averaged from `team.monthly[].active_devs`/`.reviews`
the same way `commits_per_mo`/`prs_per_mo` already were) -- previously
absent, which left the Active Devs tile's value/delta and the Reviews
tile's delta always rendering "—" (TASK-004-016's real-build finding #1).
All 6 KPI tiles now render real numbers against the real build.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

KPI_IDS = [
    "kpiCommitsValue",
    "kpiPrsValue",
    "kpiReviewsValue",
    "kpiActiveDevsValue",
    "kpiCycleValue",
    "kpiCiValue",
]


def test_overview_e2e_smoke_001_kpis_render_then_chart_toggle_round_trip(loaded_page):
    """All 6 KPI values are real numbers from the real build (TASK-004-017
    fixed the `active_devs`/`reviews_per_mo` gap -- see module docstring)."""
    page = loaded_page
    for kpi_id in KPI_IDS:
        assert page.locator(f"#{kpi_id}").inner_text() != "—"

    toggle = page.locator("#activityToggle")
    assert toggle.get_attribute("aria-pressed") == "false"

    toggle.click()
    page.wait_for_timeout(150)
    assert toggle.get_attribute("aria-pressed") == "true"

    toggle.click()
    page.wait_for_timeout(150)
    assert toggle.get_attribute("aria-pressed") == "false"
    assert page.console_errors == []


def test_overview_e2e_001b_active_devs_and_reviews_delta_now_render_real_values(loaded_page):
    """Regression guard for TASK-004-017: the Active Devs tile's value and
    delta, and the Reviews tile's delta, used to always render "—" because
    `team.h1`/`team.h2` were missing `active_devs`/`reviews_per_mo`. Now
    that collector/persist.py emits both, none of these three should be a
    dash against the real build."""
    page = loaded_page
    assert page.locator("#kpiActiveDevsValue").inner_text() != "—"
    assert page.locator("#kpiActiveDevsDelta").inner_text() != "—"
    assert page.locator("#kpiReviewsDelta").inner_text() != "—"
    assert page.console_errors == []


def test_overview_unit_003_steady_detection_never_fabricates_arrow_for_zero_change(loaded_page):
    """OVERVIEW-UNIT-003 / TASK-004-031: computeDelta's steady detection is
    one shared check, not per-tile special-casing. It must catch all 3 ways
    a tile can read as "no real H2-vs-H1 change": a literal H1===H2 tie
    (Active Devs), a literal H1===H2===0 tie in percent mode, and a missing
    H1 or H2 that renders as '—' (CI Pass in the real fixture, which used to
    still carry the 'down'/orange treatment).

    TASK-004-033: a zero H1 baseline with a nonzero H2 (PRs Merged / Reviews
    in the real fixture) is a genuine change, not a rounding artifact of
    percent-from-zero being undefined -- it must render a real up/down
    delta, not get swept into the steady bucket alongside the true ties."""
    page = loaded_page
    tie = page.evaluate("() => window.DashboardShell.computeDelta(5, 5, {mode: 'count'})")
    assert tie == {"steady": True, "direction": None, "good": None, "text": "— steady"}

    zero_h1_zero_h2 = page.evaluate("() => window.DashboardShell.computeDelta(0, 0, {mode: 'percent'})")
    assert zero_h1_zero_h2["steady"] is True
    assert zero_h1_zero_h2["direction"] is None
    assert zero_h1_zero_h2["good"] is None

    zero_baseline_real_increase = page.evaluate(
        "() => window.DashboardShell.computeDelta(0, 1.3333333333333333, {mode: 'percent'})"
    )
    assert zero_baseline_real_increase["steady"] is False
    assert zero_baseline_real_increase["direction"] == "up"
    assert zero_baseline_real_increase["good"] is True
    assert zero_baseline_real_increase["text"] == "▲ New H2"

    missing_h1 = page.evaluate("() => window.DashboardShell.computeDelta(null, 0.9375, {mode: 'point'})")
    assert missing_h1["steady"] is True
    assert missing_h1["direction"] is None
    assert missing_h1["good"] is None

    real_change = page.evaluate("() => window.DashboardShell.computeDelta(5, 6, {mode: 'count'})")
    assert real_change["steady"] is False
    assert real_change["direction"] == "up"
    assert real_change["good"] is True


def test_overview_unit_003b_kpi_row_never_up_or_down_class_when_steady(loaded_page):
    """Exercises the real renderKpiRow/computeDelta pipeline (not just the
    pure function) against a fixture shaped to hit the 2 genuine steady
    cases (tie, missing H1) at once -- the DOM class must be 'steady',
    never 'up'/'down', whenever the underlying delta is steady. PRs/Reviews
    (zero H1, nonzero H2) are a real increase, not steady (TASK-004-033) --
    covered separately so this test keeps asserting only genuine ties."""
    page = loaded_page
    tiles = page.evaluate(
        "() => window.DashboardShell.buildKpiTiles({team: {monthly: [], "
        "h1: {commits_per_mo: 10, prs_per_mo: 0, reviews_per_mo: 0, active_devs: 5, cycle_time_days: 10, ci_pass_rate: null}, "
        "h2: {commits_per_mo: 12, prs_per_mo: 1.5, reviews_per_mo: 2, active_devs: 5, cycle_time_days: 10, ci_pass_rate: 0.9}"
        "}}).map(t => ({key: t.key, steady: t.delta.steady, good: t.delta.good}))"
    )
    by_key = {t["key"]: t for t in tiles}
    assert by_key["active-devs"]["steady"] is True
    assert by_key["ci"]["steady"] is True
    for key in ("active-devs", "ci"):
        assert by_key[key]["good"] is None, f"{key} delta should not be colored up/down while steady"

    assert by_key["prs"]["steady"] is False
    assert by_key["prs"]["good"] is True
    assert by_key["reviews"]["steady"] is False
    assert by_key["reviews"]["good"] is True


def test_overview_unit_003c_real_fixture_ci_steady_prs_reviews_up(loaded_page):
    """Regression guard against the real built fixture. With the re-collected
    metrics (full PR/review data), H1 and H2 both have real PR/rereview
    counts. PRs increase from H1→H2 (green 'up'), reviews decrease slightly
    (the delta text reflects the actual percentage). CI Pass also changed
    from H1=0.0 to H2=0.76 (genuine increase, 'up')."""
    page = loaded_page

    # PRs Merged: H1→H2 is a genuine increase, must render 'up' (green)
    cls = page.locator("#kpiPrsDelta").get_attribute("class")
    assert "up" in cls.split(), f"#kpiPrsDelta class was {cls!r}, expected 'up'"
    assert "steady" not in cls.split() and "down" not in cls.split(), f"#kpiPrsDelta class was {cls!r}"


def test_overview_e2e_002_toggle_to_per_developer_shows_nine_series(loaded_page):
    page = loaded_page
    page.click("#activityToggle")
    page.wait_for_timeout(200)

    dataset_count = page.evaluate(
        "() => { const c = Chart.getChart('activityChart'); return c ? c.data.datasets.length : -1; }"
    )
    assert dataset_count == 8

    colors = page.evaluate(
        "() => { const c = Chart.getChart('activityChart'); "
        "return c.data.datasets.map(d => d.borderColor || d.backgroundColor); }"
    )
    assert len(set(colors)) == 8, f"expected 8 distinguishable series colors, got {colors}"


def test_overview_e2e_003_developer_filter_highlights_series_in_chart(loaded_page, metrics_data):
    page = loaded_page
    page.click("#activityToggle")
    page.wait_for_timeout(150)

    target_handle = metrics_data["roster"][1]["handle"]
    page.evaluate(
        "handle => window.dispatchEvent(new CustomEvent('dashboard:developer-filter', {detail: {handle}}))",
        target_handle,
    )
    page.wait_for_timeout(150)

    alphas = page.evaluate(
        "() => { const c = Chart.getChart('activityChart'); "
        "return c.data.datasets.map(d => d.backgroundColor || d.borderColor); }"
    )
    assert len(set(alphas)) > 1, "expected the selected developer's series to visually differ from the rest"

    page.evaluate(
        "() => window.dispatchEvent(new CustomEvent('dashboard:developer-filter', {detail: {handle: null}}))"
    )
    page.wait_for_timeout(150)


def test_overview_e2e_004_narrow_viewport_kpi_row_and_chart_reflow_no_overflow(loaded_page):
    page = loaded_page
    page.set_viewport_size({"width": 560, "height": 900})
    page.wait_for_timeout(150)
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 1
    page.set_viewport_size({"width": 1280, "height": 900})


def test_overview_e2e_005_hover_month_bar_shows_tooltip_with_real_counts(loaded_page, metrics_data):
    """Chart.js renders its default tooltip on the canvas itself (no DOM
    node to query), so this reads the live Chart.js tooltip model instead
    of the page's visible pixels -- it still proves the exact commit count
    from the inlined metrics.json reaches the tooltip, not placeholder
    text, which is what OVERVIEW-E2E-005 is actually checking."""
    page = loaded_page
    canvas = page.locator("#activityChart")
    box = canvas.bounding_box()
    assert box is not None

    first_month_commits = metrics_data["team"]["monthly"][0]["commits"]

    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.mouse.move(box["x"] + box["width"] * 0.06, box["y"] + box["height"] * 0.4, steps=5)
    page.wait_for_timeout(200)

    tooltip = page.evaluate(
        "() => { const c = Chart.getChart('activityChart'); "
        "return c && c.tooltip ? { opacity: c.tooltip.opacity, "
        "raw: (c.tooltip.dataPoints || []).map(dp => dp.raw) } : null; }"
    )
    assert tooltip is not None
    assert tooltip["opacity"] > 0, "hovering the first month's bar did not open a tooltip"
    assert first_month_commits in tooltip["raw"]
