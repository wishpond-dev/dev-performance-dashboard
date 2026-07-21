"""METRIC-INT-002 -- cross-module integration test for test-plan-metrics.md's
"Cross-module integration" category: a fixture containing one fully-zero
repo and one fully-active repo, run through the real
bucketing -> scoring -> dora -> persist pipeline (persist.collect_and_assemble,
exactly what collect.run_collect calls), must produce correct per-repo rows
for both repos and a team-total rollup that isn't skewed by the zero repo.

Hand-written JSON fixtures matching git_source.py's/github_source.py's
documented raw-source shape, per test-plan-metrics.md GAP-3 (same convention
as test_persist.py's METRIC-PERSIST tests) -- no real git/GitHub collection
needed since this area treats that output as a given input.
"""

from __future__ import annotations

from collector import persist
from collector.git_source import default_window_months
from collector.identity import EXPECTED_ROSTER, IdentityMap, Person

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

ROSTER_9 = [Person(name, handle) for name, handle in EXPECTED_ROSTER]
HANDLES = [p.handle for p in ROSTER_9]
MONTHS = default_window_months()

ACTIVE_MONTH = "2025-07"
QUIET_MONTH = "2025-08"  # a control month where even repo-active has no activity

# The only two developers with any activity anywhere in this fixture --
# everyone else, and every other month, stays a zero row.
DEV_A = "mc4future"  # 3 PRs, faster cycle time, higher ci_pass_rate
DEV_B = "amedwishpond"  # 1 PR, slower cycle time, perfect ci_pass_rate

DEV_A_GIT = dict(commits=12, lines_added=400, lines_removed=100, active_days=6, test_touch_rate=0.5)
DEV_A_GITHUB = dict(
    prs_merged=3, cycle_time_days=2.0, reviews_given=4,
    review_turnaround_hours=5.0, change_request_rate=0.25, ci_pass_rate=0.75,
)
DEV_B_GIT = dict(commits=8, lines_added=200, lines_removed=50, active_days=4, test_touch_rate=0.25)
DEV_B_GITHUB = dict(
    prs_merged=1, cycle_time_days=4.0, reviews_given=2,
    review_turnaround_hours=10.0, change_request_rate=0.0, ci_pass_rate=1.0,
)

EXPECTED_TEAM_COMMITS = DEV_A_GIT["commits"] + DEV_B_GIT["commits"]  # 20
EXPECTED_TEAM_PRS = DEV_A_GITHUB["prs_merged"] + DEV_B_GITHUB["prs_merged"]  # 4
EXPECTED_TEAM_REVIEWS = DEV_A_GITHUB["reviews_given"] + DEV_B_GITHUB["reviews_given"]  # 6
EXPECTED_TEAM_CYCLE_TIME = (
    DEV_A_GITHUB["cycle_time_days"] * DEV_A_GITHUB["prs_merged"]
    + DEV_B_GITHUB["cycle_time_days"] * DEV_B_GITHUB["prs_merged"]
) / EXPECTED_TEAM_PRS  # (2.0*3 + 4.0*1) / 4 = 2.5
EXPECTED_TEAM_CI_PASS_RATE = (
    DEV_A_GITHUB["ci_pass_rate"] * DEV_A_GITHUB["prs_merged"]
    + DEV_B_GITHUB["ci_pass_rate"] * DEV_B_GITHUB["prs_merged"]
) / EXPECTED_TEAM_PRS  # (0.75*3 + 1.0*1) / 4 = 0.8125


def _identity_map() -> IdentityMap:
    aliases = {f"{p.handle}@example.com": p.handle for p in ROSTER_9}
    return IdentityMap(
        roster=ROSTER_9,
        aliases=aliases,
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER_9},
        unmapped_reasons={},
    )


def _zero_git_row(month: str, **overrides) -> dict:
    row = {
        "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
        "net": 0, "active_days": 0, "test_touch_rate": 0.0,
    }
    row.update(overrides)
    return row


def _zero_github_row(month: str, **overrides) -> dict:
    row = {
        "month": month, "prs_merged": 0, "cycle_time_days": 0.0, "reviews_given": 0,
        "review_turnaround_hours": 0.0, "change_request_rate": 0.0, "ci_pass_rate": None,
    }
    row.update(overrides)
    return row


def _full_matrix(row_builder) -> dict:
    return {handle: {month: row_builder(month) for month in MONTHS} for handle in HANDLES}


def _repo_fixture(name: str, org="test-org", default_branch="main", merge_strategy="merge") -> dict:
    return {
        "name": name, "org": org, "full_name": f"{org}/{name}",
        "local_path": f"/nonexistent/{name}", "default_branch": default_branch,
        "merge_strategy": merge_strategy,
    }


