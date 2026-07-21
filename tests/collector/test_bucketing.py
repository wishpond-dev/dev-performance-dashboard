"""METRIC-BUCKET-001..007 -- month bucketing and zero-fill/consolidation
tests for bucketing.py. See test-plan-metrics.md and
results/TASK-003-002.json (TASK-003-002's add_tasks[]) for the exact
fixture/assertion wording each test below implements.

METRIC-BUCKET-001/002 exercise UTC month-boundary and non-UTC-offset
fidelity through the merge step using the git_repo fixture (already
UTC-normalized by git_source.py itself -- see git_source.py's own
IDENT-GIT-007 coverage in test_month_bucketing.py) rather than re-parsing
a raw date here, since bucketing.py never touches a raw date itself (see
bucketing.py's module docstring for why).

The remaining tests below (test_bucket_rollup_*, test_bucket_build_*)
cover the developer-across-repos and team-across-developers roll-ups that
TASK-004-007 also assigns to this module, beyond the 7 literal
METRIC-BUCKET-* cases.
"""

from __future__ import annotations

import pytest

from collector.bucketing import (
    BucketingError,
    build_consolidated_metrics,
    build_repo_developer_month_matrix,
    merge_repo_metrics,
    roll_up_developer_month,
    roll_up_team_month,
)
from collector.git_source import collect_repo_git_metrics, default_window_months
from collector.identity import IdentityMap, Person

WINDOW = ["2025-07", "2025-08"]

ROSTER_ONE = [Person("Alejandro Medina", "amedwishpond")]

ROSTER_THREE = [
    Person("Alejandro Medina", "amedwishpond"),
    Person("Amir Pourjabbari", "mc4future"),
    Person("Gabriel Laporte", "gabriellaporte-wp"),
]


def _identity_map(roster=None) -> IdentityMap:
    roster = roster if roster is not None else ROSTER_ONE
    aliases = {f"{p.handle}@example.com": p.handle for p in roster}
    return IdentityMap(
        roster=roster,
        aliases=aliases,
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in roster},
        unmapped_reasons={},
    )


_MERGED_ROW_KEYS = {
    "month", "commits", "pr_commits", "lines_added", "lines_removed", "net", "active_days",
    "test_touch_rate", "prs_merged", "cycle_time_days", "reviews_given",
    "review_turnaround_hours", "change_request_rate", "ci_pass_rate", "composite",
}


def _zero_merged_row(month: str) -> dict:
    return {
        "month": month, "commits": 0, "pr_commits": 0, "lines_added": 0, "lines_removed": 0, "net": 0,
        "active_days": 0, "test_touch_rate": 0.0, "prs_merged": 0, "cycle_time_days": 0.0,
        "reviews_given": 0, "review_turnaround_hours": 0.0, "change_request_rate": 0.0,
        "ci_pass_rate": None, "composite": None,
    }


def _merged_row(month: str, **overrides) -> dict:
    row = _zero_merged_row(month)
    row.update(overrides)
    return row


def _git_row(month: str, **overrides) -> dict:
    row = {
        "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
        "net": 0, "active_days": 0, "test_touch_rate": 0.0,
    }
    row.update(overrides)
    return row


def _github_row(month: str, **overrides) -> dict:
    row = {
        "month": month, "prs_merged": 0, "cycle_time_days": 0.0, "reviews_given": 0,
        "review_turnaround_hours": 0.0, "change_request_rate": 0.0, "ci_pass_rate": None,
    }
    row.update(overrides)
    return row


def test_metric_bucket_001_utc_month_boundary_commit_lands_correctly(git_repo):
    git_repo.commit(
        message="end of july utc", author_name="Alejandro Medina",
        author_email="amedwishpond@example.com", date="2025-07-31T23:59:59+00:00",
        files={"a.txt": "x\n"},
    )
    git_repo.commit(
        message="start of august utc", author_name="Alejandro Medina",
        author_email="amedwishpond@example.com", date="2025-08-01T00:00:00+00:00",
        files={"b.txt": "x\n"},
    )
    identity_map = _identity_map()
    git_metrics = collect_repo_git_metrics(str(git_repo.path), identity_map, window_months=WINDOW)

    merged = merge_repo_metrics(git_metrics, {}, identity_map, window_months=WINDOW)

    assert merged["amedwishpond"]["2025-07"]["commits"] == 1
    assert merged["amedwishpond"]["2025-08"]["commits"] == 1


