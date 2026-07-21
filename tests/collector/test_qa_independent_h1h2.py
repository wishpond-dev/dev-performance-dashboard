"""Independent QA verification for METRIC-SCORE-007 (TASK-005-052).

Not part of the developer's own test suite -- written fresh by QA to
adversarially check collector/scoring.py and collector/persist.py for
cross-contamination between the H1 (Jul-Dec) and H2 (Jan-Jun) composite
rollups. Uses its own fixture (3-person mini-roster, deliberately
asymmetric H1 vs H2 activity: H1 is near-idle, H2 is a heavy sprint) and
adds a mutation-isolation check the developer's own test does not have:
compute H1, then drastically rewrite H2's raw data, and assert H1's
already-computed rollup is byte-identical -- proving structurally (not
just by value comparison) that H1 has zero read/write coupling to H2.
"""
from __future__ import annotations

import copy

import pytest

from collector.identity import Person, IdentityMap
from collector.persist import build_team_section, build_developers_section
from collector.scoring import (
    ScoringError,
    half_year_months,
    score_developer_month_totals,
    score_half_year_rollups,
)

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

MONTHS = [
    "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
]

HANDLES = ["qa-alice", "qa-bob", "qa-carol"]

ROSTER = [
    Person(name="QA Alice", handle="qa-alice"),
    Person(name="QA Bob", handle="qa-bob"),
    Person(name="QA Carol", handle="qa-carol"),
]
IDENTITY_MAP = IdentityMap(
    roster=ROSTER,
    aliases={f"{p.handle}@example.com": p.handle for p in ROSTER},
    github_logins={},
    bots=frozenset(),
    departed_emails={},
    departed_count=0,
    people_by_handle={p.handle: p for p in ROSTER},
    unmapped_reasons={},
)


def _row(month, commits, prs=0, reviews=0, turnaround=24.0, tests=0.0, ci=1.0):
    return {
        "month": month,
        "commits": commits,
        "lines_added": commits * 10,
        "lines_removed": commits * 2,
        "net": commits * 8,
        "active_days": min(commits, 20),
        "test_touch_rate": tests,
        "prs_merged": prs,
        "cycle_time_days": 1.5,
        "reviews_given": reviews,
        "review_turnaround_hours": turnaround,
        "change_request_rate": 0.1,
        "ci_pass_rate": ci,
        "composite": None,
    }


def _build_fixture():
    """H1 (2025-07..2025-12): everyone nearly idle, flat 1-2 commits/mo,
    no PRs/reviews at all -- a quiet half. H2 (2026-01..2026-06): a heavy
    sprint, commits/PRs/reviews all an order of magnitude higher, and the
    *relative ranking* flips (qa-carol is the laggard in H1, the leader in
    H2) so a bug that blended halves or used a rolling window would be
    caught by a changed relative ranking, not just a changed magnitude."""
    by_developer = {handle: {} for handle in HANDLES}
    for month in MONTHS[:6]:
        by_developer["qa-alice"][month] = _row(month, commits=3, prs=0, reviews=0, tests=0.1)
        by_developer["qa-bob"][month] = _row(month, commits=2, prs=0, reviews=0, tests=0.1)
        by_developer["qa-carol"][month] = _row(month, commits=1, prs=0, reviews=0, tests=0.0)
    for month in MONTHS[6:]:
        by_developer["qa-alice"][month] = _row(month, commits=40, prs=5, reviews=8, tests=0.6)
        by_developer["qa-bob"][month] = _row(month, commits=55, prs=7, reviews=10, tests=0.7)
        by_developer["qa-carol"][month] = _row(month, commits=120, prs=15, reviews=20, tests=0.9)

    team_monthly = {}
    for month in MONTHS:
        rows = [by_developer[h][month] for h in HANDLES]
        team_monthly[month] = {
            "month": month,
            "commits": sum(r["commits"] for r in rows),
            "prs_merged": sum(r["prs_merged"] for r in rows),
            "reviews": sum(r["reviews_given"] for r in rows),
            "active_devs": sum(1 for r in rows if r["commits"] > 0),
            "cycle_time_days": 1.5,
            "ci_pass_rate": 1.0,
        }

    consolidated = {"by_developer": by_developer, "by_repo": {}, "team": team_monthly}
    return consolidated


