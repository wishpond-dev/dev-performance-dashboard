"""Dashboard shell & navigation E2E tests (test-plan.md AREA-SCOPED file).

Covers representative smoke + state-traversal IDs: SHELL-E2E-SMOKE-001,
SHELL-E2E-002/003/009/010/012/013.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_shell_e2e_smoke_001_nav_then_narrow_viewport_zero_console_errors(loaded_page):
    page = loaded_page
    page.click('a.sidebar-link[href="#activity"]')
    page.wait_for_timeout(150)
    page.click('a.sidebar-link[href="#scorecards"]')
    page.wait_for_timeout(150)
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(150)
    assert "collapsed" in (page.locator("#sidebar").get_attribute("class") or "")
    assert page.console_errors == []
    assert page.page_errors == []


def test_shell_e2e_002_default_active_section_is_overview(loaded_page):
    page = loaded_page
    active_link = page.locator("a.sidebar-link.active")
    assert active_link.get_attribute("href") == "#overview"
    assert active_link.get_attribute("aria-current") == "true"


def test_shell_e2e_003_clicking_a_nav_link_scrolls_and_highlights_it(loaded_page):
    """`scrollIntoView({behavior: 'smooth'})` (dashboard.js) animates over
    several hundred ms for a section this far down the page -- poll until
    scrollY stops moving instead of a fixed sleep, so this isn't flaky
    under slower CI hosts."""
    page = loaded_page
    page.click('a.sidebar-link[href="#scorecards"]')
    previous_scroll_y = None
    for _ in range(20):
        page.wait_for_timeout(100)
        current_scroll_y = page.evaluate("window.scrollY")
        if current_scroll_y == previous_scroll_y:
            break
        previous_scroll_y = current_scroll_y
    section = page.locator("#scorecards")
    box = section.bounding_box()
    assert box is not None and abs(box["y"]) < 150
    link = page.locator('a.sidebar-link[href="#scorecards"]')
    assert "active" in (link.get_attribute("class") or "")


def test_shell_e2e_009_narrow_viewport_collapses_sidebar_and_recovers(loaded_page):
    page = loaded_page
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(150)
    sidebar = page.locator("#sidebar")
    assert "collapsed" in (sidebar.get_attribute("class") or "")
    box = sidebar.bounding_box()
    assert box is not None and box["width"] <= 61

    page.set_viewport_size({"width": 1280, "height": 900})
    page.wait_for_timeout(150)
    assert "expanded" in (sidebar.get_attribute("class") or "")

    body_scroll_width = page.evaluate("document.documentElement.scrollWidth")
    body_client_width = page.evaluate("document.documentElement.clientWidth")
    assert body_scroll_width <= body_client_width + 1


def test_shell_e2e_010_header_numbers_come_from_real_data(loaded_page, metrics_data):
    page = loaded_page
    assert page.locator("#statRoster").inner_text() == str(len(metrics_data["roster"]))
    assert page.locator("#statRepos").inner_text() == str(len(metrics_data["repos"]))
    assert page.locator("#statMonths").inner_text() == str(len(metrics_data["window"]["months"]))


def test_shell_e2e_012_nav_is_keyboard_operable(loaded_page):
    page = loaded_page
    links = page.locator("a.sidebar-link")
    count = links.count()
    assert count == 8
    for i in range(count):
        links.nth(i).focus()
        assert page.evaluate("document.activeElement.getAttribute('href')") == links.nth(i).get_attribute("href")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    assert page.console_errors == []


def test_shell_unit_002_resolve_active_section_picks_last_once_scrolled_to_bottom(loaded_page):
    """SHELL-UNIT-002 / TASK-004-033: resolveActiveSection's optional 4th
    `maxScrollY` arg must force the LAST section active once scrollY has
    reached it, regardless of the in-range/nearest-by-top geometry -- some
    pages (this one, at the bottom, past 'Per-Repo Breakdown') don't leave
    enough room below their final section(s) for scrollY+offset to ever
    land inside them, so the geometry scan alone would keep resolving to an
    earlier section forever."""
    page = loaded_page
    sections = (
        "[{id: 'a', top: 0, bottom: 100}, {id: 'b', top: 100, bottom: 200}, "
        "{id: 'c', top: 200, bottom: 900}]"
    )
    # scrollY (500) + offset lands inside 'c's own [200, 900) range already --
    # the pre-existing algorithm handles this fine without maxScrollY.
    within_range = page.evaluate(f"() => window.DashboardShell.resolveActiveSection({sections}, 500, 16)")
    assert within_range == "c"

    # scrollY (50) is short of 'c'.top (200) and maxScrollY (50) says the
    # page can't scroll any further -- must still resolve to the LAST
    # section ('c'), not whichever one the offset geometry is nearest to.
    stuck_at_bottom = page.evaluate(f"() => window.DashboardShell.resolveActiveSection({sections}, 50, 16, 50)")
    assert stuck_at_bottom == "c"


def test_shell_e2e_014_clicking_per_repo_breakdown_highlights_it(loaded_page):
    """TASK-004-033 regression: 'Per-Repo Breakdown' is the second-to-last
    sidebar section -- this page doesn't have enough content below it for
    the browser to scroll its top near the viewport top, so it used to
    settle with an earlier ('DORA Metrics') link wrongly highlighted."""
    page = loaded_page
    page.click('a.sidebar-link[href="#repos"]')
    previous_scroll_y = None
    for _ in range(20):
        page.wait_for_timeout(100)
        current_scroll_y = page.evaluate("window.scrollY")
        if current_scroll_y == previous_scroll_y:
            break
        previous_scroll_y = current_scroll_y
    page.wait_for_timeout(300)  # let the click-scroll settle timer clear
    active_links = page.locator("a.sidebar-link.active")
    assert active_links.count() == 1
    assert active_links.first.get_attribute("href") == "#repos"
    assert active_links.first.get_attribute("aria-current") == "true"


def test_shell_e2e_015_clicking_h1_vs_h2_highlights_it(loaded_page):
    """TASK-004-033 regression: 'H1 vs H2' is the last sidebar section, at
    the very bottom of a page that can't scroll any further -- it used to
    settle with an earlier ('DORA Metrics') link wrongly highlighted."""
    page = loaded_page
    page.click('a.sidebar-link[href="#h1h2"]')
    previous_scroll_y = None
    for _ in range(20):
        page.wait_for_timeout(100)
        current_scroll_y = page.evaluate("window.scrollY")
        if current_scroll_y == previous_scroll_y:
            break
        previous_scroll_y = current_scroll_y
    page.wait_for_timeout(300)  # let the click-scroll settle timer clear
    active_links = page.locator("a.sidebar-link.active")
    assert active_links.count() == 1
    assert active_links.first.get_attribute("href") == "#h1h2"
    assert active_links.first.get_attribute("aria-current") == "true"


def test_shell_e2e_013_rapid_resize_across_breakpoint_does_not_break_sidebar(loaded_page):
    page = loaded_page
    widths = [1280, 700, 1280, 700, 1280, 700]
    for w in widths:
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(40)
    page.wait_for_timeout(150)

    sidebar = page.locator("#sidebar")
    class_list = sidebar.get_attribute("class") or ""
    assert ("collapsed" in class_list) != ("expanded" in class_list)
    assert page.console_errors == []
    assert page.page_errors == []