def test_metric_bucket_002_non_utc_offset_normalized_before_bucketing(git_repo):
    # 2025-08-01T01:00:00+14:00 == 2025-07-31T11:00:00Z -- must land in July,
    # not August, once normalized to UTC.
    git_repo.commit(
        message="non-utc offset actually lands in july", author_name="Alejandro Medina",
        author_email="amedwishpond@example.com", date="2025-08-01T01:00:00+14:00",
        files={"a.txt": "x\n"},
    )
    identity_map = _identity_map()
    git_metrics = collect_repo_git_metrics(str(git_repo.path), identity_map, window_months=WINDOW)

    merged = merge_repo_metrics(git_metrics, {}, identity_map, window_months=WINDOW)

    assert merged["amedwishpond"]["2025-07"]["commits"] == 1
    assert merged["amedwishpond"]["2025-08"]["commits"] == 0


WINDOW_003 = ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]


def test_metric_bucket_003_month_before_first_commit_is_complete_zero_row(git_repo):
    git_repo.commit(
        message="first commit of the repo", author_name="Alejandro Medina",
        author_email="amedwishpond@example.com", date="2026-02-01T09:00:00+00:00",
        files={"a.txt": "x\n"},
    )
    identity_map = _identity_map()
    git_metrics = collect_repo_git_metrics(str(git_repo.path), identity_map, window_months=WINDOW_003)

    merged = merge_repo_metrics(git_metrics, {}, identity_map, window_months=WINDOW_003)

    for month in WINDOW_003[:-1]:
        row = merged["amedwishpond"][month]
        assert set(row.keys()) == _MERGED_ROW_KEYS
        assert row == _zero_merged_row(month)
    assert merged["amedwishpond"]["2026-02"]["commits"] == 1


def test_metric_bucket_004_zero_activity_repo_yields_twelve_zero_rows():
    identity_map = _identity_map()
    months = default_window_months()

    merged = merge_repo_metrics({}, {}, identity_map, window_months=months)

    assert len(months) == 12
    for month in months:
        assert merged["amedwishpond"][month] == _zero_merged_row(month)


def test_metric_bucket_005_full_repo_developer_month_matrix_is_complete():
    identity_map = _identity_map(roster=ROSTER_THREE)
    months = default_window_months()
    repos = [{"name": "repo-a"}, {"name": "repo-b"}]

    # Sparse, uneven activity -- only a couple of cells populated in each
    # source dict; the rest of the 2x3x12 matrix must still be complete.
    git_metrics_by_repo = {
        "repo-a": {"amedwishpond": {"2025-07": _git_row("2025-07", commits=3)}},
    }
    github_metrics_by_repo = {
        "repo-b": {"mc4future": {"2025-12": _github_row("2025-12", prs_merged=2)}},
    }

    matrix = build_repo_developer_month_matrix(
        git_metrics_by_repo, github_metrics_by_repo, repos, identity_map, window_months=months
    )

    assert set(matrix.keys()) == {"repo-a", "repo-b"}
    total_cells = 0
    for repo_name in ("repo-a", "repo-b"):
        assert set(matrix[repo_name].keys()) == {"amedwishpond", "mc4future", "gabriellaporte-wp"}
        for handle in matrix[repo_name]:
            assert set(matrix[repo_name][handle].keys()) == set(months)
            total_cells += len(matrix[repo_name][handle])
    assert total_cells == 2 * 3 * 12 == 72
    assert matrix["repo-a"]["amedwishpond"]["2025-07"]["commits"] == 3
    assert matrix["repo-b"]["mc4future"]["2025-12"]["prs_merged"] == 2
    # An untouched cell is still a complete zero row, not missing.
    assert matrix["repo-a"]["gabriellaporte-wp"]["2026-06"] == _zero_merged_row("2026-06")


def test_metric_bucket_006_zero_activity_row_has_every_documented_field():
    identity_map = _identity_map()
    merged = merge_repo_metrics({}, {}, identity_map, window_months=WINDOW)

    row = merged["amedwishpond"]["2025-07"]
    assert set(row.keys()) == _MERGED_ROW_KEYS
    assert row == _zero_merged_row("2025-07")
    # ci_pass_rate and composite are the two documented null-sentinel
    # fields -- must be None, never a fabricated 0.
    assert row["ci_pass_rate"] is None
    assert row["composite"] is None


WINDOW_007 = default_window_months()