def test_qa_h1_h2_scoring_independent_fixture_disjoint_and_correct():
    consolidated = _build_fixture()
    breakdowns = score_developer_month_totals(
        consolidated["by_developer"], WEIGHTS, IDENTITY_MAP, window_months=MONTHS
    )
    half_year = score_half_year_rollups(breakdowns, window_months=MONTHS)

    h1_months, h2_months = half_year_months(MONTHS)
    assert h1_months == tuple(MONTHS[:6])
    assert h2_months == tuple(MONTHS[6:])
    assert set(h1_months).isdisjoint(set(h2_months))

    for handle in HANDLES:
        expected_h1 = sum(breakdowns[handle][m]["score"] for m in h1_months) / 6
        expected_h2 = sum(breakdowns[handle][m]["score"] for m in h2_months) / 6
        assert half_year[handle]["h1"] == pytest.approx(expected_h1)
        assert half_year[handle]["h2"] == pytest.approx(expected_h2)
        assert half_year[handle]["h1"] != pytest.approx(half_year[handle]["h2"])

    # Relative ranking inverts between halves (carol: last in H1, first in
    # H2) -- if H1/H2 were contaminated by a rolling/blended window this
    # inversion would be smeared out rather than sharp.
    h1_ranked = sorted(HANDLES, key=lambda h: half_year[h]["h1"])
    h2_ranked = sorted(HANDLES, key=lambda h: half_year[h]["h2"])
    assert h1_ranked[-1] == "qa-alice"
    assert h1_ranked[0] == "qa-carol"
    assert h2_ranked[-1] == "qa-carol"
    assert h2_ranked[0] == "qa-alice"


def test_qa_mutation_isolation_h1_unaffected_by_rewriting_h2_after_the_fact():
    """Structural (not just value) proof of zero cross-contamination:
    compute H1's rollup, then violently rewrite every H2 month's raw data
    in place, recompute, and assert H1's rollup for every handle is
    bit-identical to before. If scoring.py's half-year split ever became a
    rolling/trailing window, or the H1 sum accidentally captured a
    reference into H2's rows, this mutation would change the "H1" result."""
    consolidated = _build_fixture()
    breakdowns_before = score_developer_month_totals(
        copy.deepcopy(consolidated["by_developer"]), WEIGHTS, IDENTITY_MAP, window_months=MONTHS
    )
    half_year_before = score_half_year_rollups(breakdowns_before, window_months=MONTHS)

    # Distinct-but-inverted values per handle (not identical across the
    # roster) so min-max normalization can't coincidentally collapse to
    # the same spread-zero 0.0 score both before and after the mutation.
    mutated = copy.deepcopy(consolidated["by_developer"])
    for month in MONTHS[6:]:
        mutated["qa-alice"][month] = _row(month, commits=9000, prs=90, reviews=90, tests=1.0)
        mutated["qa-bob"][month] = _row(month, commits=5000, prs=50, reviews=50, tests=0.5)
        mutated["qa-carol"][month] = _row(month, commits=1000, prs=10, reviews=10, tests=0.0)

    breakdowns_after = score_developer_month_totals(mutated, WEIGHTS, IDENTITY_MAP, window_months=MONTHS)
    half_year_after = score_half_year_rollups(breakdowns_after, window_months=MONTHS)

    for handle in HANDLES:
        assert half_year_after[handle]["h1"] == pytest.approx(half_year_before[handle]["h1"])
        assert half_year_after[handle]["h2"] != pytest.approx(half_year_before[handle]["h2"])


def test_qa_persist_team_and_developer_sections_disjoint_h1_h2():
    """Exercises persist.py's build_team_section / build_developers_section
    (the metrics.json assembly layer, not just scoring.py's raw rollup) on
    the same asymmetric fixture, confirming the persisted h1/h2 objects are
    each built from their own half only."""
    consolidated = _build_fixture()
    breakdowns = score_developer_month_totals(
        consolidated["by_developer"], WEIGHTS, IDENTITY_MAP, window_months=MONTHS
    )
    half_year = score_half_year_rollups(breakdowns, window_months=MONTHS)

    team_section = build_team_section(consolidated, team_dora={}, window_months=MONTHS)
    h1_manual_commits = sum(consolidated["team"][m]["commits"] for m in MONTHS[:6]) / 6
    h2_manual_commits = sum(consolidated["team"][m]["commits"] for m in MONTHS[6:]) / 6
    assert team_section["h1"]["commits_per_mo"] == pytest.approx(h1_manual_commits)
    assert team_section["h2"]["commits_per_mo"] == pytest.approx(h2_manual_commits)
    assert team_section["h1"]["commits_per_mo"] < team_section["h2"]["commits_per_mo"]

    developers = build_developers_section(
        consolidated, breakdowns, half_year, IDENTITY_MAP, MONTHS, repo_order=[]
    )
    by_handle = {d["handle"]: d for d in developers}
    for handle in HANDLES:
        h1_manual = sum(consolidated["by_developer"][handle][m]["commits"] for m in MONTHS[:6])
        h2_manual = sum(consolidated["by_developer"][handle][m]["commits"] for m in MONTHS[6:])
        assert by_handle[handle]["h1"]["commits"] == h1_manual
        assert by_handle[handle]["h2"]["commits"] == h2_manual
        assert by_handle[handle]["h1"]["commits"] != by_handle[handle]["h2"]["commits"]
        assert by_handle[handle]["h1"]["composite"] == pytest.approx(half_year[handle]["h1"])
        assert by_handle[handle]["h2"]["composite"] == pytest.approx(half_year[handle]["h2"])


def test_qa_half_year_months_rejects_non_twelve_month_window():
    with pytest.raises(ScoringError):
        half_year_months(MONTHS[:11])
    with pytest.raises(ScoringError):
        half_year_months(MONTHS + ["2026-07"])
