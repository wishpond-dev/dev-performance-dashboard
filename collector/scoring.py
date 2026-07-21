"""Composite scorecard: a transparent, normalized, weighted blend of five
signals (commit volume, PR throughput, review contribution weighted by
turnaround, test-touch rate, CI health) into one composite score per
developer per month, per spec.md S8 / implementation-plan.md S4 step 8.

Consumes bucketing.py's already-consolidated by_developer/by_repo matrices
(build_developer_month_totals / build_repo_developer_month_matrix) and
config/score-weights.json's five weights (validated to sum to 1.0
upstream -- never re-validated or hardcoded here, per spec.md S8: "weights
live in config; changing them + rebuilding re-scores everyone
consistently"). Every signal is normalized within the month across the
roster's 9 developers before weights are applied (spec.md S8: "so the
score reflects relative contribution, not raw volume that penalizes small
months"), and this module never reads a Shortcut-derived input (story
points, bug MTTR) -- those were removed from the formula entirely
(spec.md S8, METRIC-SCORE-008).

`composite` is bucketing.py's reserved `None` placeholder (see
bucketing.py's module docstring): this module's job is to overwrite it, in
place, on every row it scores -- both the by_developer rows the score is
computed from (normalized across the roster, per developer/month -- a
concept that only makes sense rolled up across repos) and the
corresponding by_repo rows for that same developer/month, which get the
identical already-computed value rather than a separate per-repo
renormalization (bucketing.py's "no missing keys, ever" rule means a
by_repo row's composite must never be left as a stale None once this
module has run).
"""

from __future__ import annotations

import logging
from typing import Optional

from collector.git_source import default_window_months
from collector.identity import IdentityMap

logger = logging.getLogger(__name__)

# The five signals, in the fixed order the "signals" breakdown is always
# reported (METRIC-SCORE-004: never a bare aggregate score with no
# breakdown). Deliberately excludes any Shortcut-derived input (story
# points, bug MTTR) -- METRIC-SCORE-008.
SIGNAL_KEYS = ("commits", "prs", "reviews", "tests", "ci")

# Reference point for review-turnaround weighting: a review submitted
# within this many hours keeps most of its raw weight; one that took much
# longer decays toward a small fraction of a full review (spec.md S8:
# "reviews given, weighted by turnaround" -- METRIC-SCORE-006 requires this
# to actually change the signal, not just decorate it).
REVIEW_TURNAROUND_REFERENCE_HOURS = 24.0


class ScoringError(RuntimeError):
    """Raised when scoring.py is given structurally invalid input, e.g.
    weights that don't cover exactly the five expected signal keys."""


