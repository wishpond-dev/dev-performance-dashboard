"""METRIC-INT-003 -- cross-module integration test. See test-plan-metrics.md
for the exact acceptance-case wording this implements: a fixture spanning
the Dec 2025 / Jan 2026 boundary, run through the full pipeline
(bucketing -> scoring -> dora -> persist, via persist.collect_and_assemble --
the same real production entrypoint METRIC-INT-001/002 exercise), must
produce H1 vs H2 rollups (team and per-developer) that match hand-computed
expected values split exactly on that boundary.

Fixture design: two developers, one repo, one month of activity each --
qa-int-dec is active only in 2025-12 (the LAST month of H1), qa-int-jan is
active only in 2026-01 (the FIRST month of H2). Every other one of the 12
window months is left absent in the raw input on purpose, so
bucketing.py's own zero-fill fallback fills them (same convention as
test_metric_int_001.py's _ACTIVE_MONTHS approach) -- this keeps the
fixture minimal and gives the boundary assertion its sharpest possible
form: if a single month ever leaked across the Dec/Jan seam (into the
wrong half, or smeared as a rolling window), the developer whose only
activity sits on that exact seam would show a nonzero value in the WRONG
half, which these assertions catch exactly.

Only two roster developers are used (not the full 9) -- consistent with
test_metric_int_001.py's precedent and test-plan-metrics.md GAP-3
(hand-written JSON fixtures matching the raw-source shape are sufficient
for these cross-module integration cases; the sibling area's own test
suite covers git/GitHub raw-parsing).
"""

from __future__ import annotations

import pytest

from collector import persist
from collector.git_source import default_window_months
from collector.identity import IdentityMap, Person

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

MONTHS = default_window_months()  # full 12-month window -- required by scoring.py's half_year_months
DEC_2025 = "2025-12"  # last month of H1
JAN_2026 = "2026-01"  # first month of H2

DEV_DEC = Person(name="QA Int Dec Dev", handle="qa-int-dec")
DEV_JAN = Person(name="QA Int Jan Dev", handle="qa-int-jan")
ROSTER = [DEV_DEC, DEV_JAN]

REPO = {
    "name": "repo-x", "org": "test-org", "full_name": "test-org/repo-x",
    "local_path": "/nonexistent/repo-x", "default_branch": "main", "merge_strategy": "merge",
}

DEC_GIT = dict(commits=20, lines_added=600, lines_removed=100, net=500, active_days=10, test_touch_rate=0.5)
DEC_GITHUB = dict(
    prs_merged=4, cycle_time_days=2.0, reviews_given=5,
    review_turnaround_hours=6.0, change_request_rate=0.1, ci_pass_rate=0.9,
)
JAN_GIT = dict(commits=30, lines_added=900, lines_removed=150, net=750, active_days=15, test_touch_rate=0.6)
JAN_GITHUB = dict(
    prs_merged=6, cycle_time_days=1.0, reviews_given=8,
    review_turnaround_hours=3.0, change_request_rate=0.05, ci_pass_rate=0.95,
)

# Hand-computed expected composite scores. With only 2 roster developers,
# min-max normalization across the roster (scoring.py's
# normalize_month_signals) gives the single active developer every
# signal = 1.0 that month and the idle developer 0.0 -- so the active
# developer's composite is exactly sum(WEIGHTS.values()) * 100 == 100.0,
# and the idle developer's is 0.0. Every other one of the 12 months has
# both developers idle (all-zero, equal) so both normalize to 0.0 and
# score 0.0 -- confirmed against scoring.py's compute_composite_score.
_ACTIVE_MONTH_SCORE = sum(WEIGHTS.values()) * 100.0  # == 100.0
EXPECTED_DEV_DEC_H1 = _ACTIVE_MONTH_SCORE / 6  # one scoring month (Dec) out of H1's 6
EXPECTED_DEV_JAN_H2 = _ACTIVE_MONTH_SCORE / 6  # one scoring month (Jan) out of H2's 6


def _git_row(month, **overrides):
    row = {
        "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
        "net": 0, "active_days": 0, "test_touch_rate": 0.0,
    }
    row.update(overrides)
    return row


def _github_row(month, **overrides):
    row = {
        "month": month, "prs_merged": 0, "cycle_time_days": 0.0, "reviews_given": 0,
        "review_turnaround_hours": 0.0, "change_request_rate": 0.0, "ci_pass_rate": None,
    }
    row.update(overrides)
    return row


def _identity_map() -> IdentityMap:
    return IdentityMap(
        roster=ROSTER,
        aliases={f"{p.handle}@example.com": p.handle for p in ROSTER},
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER},
        unmapped_reasons={},
    )


@pytest.fixture(scope="module")
def pipeline_document():
    git_metrics_by_repo = {
        "repo-x": {
            "qa-int-dec": {DEC_2025: _git_row(DEC_2025, **DEC_GIT)},
            "qa-int-jan": {JAN_2026: _git_row(JAN_2026, **JAN_GIT)},
        }
    }
    github_metrics_by_repo = {
        "repo-x": {
            "qa-int-dec": {DEC_2025: _github_row(DEC_2025, **DEC_GITHUB)},
            "qa-int-jan": {JAN_2026: _github_row(JAN_2026, **JAN_GITHUB)},
        }
    }
    document, consolidated = persist.collect_and_assemble(
        [REPO],
        _identity_map(),
        git_metrics_by_repo,
        github_metrics_by_repo,
        WEIGHTS,
        window_months=MONTHS,
        generated_at="2026-07-20T00:00:00Z",
    )
    return document, consolidated


