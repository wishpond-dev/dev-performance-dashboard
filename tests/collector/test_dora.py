"""METRIC-DORA-001..007 -- DORA proxy tests for dora.py. See
test-plan-metrics.md for the exact acceptance-case wording each test below
implements.
"""

from __future__ import annotations

import pytest

from collector.dora import (
    DoraMetrics,
    compute_dora_metrics,
    compute_monthly_deploy_frequency,
    filter_merged_to_branch,
    is_change_failure,
    lead_time_days,
)

WINDOW = ["2025-07", "2025-08"]


def _pr(created_at, merged_at, title="feature: add export button", base_ref="main", number=1):
    return {
        "number": number,
        "title": title,
        "created_at": created_at,
        "merged_at": merged_at,
        "base": {"ref": base_ref},
    }


def test_metric_dora_001_lead_time_equals_merged_minus_created_for_fixture_pr():
    pr = _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-03T12:00:00Z")
    assert lead_time_days(pr) == pytest.approx(2.5)


def test_metric_dora_001b_lead_time_is_none_when_dates_missing():
    assert lead_time_days(_pr(created_at=None, merged_at="2025-07-03T12:00:00Z")) is None
    assert lead_time_days(_pr(created_at="2025-07-01T00:00:00Z", merged_at=None)) is None


def test_metric_dora_002_revert_title_flagged_as_change_failure():
    assert is_change_failure("revert: bad deploy to prod") is True
    assert is_change_failure("Revert \"feature X\"") is True


def test_metric_dora_003_hotfix_rollback_fix_regression_titles_flagged():
    assert is_change_failure("hotfix: patch payment bug") is True
    assert is_change_failure("rollback broken migration") is True
    assert is_change_failure("fix regression in checkout flow") is True
    # Case-insensitive.
    assert is_change_failure("HOTFIX for outage") is True


def test_metric_dora_003b_fixture_pr_set_hotfix_rollback_fix_regression_flagged_end_to_end():
    # Full PR-dict fixtures (not bare title strings) run through the real
    # aggregation path, mirroring how dora.py sees these PRs in production:
    # compute_dora_metrics() only ever receives raw PR dicts, never titles
    # in isolation.
    prs = [
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-02T00:00:00Z",
            title="hotfix: patch payment bug", number=1),
        _pr(created_at="2025-07-03T00:00:00Z", merged_at="2025-07-04T00:00:00Z",
            title="rollback broken migration", number=2),
        _pr(created_at="2025-07-05T00:00:00Z", merged_at="2025-07-06T00:00:00Z",
            title="fix regression in checkout flow", number=3),
        _pr(created_at="2025-07-07T00:00:00Z", merged_at="2025-07-08T00:00:00Z",
            title="HOTFIX: prod outage", number=4),
        _pr(created_at="2025-08-01T00:00:00Z", merged_at="2025-08-02T00:00:00Z",
            title="feature: add export button", number=5),
        _pr(created_at="2025-08-03T00:00:00Z", merged_at="2025-08-04T00:00:00Z",
            title="fix: typo in README", number=6),
    ]

    # Every hotfix/rollback/fix-regression PR is individually flagged...
    failure_titles = {pr["number"]: is_change_failure(pr["title"]) for pr in prs}
    assert failure_titles == {1: True, 2: True, 3: True, 4: True, 5: False, 6: False}

    # ...and the aggregate change_failure_rate over the whole fixture set
    # reflects exactly those 4 flagged PRs out of 6 total deploys.
    result = compute_dora_metrics(prs, WINDOW)
    assert result["change_failure_rate"] == pytest.approx(4 / 6)


def test_metric_dora_004_normal_titles_not_flagged():
    assert is_change_failure("feature: add export button") is False
    assert is_change_failure("chore: bump dependency versions") is False
    assert is_change_failure("fix: typo in README") is False
    assert is_change_failure(None) is False
    assert is_change_failure("") is False


def test_metric_dora_005_mttr_always_null_regardless_of_inputs():
    assert DoraMetrics().mttr is None
    assert DoraMetrics().to_dict()["mttr"] is None

    empty_result = compute_dora_metrics([], WINDOW)
    assert empty_result["mttr"] is None

    prs = [
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-02T00:00:00Z", title="revert: bad"),
        _pr(created_at="2025-07-05T00:00:00Z", merged_at="2025-07-06T00:00:00Z", number=2),
    ]
    active_result = compute_dora_metrics(prs, WINDOW)
    assert active_result["mttr"] is None
    assert "mttr" in active_result  # never an omitted key


def test_metric_dora_006_deploy_frequency_matches_bucketed_pr_count():
    prs = [
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-05T00:00:00Z", number=1),
        _pr(created_at="2025-07-02T00:00:00Z", merged_at="2025-07-10T00:00:00Z", number=2),
        _pr(created_at="2025-08-01T00:00:00Z", merged_at="2025-08-15T00:00:00Z", number=3),
    ]
    monthly_counts = compute_monthly_deploy_frequency(prs, WINDOW)
    assert monthly_counts == {"2025-07": 2, "2025-08": 1}

    # The bucketed per-month count and the aggregate rate agree: 3 deploys
    # over 2 months == 1.5/month.
    aggregate = compute_dora_metrics(prs, WINDOW)
    assert aggregate["deploy_frequency_per_month"] == pytest.approx(1.5)
    assert sum(monthly_counts.values()) == 3


def test_metric_dora_007_h1_vs_h2_reflect_only_that_halfs_merged_prs():
    h1_months = ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"]
    h2_months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

    prs = [
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-05T00:00:00Z", number=1),
        _pr(created_at="2025-08-01T00:00:00Z", merged_at="2025-08-05T00:00:00Z", number=2, title="hotfix: urgent"),
        _pr(created_at="2026-01-01T00:00:00Z", merged_at="2026-01-05T00:00:00Z", number=3),
    ]

    h1_result = compute_dora_metrics(prs, h1_months)
    h2_result = compute_dora_metrics(prs, h2_months)

    # H1 sees its 2 PRs only -- 2 deploys over 6 months.
    assert h1_result["deploy_frequency_per_month"] == pytest.approx(2 / 6)
    assert h1_result["change_failure_rate"] == pytest.approx(0.5)  # 1 of 2 flagged

    # H2 sees only its own 1 PR -- not the full year's 3.
    assert h2_result["deploy_frequency_per_month"] == pytest.approx(1 / 6)
    assert h2_result["change_failure_rate"] == pytest.approx(0.0)

    assert h1_result != h2_result


def test_filter_merged_to_branch_excludes_non_default_branch_and_out_of_window():
    prs = [
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-05T00:00:00Z", base_ref="main", number=1),
        _pr(created_at="2025-07-01T00:00:00Z", merged_at="2025-07-05T00:00:00Z", base_ref="feature-x", number=2),
        _pr(created_at="2025-09-01T00:00:00Z", merged_at="2025-09-05T00:00:00Z", base_ref="main", number=3),
        {"number": 4, "title": "unmerged", "created_at": "2025-07-01T00:00:00Z", "merged_at": None, "base": {"ref": "main"}},
    ]
    filtered = filter_merged_to_branch(prs, "main", window_months=WINDOW)
    assert [pr["number"] for pr in filtered] == [1]


def test_compute_dora_metrics_zero_deploys_is_defined_not_crash():
    result = compute_dora_metrics([], WINDOW)
    assert result == {
        "deploy_frequency_per_month": 0.0,
        "lead_time_days": 0.0,
        "change_failure_rate": 0.0,
        "mttr": None,
    }
