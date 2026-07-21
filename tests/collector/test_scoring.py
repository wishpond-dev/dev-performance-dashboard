"""METRIC-SCORE-001..008 -- composite scorecard tests for scoring.py. See
test-plan-metrics.md for the exact acceptance-case wording each test below
implements.
"""

from __future__ import annotations

import pytest

from collector.bucketing import build_consolidated_metrics
from collector.identity import IdentityMap, Person
from collector.scoring import (
    SIGNAL_KEYS,
    ScoringError,
    compute_composite_score,
    half_year_months,
    normalize_month_signals,
    score_all,
    score_developer_month_totals,
    score_half_year_rollups,
    score_month,
)

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

ROSTER_8 = [
    Person("Alejandro Medina", "amedwishpond"),
    Person("Amir Pourjabbari", "mc4future"),
    Person("Gabriel Laporte", "gabriellaporte-wp"),
    Person("Igor Negrizoli", "igorFNegrizoli"),
    Person("Paulo Mellin", "pmellingimenes"),
    Person("Umer Boostani", "umerbhattiboostani"),
    Person("David Moradi", "davidmoradi"),
    Person("Marcelo Negrini", "marcelon-salescloser"),
]
HANDLES = [p.handle for p in ROSTER_8]


def _identity_map() -> IdentityMap:
    aliases = {f"{p.handle}@example.com": p.handle for p in ROSTER_8}
    return IdentityMap(
        roster=ROSTER_8,
        aliases=aliases,
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER_8},
        unmapped_reasons={},
    )


def _zero_row(month: str, **overrides) -> dict:
    row = {
        "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0, "net": 0,
        "active_days": 0, "test_touch_rate": 0.0, "prs_merged": 0, "cycle_time_days": 0.0,
        "reviews_given": 0, "review_turnaround_hours": 0.0, "change_request_rate": 0.0,
        "ci_pass_rate": None, "composite": None,
    }
    row.update(overrides)
    return row


def _month_rows_9(month: str, overrides_by_handle: dict) -> dict:
    """{handle: row} for all 8 roster handles in one month; only the
    handles present in overrides_by_handle get non-zero fields."""
    return {
        handle: _zero_row(month, **overrides_by_handle.get(handle, {}))
        for handle in HANDLES
    }


def test_metric_score_001_weighted_blend_matches_hand_computed_value():
    signals = {"commits": 1.0, "prs": 0.5, "reviews": 0.0, "tests": 0.8, "ci": 0.2}
    score = compute_composite_score(signals, WEIGHTS)
    expected = (1.0 * 0.20 + 0.5 * 0.30 + 0.0 * 0.25 + 0.8 * 0.15 + 0.2 * 0.10) * 100.0
    assert score == pytest.approx(expected)
    assert expected == pytest.approx(49.0)


def test_metric_score_002_each_signal_normalized_within_month_across_nine():
    # Top dev in absolute commits but a small month overall; another dev
    # has fewer commits but is still the max for that (smaller) month.
    month_rows = _month_rows_9("2025-07", {
        "amedwishpond": {"commits": 100},
        "mc4future": {"commits": 50},
    })
    normalized = normalize_month_signals(month_rows)

    # Absolute volume alone does not determine relative signal value --
    # the max in this 8-person set (100) normalizes to 1.0, and everyone
    # else is relative to that same min/max, not to some external scale.
    assert normalized["amedwishpond"]["commits"] == pytest.approx(1.0)
    assert normalized["mc4future"]["commits"] == pytest.approx(0.5)
    for handle in HANDLES:
        if handle not in ("amedwishpond", "mc4future"):
            assert normalized[handle]["commits"] == pytest.approx(0.0)


def test_metric_score_003_changing_one_weight_changes_score_deterministically():
    signals = {"commits": 0.4, "prs": 0.6, "reviews": 0.2, "tests": 0.9, "ci": 0.1}
    base_score = compute_composite_score(signals, WEIGHTS)

    reweighted = dict(WEIGHTS, commits=0.30)  # was 0.20
    new_score = compute_composite_score(signals, reweighted)

    delta = new_score - base_score
    expected_delta = (0.30 - 0.20) * signals["commits"] * 100.0
    assert delta == pytest.approx(expected_delta)

    # Every other signal's own contribution is untouched by the reweight.
    for key in ("prs", "reviews", "tests", "ci"):
        assert reweighted[key] == WEIGHTS[key]


def test_metric_score_004_signals_breakdown_always_has_all_five_named_keys():
    month_rows = _month_rows_9("2025-07", {"amedwishpond": {"commits": 10, "prs_merged": 2}})
    scored = score_month(month_rows, WEIGHTS)

    for handle in HANDLES:
        assert set(scored[handle]["signals"].keys()) == set(SIGNAL_KEYS)
        assert "score" in scored[handle]
        assert isinstance(scored[handle]["score"], float)


def test_metric_score_005_zero_activity_developer_gets_defined_value_not_nan():
    # Whole month is zero activity for everyone -- must not divide by zero.
    month_rows = _month_rows_9("2025-07", {})
    scored = score_month(month_rows, WEIGHTS)

    for handle in HANDLES:
        for signal_value in scored[handle]["signals"].values():
            assert signal_value == 0.0
            assert signal_value == signal_value  # NaN check: NaN != NaN
        assert scored[handle]["score"] == 0.0

    # Also: one active developer, rest idle -- the idle ones still get 0.0.
    month_rows_mixed = _month_rows_9("2025-08", {"amedwishpond": {"commits": 5}})
    scored_mixed = score_month(month_rows_mixed, WEIGHTS)
    for handle in HANDLES:
        if handle != "amedwishpond":
            assert scored_mixed[handle]["signals"]["commits"] == 0.0
            assert scored_mixed[handle]["signals"]["commits"] == scored_mixed[handle]["signals"]["commits"]


