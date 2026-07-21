"""Developer scorecards, reviews & DORA panel E2E tests (test-plan.md
"Developer scorecards, reviews & DORA panels" area). Covers representative
smoke + state-traversal IDs: DEVPANEL-E2E-001/002/003/006/007/009.
"""
from __future__ import annotations

import pytest

from constants import BOT_AND_NON_ROSTER_MARKERS

pytestmark = pytest.mark.e2e


def test_devpanel_e2e_001_smoke_browse_filter_reviews_reflect_clear(loaded_page):
    page = loaded_page
    first_card = page.locator(".scorecard").first
    handle = first_card.get_attribute("data-handle")
    assert handle

    first_card.click()
    page.wait_for_timeout(150)
    assert "selected" in (first_card.get_attribute("class") or "")

    matching_review_row = page.locator(f'.review-row[data-handle="{handle}"]')
    assert "selected" in (matching_review_row.get_attribute("class") or "")
    other_rows = page.locator(".review-row:not(.selected)")
    assert other_rows.count() == 7
    for i in range(other_rows.count()):
        assert "dimmed" in (other_rows.nth(i).get_attribute("class") or "")

    first_card.click()  # clear
    page.wait_for_timeout(150)
    assert "selected" not in (first_card.get_attribute("class") or "")
    assert page.locator(".scorecard.selected, .scorecard.dimmed").count() == 0
    assert page.locator(".review-row.selected, .review-row.dimmed").count() == 0
    assert page.console_errors == []


def test_devpanel_e2e_002_click_highlights_consistently_click_again_clears(loaded_page):
    page = loaded_page
    second_card = page.locator(".scorecard").nth(1)
    handle = second_card.get_attribute("data-handle")

    second_card.click()
    page.wait_for_timeout(150)
    assert page.locator(".scorecard.selected").count() == 1
    assert page.locator(f'.scorecard.selected[data-handle="{handle}"]').count() == 1
    assert page.locator(".scorecard.dimmed").count() == 7

    second_card.click()
    page.wait_for_timeout(150)
    assert page.locator(".scorecard.selected, .scorecard.dimmed").count() == 0


def test_devpanel_e2e_003_heading_discloses_composite_formula_and_weights(loaded_page, metrics_data):
    page = loaded_page
    heading_text = page.locator("#scorecardsHeading").inner_text()
    weights = metrics_data["score_weights"]
    assert "composite" in heading_text.lower()
    for key in ("commits", "prs", "reviews", "tests", "ci"):
        weight_str = f"{weights[key]:.2f}"
        assert weight_str in heading_text, f"{key} weight {weight_str!r} missing from scorecards heading: {heading_text!r}"


def test_devpanel_e2e_006_all_nine_review_share_rows_render(loaded_page):
    page = loaded_page
    rows = page.locator(".review-row")
    assert rows.count() == 8
    for i in range(rows.count()):
        pct_text = rows.nth(i).locator(".review-pct").inner_text()
        assert pct_text.endswith("%")


def test_devpanel_e2e_007_dora_mttr_always_na_unaffected_by_filter(loaded_page):
    page = loaded_page
    boxes = page.locator(".dora-box")
    assert boxes.count() == 4
    mttr_box = boxes.filter(has_text="MTTR")
    assert mttr_box.count() == 1
    assert "N/A" in mttr_box.inner_text()
    assert "no incident source" in mttr_box.inner_text().lower()

    page.locator(".scorecard").first.click()
    page.wait_for_timeout(150)
    assert "N/A" in page.locator(".dora-box").filter(has_text="MTTR").inner_text()
    page.locator(".scorecard").first.click()  # clear
    page.wait_for_timeout(150)


def test_devpanel_e2e_009_no_bot_or_nonroster_name_in_scorecards_grid(loaded_page):
    page = loaded_page
    grid_text = page.locator("#scorecardGrid").inner_text()
    for marker in BOT_AND_NON_ROSTER_MARKERS:
        assert marker not in grid_text