def _validate_weights(weights: dict) -> None:
    missing = set(SIGNAL_KEYS) - set(weights)
    extra = set(weights) - set(SIGNAL_KEYS)
    if missing or extra:
        raise ScoringError(
            f"score weights must have exactly the keys {SIGNAL_KEYS}; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def _review_signal_raw(reviews_given: int, review_turnaround_hours: float) -> float:
    """reviews_given weighted by turnaround speed: a review submitted
    quickly counts close to its full weight, one that took a long time
    decays toward a smaller contribution. This is the turnaround
    weighting itself (METRIC-SCORE-006), not a decorative extra factor."""
    if reviews_given <= 0:
        return 0.0
    turnaround_weight = REVIEW_TURNAROUND_REFERENCE_HOURS / (
        REVIEW_TURNAROUND_REFERENCE_HOURS + review_turnaround_hours
    )
    return reviews_given * turnaround_weight


def _raw_signals(row: dict) -> dict:
    """This developer/month row's five raw (un-normalized) signal values.
    `ci_pass_rate` is None for a developer with zero CI checks that month
    -- treated as a 0.0 raw signal (zero activity, not a crash or NaN;
    METRIC-SCORE-005), never a Shortcut-derived input (METRIC-SCORE-008)."""
    return {
        "commits": float(row["commits"]),
        "prs": float(row["prs_merged"]),
        "reviews": _review_signal_raw(row["reviews_given"], row["review_turnaround_hours"]),
        "tests": float(row["test_touch_rate"]),
        "ci": float(row["ci_pass_rate"]) if row["ci_pass_rate"] is not None else 0.0,
    }


def _normalize_min_max(values: dict) -> dict:
    """Min-max normalize one signal's raw values across the roster (the
    dict's keys) to 0..1. All-equal roster values (including all-zero)
    normalize to 0.0 for everyone -- there is no relative contribution to
    reward, and this must never divide by zero (METRIC-SCORE-005)."""
    raw_values = values.values()
    lo, hi = min(raw_values), max(raw_values)
    spread = hi - lo
    if spread == 0:
        return {handle: 0.0 for handle in values}
    return {handle: (value - lo) / spread for handle, value in values.items()}


def normalize_month_signals(month_rows: dict) -> dict:
    """`month_rows` is {handle: row} for every roster developer in one
    month. Returns {handle: {signal_key: normalized_0_to_1}} -- each
    signal normalized independently, within this month, across exactly
    these developers (METRIC-SCORE-002)."""
    raw_by_handle = {handle: _raw_signals(row) for handle, row in month_rows.items()}
    normalized: dict = {handle: {} for handle in month_rows}
    for signal_key in SIGNAL_KEYS:
        raw_for_signal = {handle: raw[signal_key] for handle, raw in raw_by_handle.items()}
        for handle, value in _normalize_min_max(raw_for_signal).items():
            normalized[handle][signal_key] = value
    return normalized


def compute_composite_score(signals: dict, weights: dict) -> float:
    """The weighted blend itself: sum(signal[k] * weight[k] for k in the
    five signals) * 100, so a developer at the top of the roster on every
    signal scores 100 and one with zero relative contribution on every
    signal scores 0 (METRIC-SCORE-001). Changing one weight changes the
    score only by that signal's own (weight_delta * signal_value * 100)
    contribution, holding the other four signals' contributions fixed
    (METRIC-SCORE-003)."""
    _validate_weights(weights)
    return sum(signals[key] * weights[key] for key in SIGNAL_KEYS) * 100.0


def score_month(month_rows: dict, weights: dict) -> dict:
    """`month_rows` is {handle: row} for one month, all 9 roster
    developers. Returns {handle: {"signals": {...5 keys...}, "score": float}}
    -- the full breakdown is always present alongside the aggregate
    (METRIC-SCORE-004)."""
    normalized = normalize_month_signals(month_rows)
    return {
        handle: {"signals": signals, "score": compute_composite_score(signals, weights)}
        for handle, signals in normalized.items()
    }


def score_developer_month_totals(
    developer_month_totals: dict,
    weights: dict,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Score every roster developer for every window month, in place:
    each row in `developer_month_totals[handle][month]` gets its reserved
    `composite` field overwritten with the computed score (a plain
    number, matching bucketing.py's MergedMonthlyMetrics.composite /
    developers[].monthly[].composite shape).

    Normalization always uses exactly `identity_map.roster`'s 9 handles
    for a given month (METRIC-SCORE-002's "across the 9"), not whatever
    keys happen to be present in `developer_month_totals` -- matching the
    `for person in identity_map.roster` convention already used
    throughout bucketing.py/git_source.py/github_source.py.

    Returns {handle: {month: {"signals": ..., "score": ...}}} -- the full
    breakdown, for callers (e.g. score_all below, or persist.py) that need
    the per-signal detail, not just the number written back onto the row.
    """
    months = window_months if window_months is not None else default_window_months()
    handles = [person.handle for person in identity_map.roster]
    breakdowns: dict = {handle: {} for handle in handles}
    for month in months:
        month_rows = {handle: developer_month_totals[handle][month] for handle in handles}
        scored = score_month(month_rows, weights)
        for handle, result in scored.items():
            developer_month_totals[handle][month]["composite"] = result["score"]
            breakdowns[handle][month] = result
    return breakdowns


def apply_scores_to_repo_matrix(
    repo_developer_month_matrix: dict,
    breakdowns: dict,
    window_months: Optional[list] = None,
) -> None:
    """Overwrite the reserved `composite` field on every by_repo row with
    that developer/month's already-computed score (see module docstring:
    composite is a per-developer-per-month concept, normalized across the
    roster -- not renormalized per repo -- so every repo row for the same
    developer/month gets the identical value the by_developer rollup
    produced)."""
    months = window_months if window_months is not None else default_window_months()
    for repo_rows in repo_developer_month_matrix.values():
        for handle, month_rows in repo_rows.items():
            if handle not in breakdowns:
                continue
            for month in months:
                if month in breakdowns[handle]:
                    month_rows[month]["composite"] = breakdowns[handle][month]["score"]


def half_year_months(window_months: Optional[list] = None) -> tuple:
    """(h1_months, h2_months): the first 6 and last 6 months of the
    12-month window (spec.md S10: H1 = Jul-Dec 2025, H2 = Jan-Jun 2026).
    Raises ScoringError if window_months isn't exactly 12 months -- H1/H2
    only make sense for the full annual window."""
    months = window_months if window_months is not None else default_window_months()
    if len(months) != 12:
        raise ScoringError(f"half_year_months requires exactly 12 months, got {len(months)}")
    return tuple(months[:6]), tuple(months[6:])


def score_half_year_rollups(
    breakdowns: dict,
    window_months: Optional[list] = None,
) -> dict:
    """{handle: {"h1": composite_score, "h2": composite_score}} -- each
    half's score is the average of that half's own 6 monthly composite
    scores (already computed by score_developer_month_totals), computed
    independently of the other half and of the full-year average
    (METRIC-SCORE-007)."""
    h1_months, h2_months = half_year_months(window_months)
    rollups: dict = {}
    for handle, monthly in breakdowns.items():
        h1_scores = [monthly[month]["score"] for month in h1_months]
        h2_scores = [monthly[month]["score"] for month in h2_months]
        rollups[handle] = {
            "h1": sum(h1_scores) / len(h1_scores),
            "h2": sum(h2_scores) / len(h2_scores),
        }
    return rollups


def score_all(
    consolidated: dict,
    weights: dict,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Top-level entrypoint: given bucketing.build_consolidated_metrics's
    output ({"by_repo", "by_developer", "team"}) and score-weights.json's
    weights, score every roster developer for every month, overwrite
    `composite` in place on both by_developer and by_repo rows, and return
    the full monthly breakdown plus H1/H2 rollups.

    Mutates `consolidated["by_developer"]` and `consolidated["by_repo"]`
    in place (the reserved composite field bucketing.py left as None);
    `consolidated["team"]` is untouched -- TeamMonthlyMetrics has no
    composite field at all (bucketing.py's data contract has no
    team-level composite score).
    """
    months = window_months if window_months is not None else default_window_months()
    breakdowns = score_developer_month_totals(
        consolidated["by_developer"], weights, identity_map, window_months=months
    )
    apply_scores_to_repo_matrix(consolidated["by_repo"], breakdowns, window_months=months)
    half_year = score_half_year_rollups(breakdowns, window_months=months)
    return {"monthly": breakdowns, "half_year": half_year}


if __name__ == "__main__":
    import json
    import sys

    from collector.bucketing import build_consolidated_metrics
    from collector.git_source import collect_all_git_metrics
    from collector.github_source import collect_all_github_metrics
    from collector.identity import load_identity_map, validate_identity_map

    repos_config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"
    identity_map_path = sys.argv[2] if len(sys.argv) > 2 else "config/identity-map.json"
    weights_path = sys.argv[3] if len(sys.argv) > 3 else "config/score-weights.json"

    with open(repos_config_path, encoding="utf-8") as f:
        repos_list = json.load(f)["repos"]
    with open(weights_path, encoding="utf-8") as f:
        score_weights = json.load(f)

    loaded_map = load_identity_map(identity_map_path)
    validate_identity_map(loaded_map)

    git_metrics = collect_all_git_metrics(repos_list, loaded_map)
    github_metrics = collect_all_github_metrics(repos_list, loaded_map)
    consolidated_metrics = build_consolidated_metrics(repos_list, loaded_map, git_metrics, github_metrics)

    scored = score_all(consolidated_metrics, score_weights, loaded_map)
    latest_month = default_window_months()[-1]
    for handle, monthly in scored["monthly"].items():
        print(
            f"{handle:24s} {latest_month} composite={monthly[latest_month]['score']:.1f}  "
            f"h1={scored['half_year'][handle]['h1']:.1f}  h2={scored['half_year'][handle]['h2']:.1f}"
        )