def _build_fixture_document():
    repos = [_repo_fixture("repo-zero"), _repo_fixture("repo-active")]

    git_metrics_by_repo = {
        "repo-zero": _full_matrix(_zero_git_row),
        "repo-active": _full_matrix(_zero_git_row),
    }
    git_metrics_by_repo["repo-active"][DEV_A][ACTIVE_MONTH] = _zero_git_row(ACTIVE_MONTH, **DEV_A_GIT)
    git_metrics_by_repo["repo-active"][DEV_B][ACTIVE_MONTH] = _zero_git_row(ACTIVE_MONTH, **DEV_B_GIT)

    github_metrics_by_repo = {
        "repo-zero": _full_matrix(_zero_github_row),
        "repo-active": _full_matrix(_zero_github_row),
    }
    github_metrics_by_repo["repo-active"][DEV_A][ACTIVE_MONTH] = _zero_github_row(ACTIVE_MONTH, **DEV_A_GITHUB)
    github_metrics_by_repo["repo-active"][DEV_B][ACTIVE_MONTH] = _zero_github_row(ACTIVE_MONTH, **DEV_B_GITHUB)

    document, _consolidated = persist.collect_and_assemble(
        repos, _identity_map(), git_metrics_by_repo, github_metrics_by_repo,
        WEIGHTS, window_months=MONTHS, generated_at="2026-07-20T00:00:00Z",
    )
    return document


def test_metric_int_002_zero_repo_appears_with_zeros_not_omitted():
    document = _build_fixture_document()
    breakdown_by_repo = {row["repo"]: row for row in document["repo_breakdown"]}

    assert set(breakdown_by_repo) == {"repo-zero", "repo-active"}  # never dropped
    zero_row = breakdown_by_repo["repo-zero"]
    assert zero_row["commits"] == 0
    assert zero_row["prs"] == 0
    assert zero_row["reviews"] == 0
    assert zero_row["ci_pass_rate"] is None  # null, not a fabricated 0.0
    assert zero_row["active_devs"] == 0


def test_metric_int_002_active_repo_breakdown_matches_hand_computed_totals():
    document = _build_fixture_document()
    breakdown_by_repo = {row["repo"]: row for row in document["repo_breakdown"]}

    active_row = breakdown_by_repo["repo-active"]
    assert active_row["commits"] == EXPECTED_TEAM_COMMITS
    assert active_row["prs"] == EXPECTED_TEAM_PRS
    assert active_row["reviews"] == EXPECTED_TEAM_REVIEWS
    assert active_row["active_devs"] == 2


def test_metric_int_002_team_month_totals_equal_exactly_active_repo_contribution():
    document = _build_fixture_document()
    team_by_month = {row["month"]: row for row in document["team"]["monthly"]}

    active_month_row = team_by_month[ACTIVE_MONTH]
    assert active_month_row["commits"] == EXPECTED_TEAM_COMMITS
    assert active_month_row["prs_merged"] == EXPECTED_TEAM_PRS
    assert active_month_row["reviews"] == EXPECTED_TEAM_REVIEWS
    assert active_month_row["active_devs"] == 2
    assert active_month_row["cycle_time_days"] == EXPECTED_TEAM_CYCLE_TIME
    assert active_month_row["ci_pass_rate"] == EXPECTED_TEAM_CI_PASS_RATE


def test_metric_int_002_quiet_month_is_all_zero_not_contaminated_by_zero_repo():
    document = _build_fixture_document()
    team_by_month = {row["month"]: row for row in document["team"]["monthly"]}

    quiet_row = team_by_month[QUIET_MONTH]
    assert quiet_row["commits"] == 0
    assert quiet_row["prs_merged"] == 0
    assert quiet_row["reviews"] == 0
    assert quiet_row["active_devs"] == 0
    assert quiet_row["cycle_time_days"] == 0.0
    assert quiet_row["ci_pass_rate"] is None


def test_metric_int_002_window_totals_across_all_12_months_unaffected_by_zero_repo():
    """The zero repo contributes 0 to every one of the 12 monthly buckets --
    summing team.monthly across the whole window must equal exactly the
    active repo's totals, proving the zero repo never dilutes or inflates
    the aggregate (the core METRIC-INT-002 acceptance case)."""
    document = _build_fixture_document()

    total_commits = sum(row["commits"] for row in document["team"]["monthly"])
    total_prs = sum(row["prs_merged"] for row in document["team"]["monthly"])
    total_reviews = sum(row["reviews"] for row in document["team"]["monthly"])

    assert total_commits == EXPECTED_TEAM_COMMITS
    assert total_prs == EXPECTED_TEAM_PRS
    assert total_reviews == EXPECTED_TEAM_REVIEWS