def test_metric_int_003_window_spans_the_dec_jan_boundary(pipeline_document):
    document, _ = pipeline_document
    assert document["window"]["months"][5] == "2025-12"
    assert document["window"]["months"][6] == "2026-01"


def test_metric_int_003_team_h1_h2_split_exactly_on_boundary(pipeline_document):
    document, _ = pipeline_document
    team = document["team"]

    # H1 (Jul-Dec) carries only Dec's activity; H2 (Jan-Jun) carries only
    # Jan's -- hand-computed as an average over each half's 6 months
    # (matching bucketing.py's roll_up_team_month / persist.py's
    # _team_half prs_merged-weighted cycle_time_days/ci_pass_rate).
    assert team["h1"]["commits_per_mo"] == pytest.approx(20 / 6)
    assert team["h1"]["prs_per_mo"] == pytest.approx(4 / 6)
    assert team["h1"]["reviews_per_mo"] == pytest.approx(5 / 6)
    assert team["h1"]["cycle_time_days"] == pytest.approx(2.0)
    assert team["h1"]["ci_pass_rate"] == pytest.approx(0.9)

    assert team["h2"]["commits_per_mo"] == pytest.approx(30 / 6)
    assert team["h2"]["prs_per_mo"] == pytest.approx(6 / 6)
    assert team["h2"]["reviews_per_mo"] == pytest.approx(8 / 6)
    assert team["h2"]["cycle_time_days"] == pytest.approx(1.0)
    assert team["h2"]["ci_pass_rate"] == pytest.approx(0.95)

    # Neither half's numbers collapse into the other's -- the boundary is sharp.
    assert team["h1"] != team["h2"]


def test_metric_int_003_team_monthly_rows_isolate_the_boundary_months(pipeline_document):
    document, _ = pipeline_document
    monthly_by_month = {row["month"]: row for row in document["team"]["monthly"]}

    dec_row = monthly_by_month["2025-12"]
    assert dec_row["commits"] == 20
    assert dec_row["prs_merged"] == 4

    jan_row = monthly_by_month["2026-01"]
    assert jan_row["commits"] == 30
    assert jan_row["prs_merged"] == 6

    # Every other month (including Nov, right before the active Dec
    # month, and Feb, right after the active Jan month) stays fully zero
    # -- proving there's no smearing into neighboring months either.
    for month in MONTHS:
        if month in ("2025-12", "2026-01"):
            continue
        row = monthly_by_month[month]
        assert row["commits"] == 0
        assert row["prs_merged"] == 0
        assert row["ci_pass_rate"] is None


def test_metric_int_003_developer_h1_h2_commits_split_exactly_on_boundary(pipeline_document):
    document, _ = pipeline_document
    by_handle = {d["handle"]: d for d in document["developers"]}

    dec_dev = by_handle["qa-int-dec"]
    assert dec_dev["h1"]["commits"] == 20  # all in H1 (Dec)
    assert dec_dev["h2"]["commits"] == 0   # none in H2

    jan_dev = by_handle["qa-int-jan"]
    assert jan_dev["h1"]["commits"] == 0   # none in H1
    assert jan_dev["h2"]["commits"] == 30  # all in H2 (Jan)


def test_metric_int_003_developer_h1_h2_composite_matches_hand_computed_values(pipeline_document):
    document, _ = pipeline_document
    by_handle = {d["handle"]: d for d in document["developers"]}

    # qa-int-dec: only active in Dec (last of H1's 6 months) -- H1's
    # average composite is exactly 100/6, H2's is exactly 0.
    dec_dev = by_handle["qa-int-dec"]
    assert dec_dev["h1"]["composite"] == pytest.approx(EXPECTED_DEV_DEC_H1)
    assert dec_dev["h2"]["composite"] == pytest.approx(0.0)

    # qa-int-jan: the mirror image, active only in Jan (first of H2's 6 months).
    jan_dev = by_handle["qa-int-jan"]
    assert jan_dev["h1"]["composite"] == pytest.approx(0.0)
    assert jan_dev["h2"]["composite"] == pytest.approx(EXPECTED_DEV_JAN_H2)

    # The two developers' rollups are not accidentally identical or swapped.
    assert dec_dev["h1"]["composite"] != pytest.approx(dec_dev["h2"]["composite"])
    assert jan_dev["h1"]["composite"] != pytest.approx(jan_dev["h2"]["composite"])
    assert dec_dev["h1"]["composite"] == pytest.approx(jan_dev["h2"]["composite"])


def test_metric_int_003_mttr_still_null_and_schema_keys_present_at_the_boundary(pipeline_document):
    """Sanity check that the boundary fixture doesn't break the documented
    schema contract (implementation-plan.md S3) elsewhere in the document --
    this is a cross-module integration test, not just a scoring unit test."""
    document, _ = pipeline_document
    assert document["team"]["dora"]["mttr"] is None
    assert set(document["team"].keys()) == {"monthly", "h1", "h2", "dora"}
    for dev in document["developers"]:
        assert set(dev["h1"].keys()) == {"commits", "composite"}
        assert set(dev["h2"].keys()) == {"commits", "composite"}
