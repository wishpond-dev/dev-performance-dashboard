"""Month-bucket consolidation: merges git_source.py's and github_source.py's
output into one complete UTC-month x repo x developer record, then rolls
those records up to per-developer-per-month totals (across all 8 repos)
and team-per-month totals (across all developers and repos).

spec.md S9/S10 and implementation-plan.md S3/S4 step 7 assign this module
the job of sitting between the two per-source collectors and the
scoring/persistence layer: it is purely a consolidation stage, a pure
function of git_source.py's and github_source.py's already-computed,
already-UTC-bucketed {repo: {handle: {month: ...}}} dicts. It never talks
to git or the GitHub API, and never re-parses a raw date -- both source
modules already normalize author/merged/submitted dates to UTC before
bucketing (git_source.py's astimezone(timezone.utc) call, github_source.py's
_parse_iso helper), so bucketing.py only ever merges on the "YYYY-MM"
string keys those modules already produced.

Zero-fill (METRIC-BUCKET-003/004/005/007) is inherited for free: both
source modules already scaffold every repo/handle/month combination before
returning (see their own "the full matrix is scaffolded before the walk
runs" docstrings), so this module scaffolds only the repo x developer x
month shape it owns (the *_get_or_zero fallbacks below) rather than
re-deriving completeness from raw data.

Month-boundary fidelity (METRIC-BUCKET-001/002) is exercised end to end in
tests/collector/test_bucketing.py via git_source.collect_repo_git_metrics
plus a synthetic GitHub matrix, rather than re-tested from raw dates here --
that would duplicate git_source.py's own IDENT-GIT-007 coverage
(tests/collector/test_month_bucketing.py) for a case bucketing.py cannot
get wrong on its own, since it never touches a raw date.

`composite` is reserved as an explicit `None` placeholder field on every
merged row (never a fabricated number, matching the mttr/ci_pass_rate
null-sentinel convention already used by git_source.py/github_source.py):
scoring.py (implementation-plan.md S4 step 8) computes and fills it in
downstream, once bucketing.py's rows exist. Reserving the key here --
rather than omitting it -- keeps every row's key set identical across the
whole pipeline (implementation-plan.md S3: "no missing keys, ever") and
satisfies METRIC-BUCKET-006's documented 13-field row shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from collector.git_source import MonthlyGitMetrics, default_window_months
from collector.github_source import MonthlyGitHubMetrics
from collector.identity import IdentityMap

# The git-sourced fields, merged verbatim from MonthlyGitMetrics.to_dict()
# (excluding "month", which is handled separately, and "commits", which is
# the sum of git-sourced non-squash commits + pr_commits — see _merge_row).
_GIT_FIELDS = ("lines_added", "lines_removed", "net", "active_days", "test_touch_rate")

# The GitHub-sourced fields, merged verbatim from MonthlyGitHubMetrics.to_dict()
# (excluding "month").  `pr_commits` is a GitHub-sourced field but is handled
# separately (added to `commits` in _merge_row) so it is NOT in this tuple.
_GITHUB_FIELDS = (
    "prs_merged", "cycle_time_days", "reviews_given",
    "review_turnaround_hours", "change_request_rate", "ci_pass_rate",
)


class BucketingError(RuntimeError):
    """Raised when bucketing.py is given structurally invalid input, e.g. an
    empty row list to roll up."""


@dataclass(frozen=True)
class MergedMonthlyMetrics:
    """One complete repo/developer/month row: the union of git_source.py's
    and github_source.py's fields, plus a reserved (always None here)
    composite slot scoring.py fills in later. Every field always present,
    per implementation-plan.md S3's "no missing keys, ever" rule.

    `pr_commits` is the count of feature-branch commits inside
    squash-merged PRs (fetched from the GitHub /pulls/{n}/commits API).
    These replace the squash commits that git_source.py now excludes from
    its `commits` count. The `commits` field in the merged row is already
    the sum of git-sourced non-squash commits + pr_commits (see _merge_row).
    """

    month: str
    commits: int = 0
    pr_commits: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    net: int = 0
    active_days: int = 0
    test_touch_rate: float = 0.0
    prs_merged: int = 0
    cycle_time_days: float = 0.0
    reviews_given: int = 0
    review_turnaround_hours: float = 0.0
    change_request_rate: float = 0.0
    ci_pass_rate: Optional[float] = None
    composite: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "commits": self.commits,
            "pr_commits": self.pr_commits,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "net": self.net,
            "active_days": self.active_days,
            "test_touch_rate": self.test_touch_rate,
            "prs_merged": self.prs_merged,
            "cycle_time_days": self.cycle_time_days,
            "reviews_given": self.reviews_given,
            "review_turnaround_hours": self.review_turnaround_hours,
            "change_request_rate": self.change_request_rate,
            "ci_pass_rate": self.ci_pass_rate,
            "composite": self.composite,
        }


def _zero_git_row(month: str) -> dict:
    return MonthlyGitMetrics(month=month).to_dict()


def _zero_github_row(month: str) -> dict:
    return MonthlyGitHubMetrics(month=month).to_dict()


def _merge_row(git_row: dict, github_row: dict, month: str) -> dict:
    """Combine one git_source.py row and one github_source.py row (same
    developer, same month) into one MergedMonthlyMetrics dict.

    `commits` = git-sourced non-squash commits + GitHub-sourced pr_commits.
    git_source.py already excludes squash-merge commits (subjects ending in
    '(#NNN)') from its `commits` count; github_source.py fetches the real
    feature-branch commits via /pulls/{n}/commits and stores them as
    `pr_commits`. The sum is the true commit count: real development effort
    for squash-merge repos (not 1-per-PR), and unchanged for merge/direct-push
    repos where pr_commits is always 0.
    """
    pr_commits = github_row.get("pr_commits", 0)
    git_commits = git_row["commits"]
    merged = MergedMonthlyMetrics(
        month=month,
        commits=git_commits + pr_commits,
        pr_commits=pr_commits,
        **{field: git_row[field] for field in _GIT_FIELDS},
        **{field: github_row[field] for field in _GITHUB_FIELDS},
    )
    return merged.to_dict()


def merge_repo_metrics(
    git_metrics: dict,
    github_metrics: dict,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Merge one repo's already-collected git and GitHub matrices.

    `git_metrics`/`github_metrics` are `{handle: {month: row}}`, as
    returned by git_source.collect_repo_git_metrics /
    github_source.collect_repo_github_metrics for a single repo. Returns
    `{handle: {month: merged_row}}`, complete for every roster handle x
    every window month -- a handle or month missing from either input
    (should not happen, since both sources already zero-fill, but handled
    defensively here) falls back to a zero row rather than raising.
    """
    months = window_months if window_months is not None else default_window_months()
    result: dict = {}
    for person in identity_map.roster:
        handle = person.handle
        git_by_month = git_metrics.get(handle, {})
        github_by_month = github_metrics.get(handle, {})
        result[handle] = {}
        for month in months:
            git_row = git_by_month.get(month) or _zero_git_row(month)
            github_row = github_by_month.get(month) or _zero_github_row(month)
            result[handle][month] = _merge_row(git_row, github_row, month)
    return result