def test_metric_score_006_equal_review_counts_different_turnaround_differ():
    month_rows = _month_rows_9("2025-07", {
        "amedwishpond": {"reviews_given": 10, "review_turnaround_hours": 1.0},
        "mc4future": {"reviews_given": 10, "review_turnaround_hours": 200.0},
    })
    normalized = normalize_month_signals(month_rows)

    fast_signal = normalized["amedwishpond"]["reviews"]
    slow_signal = normalized["mc4future"]["reviews"]
    assert fast_signal != slow_signal
    assert fast_signal > slow_signal


def test_metric_score_007_h1_h2_rollups_use_only_their_own_six_months():
    identity_map = _identity_map()
    months = [
        "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    ]
    developer_month_totals = {
        handle: {
            month: _zero_row(month, commits=50)
            for month in months
        }
        for handle in HANDLES
    }
    # H1: amedwishpond is the clear top of the roster (10 vs everyone
    # else's 5). H2: mc4future overtakes as the top instead, and
    # amedwishpond's relative position drops to the middle of the pack --
    # a genuinely different normalization basis, not just a rescaled copy
    # of H1's ratios.
    for month in months[:6]:
        developer_month_totals["amedwishpond"][month] = _zero_row(month, commits=10)
        for handle in HANDLES:
            if handle != "amedwishpond":
                developer_month_totals[handle][month] = _zero_row(month, commits=5)
    for month in months[6:]:
        developer_month_totals["amedwishpond"][month] = _zero_row(month, commits=60)
        developer_month_totals["mc4future"][month] = _zero_row(month, commits=100)
        for handle in HANDLES:
            if handle not in ("amedwishpond", "mc4future"):
                developer_month_totals[handle][month] = _zero_row(month, commits=50)

    breakdowns = score_developer_month_totals(developer_month_totals, WEIGHTS, identity_map, window_months=months)
    half_year = score_half_year_rollups(breakdowns, window_months=months)

    h1_months, h2_months = half_year_months(months)
    assert h1_months == tuple(months[:6])
    assert h2_months == tuple(months[6:])

    for handle in HANDLES:
        expected_h1 = sum(breakdowns[handle][m]["score"] for m in h1_months) / 6
        expected_h2 = sum(breakdowns[handle][m]["score"] for m in h2_months) / 6
        assert half_year[handle]["h1"] == pytest.approx(expected_h1)
        assert half_year[handle]["h2"] == pytest.approx(expected_h2)
        # H2 (higher commit volume for everyone) is a different rollup
        # basis than H1 -- not simply the full-year average repeated.
        if handle == "amedwishpond":
            assert half_year[handle]["h1"] != half_year[handle]["h2"]

    with pytest.raises(ScoringError):
        half_year_months(months[:5])


def test_metric_score_008_no_shortcut_derived_signal_anywhere():
    forbidden = {"story_points", "bug_mttr", "mttr", "shortcut"}
    assert set(SIGNAL_KEYS).isdisjoint(forbidden)
    assert set(WEIGHTS.keys()).isdisjoint(forbidden)
    assert set(WEIGHTS.keys()) == set(SIGNAL_KEYS)

    month_rows = _month_rows_9("2025-07", {"amedwishpond": {"commits": 10}})
    scored = score_month(month_rows, WEIGHTS)
    for handle in HANDLES:
        assert set(scored[handle]["signals"].keys()).isdisjoint(forbidden)


def test_scoring_score_all_overwrites_composite_on_both_by_repo_and_by_developer():
    from collector.git_source import default_window_months

    identity_map = _identity_map()
    months = default_window_months()
    repos = [{"name": "repo-a"}]
    git_metrics_by_repo = {
        "repo-a": {"amedwishpond": {"2025-07": {
            "month": "2025-07", "commits": 10, "lines_added": 0, "lines_removed": 0,
            "net": 0, "active_days": 0, "test_touch_rate": 0.0,
        }}},
    }
    github_metrics_by_repo = {}

    consolidated = build_consolidated_metrics(
        repos, identity_map, git_metrics_by_repo, github_metrics_by_repo, window_months=months
    )
    assert consolidated["by_developer"]["amedwishpond"]["2025-07"]["composite"] is None
    assert consolidated["by_repo"]["repo-a"]["amedwishpond"]["2025-07"]["composite"] is None

    result = score_all(consolidated, WEIGHTS, identity_map, window_months=months)

    by_dev_score = consolidated["by_developer"]["amedwishpond"]["2025-07"]["composite"]
    by_repo_score = consolidated["by_repo"]["repo-a"]["amedwishpond"]["2025-07"]["composite"]
    assert by_dev_score is not None
    assert by_repo_score is not None
    assert by_dev_score == pytest.approx(by_repo_score)
    assert by_dev_score == pytest.approx(result["monthly"]["amedwishpond"]["2025-07"]["score"])

    for handle in HANDLES:
        for month in months:
            assert consolidated["by_developer"][handle][month]["composite"] is not None