def test_metric_bucket_007_partial_window_repo_creation_splits_zero_and_real(git_repo):
    git_repo.commit(
        message="repo created partway through the window", author_name="Alejandro Medina",
        author_email="amedwishpond@example.com", date="2026-04-15T12:00:00+00:00",
        files={"a.txt": "x\n"},
    )
    identity_map = _identity_map()
    git_metrics = collect_repo_git_metrics(str(git_repo.path), identity_map, window_months=WINDOW_007)

    merged = merge_repo_metrics(git_metrics, {}, identity_map, window_months=WINDOW_007)

    idx = WINDOW_007.index("2026-04")
    for month in WINDOW_007[:idx]:
        assert merged["amedwishpond"][month] == _zero_merged_row(month)
    assert merged["amedwishpond"]["2026-04"]["commits"] == 1
    for month in WINDOW_007[idx + 1:]:
        # No crash past the creation month either -- no more real commits,
        # so back to a complete zero row.
        assert merged["amedwishpond"][month]["commits"] == 0


def test_bucket_rollup_developer_month_sums_counts_across_repos():
    rows = [
        _merged_row("2025-07", commits=4, lines_added=100, lines_removed=20, prs_merged=1, ci_pass_rate=1.0),
        _merged_row("2025-07", commits=2, lines_added=30, lines_removed=10, prs_merged=3, ci_pass_rate=0.5),
    ]

    total = roll_up_developer_month(rows)

    assert total["commits"] == 6
    assert total["lines_added"] == 130
    assert total["lines_removed"] == 30
    assert total["net"] == 100
    assert total["prs_merged"] == 4
    # ci_pass_rate is a prs_merged-weighted average: (1.0*1 + 0.5*3) / 4 = 0.625
    assert total["ci_pass_rate"] == pytest.approx(0.625)


def test_bucket_ci_pass_rate_none_not_coerced_to_zero_in_rollup():
    rows = [
        _merged_row("2025-07", prs_merged=2, ci_pass_rate=None),
        _merged_row("2025-07", prs_merged=3, ci_pass_rate=None),
    ]

    total = roll_up_developer_month(rows)

    assert total["ci_pass_rate"] is None


def test_bucket_rollup_developer_month_raises_on_empty_rows():
    with pytest.raises(BucketingError):
        roll_up_developer_month([])


def test_bucket_rollup_team_month_active_devs_counts_only_active_developers():
    dev_rows = [
        _merged_row("2025-07", commits=5, prs_merged=1, reviews_given=2, ci_pass_rate=1.0),
        _merged_row("2025-07", commits=0, prs_merged=0, reviews_given=0, ci_pass_rate=None),
        _merged_row("2025-07", commits=0, prs_merged=2, reviews_given=0, ci_pass_rate=0.8),
    ]

    team = roll_up_team_month(dev_rows)

    assert team["commits"] == 5
    assert team["prs_merged"] == 3
    assert team["reviews"] == 2
    # dev-1 active via commits, dev-3 active via prs_merged, dev-2 fully idle.
    assert team["active_devs"] == 2
    assert team["ci_pass_rate"] == pytest.approx((1.0 * 1 + 0.8 * 2) / 3)


def test_bucket_rollup_team_month_raises_on_empty_rows():
    with pytest.raises(BucketingError):
        roll_up_team_month([])


def test_bucket_build_consolidated_metrics_wires_all_three_views_together():
    identity_map = _identity_map(roster=ROSTER_THREE)
    months = ["2025-07", "2025-08"]
    repos = [{"name": "repo-a"}, {"name": "repo-b"}]
    git_metrics_by_repo = {
        "repo-a": {"amedwishpond": {"2025-07": _git_row("2025-07", commits=5)}},
        "repo-b": {"amedwishpond": {"2025-07": _git_row("2025-07", commits=3)}},
    }
    github_metrics_by_repo = {}

    consolidated = build_consolidated_metrics(
        repos, identity_map, git_metrics_by_repo, github_metrics_by_repo, window_months=months
    )

    assert set(consolidated.keys()) == {"by_repo", "by_developer", "team"}
    assert consolidated["by_repo"]["repo-a"]["amedwishpond"]["2025-07"]["commits"] == 5
    assert consolidated["by_developer"]["amedwishpond"]["2025-07"]["commits"] == 8
    assert consolidated["team"]["2025-07"]["commits"] == 8
    assert consolidated["team"]["2025-07"]["active_devs"] == 1