def build_repo_developer_month_matrix(
    git_metrics_by_repo: dict,
    github_metrics_by_repo: dict,
    repos: Iterable[dict],
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Merge every repo's git+GitHub matrices into one consolidated view.

    `git_metrics_by_repo`/`github_metrics_by_repo` are
    `{repo_name: {handle: {month: row}}}`, as returned by
    git_source.collect_all_git_metrics / github_source.collect_all_github_metrics.
    `repos` is config/repos.json's `repos` list (each dict needs `name`).

    Returns `{repo_name: {handle: {month: merged_row}}}`, complete for
    every repo x roster handle x window month -- a repo missing from one
    or both source dicts (e.g. a repo whose collection failed entirely)
    still produces a full zero-filled matrix here (METRIC-BUCKET-004),
    never a KeyError or a missing repo entry.
    """
    months = window_months if window_months is not None else default_window_months()
    matrix: dict = {}
    for repo in repos:
        name = repo.get("name") or repo.get("local_path", "")
        git_metrics = git_metrics_by_repo.get(name, {})
        github_metrics = github_metrics_by_repo.get(name, {})
        matrix[name] = merge_repo_metrics(
            git_metrics, github_metrics, identity_map, window_months=months
        )
    return matrix


def _weighted_average(pairs: Iterable[tuple]) -> float:
    """`pairs` is an iterable of (value, weight). Returns the
    weight-weighted average, or 0.0 if every weight is zero (no activity
    to weight by -- matches the "no fabricated non-zero rate" convention
    already used throughout git_source.py/github_source.py)."""
    total_weight = 0.0
    total = 0.0
    for value, weight in pairs:
        total += value * weight
        total_weight += weight
    return (total / total_weight) if total_weight else 0.0


def _average_skip_none(pairs: Iterable[tuple]) -> Optional[float]:
    """`pairs` is an iterable of (value_or_None, weight). Weighted average
    over only the non-None entries; None (not 0.0) if every entry is
    None -- preserves ci_pass_rate's own null-vs-zero distinction
    (github_source.py's _MonthGithubAccumulator.finalize) through the
    roll-up, per METRIC-PERSIST-002/METRIC-BUCKET-006."""
    present = [(value, weight) for value, weight in pairs if value is not None]
    if not present:
        return None
    total_weight = sum(weight for _, weight in present)
    if not total_weight:
        return sum(value for value, _ in present) / len(present)
    return sum(value * weight for value, weight in present) / total_weight


def roll_up_developer_month(rows: list) -> dict:
    """Roll up the per-repo merged rows for one developer and one month
    (e.g. `[matrix[repo][handle][month] for repo in repo_names]`) into one
    per-developer-per-month total across all repos.

    Counts (commits, lines_added/removed, net, active_days, prs_merged,
    reviews_given) sum across repos. Rates (test_touch_rate,
    cycle_time_days, review_turnaround_hours, change_request_rate) are
    weighted averages using the same-row activity count as weight
    (commits for test_touch_rate, prs_merged for cycle_time_days and
    change_request_rate, reviews_given for review_turnaround_hours) so a
    repo with more activity contributes proportionally more to the
    average, rather than every repo counting equally regardless of
    volume. `ci_pass_rate` is a prs_merged-weighted average over only the
    non-None repo rows, staying None if no repo produced a CI signal.
    """
    if not rows:
        raise BucketingError("roll_up_developer_month requires at least one row")
    month = rows[0]["month"]
    lines_added = sum(r["lines_added"] for r in rows)
    lines_removed = sum(r["lines_removed"] for r in rows)
    pr_commits = sum(r.get("pr_commits", 0) for r in rows)
    merged = MergedMonthlyMetrics(
        month=month,
        commits=sum(r["commits"] for r in rows),
        pr_commits=pr_commits,
        lines_added=lines_added,
        lines_removed=lines_removed,
        net=lines_added - lines_removed,
        active_days=sum(r["active_days"] for r in rows),
        test_touch_rate=_weighted_average((r["test_touch_rate"], r["commits"]) for r in rows),
        prs_merged=sum(r["prs_merged"] for r in rows),
        cycle_time_days=_weighted_average((r["cycle_time_days"], r["prs_merged"]) for r in rows),
        reviews_given=sum(r["reviews_given"] for r in rows),
        review_turnaround_hours=_weighted_average(
            (r["review_turnaround_hours"], r["reviews_given"]) for r in rows
        ),
        change_request_rate=_weighted_average((r["change_request_rate"], r["prs_merged"]) for r in rows),
        ci_pass_rate=_average_skip_none((r["ci_pass_rate"], r["prs_merged"]) for r in rows),
    )
    return merged.to_dict()


def build_developer_month_totals(
    repo_developer_month_matrix: dict,
    identity_map: IdentityMap,
    repos: Iterable[dict],
    window_months: Optional[list] = None,
) -> dict:
    """Roll up build_repo_developer_month_matrix's per-repo rows to
    per-developer-per-month totals across all repos. Returns
    `{handle: {month: total_row}}`, complete for every roster handle x
    window month (METRIC-BUCKET-005)."""
    months = window_months if window_months is not None else default_window_months()
    repo_names = [repo.get("name") or repo.get("local_path", "") for repo in repos]
    totals: dict = {}
    for person in identity_map.roster:
        handle = person.handle
        totals[handle] = {}
        for month in months:
            rows = [
                repo_developer_month_matrix.get(name, {}).get(handle, {}).get(month)
                or _merge_row(_zero_git_row(month), _zero_github_row(month), month)
                for name in repo_names
            ]
            totals[handle][month] = roll_up_developer_month(rows)
    return totals


@dataclass(frozen=True)
class TeamMonthlyMetrics:
    """One team-per-month total row, matching implementation-plan.md S3's
    data contract team.monthly[] shape exactly: month, commits,
    prs_merged, reviews, active_devs, cycle_time_days, ci_pass_rate,
    lines_added, lines_removed."""

    month: str
    commits: int = 0
    prs_merged: int = 0
    reviews: int = 0
    active_devs: int = 0
    cycle_time_days: float = 0.0
    ci_pass_rate: Optional[float] = None
    lines_added: int = 0
    lines_removed: int = 0

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "commits": self.commits,
            "prs_merged": self.prs_merged,
            "reviews": self.reviews,
            "active_devs": self.active_devs,
            "cycle_time_days": self.cycle_time_days,
            "ci_pass_rate": self.ci_pass_rate,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }


def roll_up_team_month(developer_rows: list) -> dict:
    """Roll up one month's per-developer totals (e.g.
    `[developer_month_totals[handle][month] for handle in ...]`) into one
    team-per-month total across all developers (and, transitively, all
    repos, since developer_rows are already rolled up across repos).

    `active_devs` counts developers with any commit, PR, or review
    activity that month (git_source.py/github_source.py zero-fill every
    developer regardless of activity, so this cannot be inferred from row
    presence -- it must be derived from the row values themselves).
    """
    if not developer_rows:
        raise BucketingError("roll_up_team_month requires at least one row")
    month = developer_rows[0]["month"]
    team = TeamMonthlyMetrics(
        month=month,
        commits=sum(r["commits"] for r in developer_rows),
        prs_merged=sum(r["prs_merged"] for r in developer_rows),
        reviews=sum(r["reviews_given"] for r in developer_rows),
        active_devs=sum(
            1 for r in developer_rows if r["commits"] or r["prs_merged"] or r["reviews_given"]
        ),
        cycle_time_days=_weighted_average(
            (r["cycle_time_days"], r["prs_merged"]) for r in developer_rows
        ),
        ci_pass_rate=_average_skip_none(
            (r["ci_pass_rate"], r["prs_merged"]) for r in developer_rows
        ),
        lines_added=sum(r.get("lines_added", 0) for r in developer_rows),
        lines_removed=sum(r.get("lines_removed", 0) for r in developer_rows),
    )
    return team.to_dict()


def build_team_month_totals(
    developer_month_totals: dict,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Roll up build_developer_month_totals's per-developer rows to
    team-per-month totals. Returns `{month: team_row}`, complete for every
    window month."""
    months = window_months if window_months is not None else default_window_months()
    team: dict = {}
    for month in months:
        rows = [developer_month_totals[person.handle][month] for person in identity_map.roster]
        team[month] = roll_up_team_month(rows)
    return team


def build_consolidated_metrics(
    repos: Iterable[dict],
    identity_map: IdentityMap,
    git_metrics_by_repo: dict,
    github_metrics_by_repo: dict,
    window_months: Optional[list] = None,
) -> dict:
    """Top-level entrypoint: the three consolidated views bucketing.py
    owns, built from git_source.py's and github_source.py's already
    collected per-repo output (e.g. collect_all_git_metrics /
    collect_all_github_metrics).

    Returns:
      `by_repo`: {repo: {handle: {month: merged row}}}
      `by_developer`: {handle: {month: row rolled up across all repos}}
      `team`: {month: row rolled up across all developers and repos}
    """
    repos = list(repos)
    months = window_months if window_months is not None else default_window_months()
    by_repo = build_repo_developer_month_matrix(
        git_metrics_by_repo, github_metrics_by_repo, repos, identity_map, window_months=months
    )
    by_developer = build_developer_month_totals(by_repo, identity_map, repos, window_months=months)
    team = build_team_month_totals(by_developer, identity_map, window_months=months)
    return {"by_repo": by_repo, "by_developer": by_developer, "team": team}


if __name__ == "__main__":
    import json
    import sys

    from collector.git_source import collect_all_git_metrics
    from collector.github_source import collect_all_github_metrics
    from collector.identity import load_identity_map, validate_identity_map

    repos_config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"
    identity_map_path = sys.argv[2] if len(sys.argv) > 2 else "config/identity-map.json"

    with open(repos_config_path, encoding="utf-8") as f:
        repos_list = json.load(f)["repos"]

    loaded_map = load_identity_map(identity_map_path)
    validate_identity_map(loaded_map)

    git_metrics = collect_all_git_metrics(repos_list, loaded_map)
    github_metrics = collect_all_github_metrics(repos_list, loaded_map)
    consolidated = build_consolidated_metrics(repos_list, loaded_map, git_metrics, github_metrics)

    for month, row in consolidated["team"].items():
        print(
            f"{month}  commits={row['commits']:4d}  prs_merged={row['prs_merged']:3d}  "
            f"active_devs={row['active_devs']}"
        )
