"""Independent QA verification for METRIC-DORA-007 (TASK-005-060).

Not part of the developer's own test suite -- written fresh by QA to
adversarially check collector/dora.py's compute_dora_metrics (the
function dora.py's own docstring identifies as where H1-vs-H2 isolation
happens: "METRIC-DORA-007 is just this function called with a 6-month
window_months instead of all 12") for cross-contamination between the H1
(Jul-Dec) and H2 (Jan-Jun) DORA rollups.

Uses its own fixture (deliberately asymmetric H1 vs H2 signal shape: H1
has a low change-failure rate and long lead times, H2 has a high
change-failure rate and short lead times) and adds a mutation-isolation
check the developer's own test-dora.py::test_metric_dora_007_... does not
have: compute H1's rollup from a pooled PR list, then append/rewrite a
pile of extra H2-only PRs into that same pooled list, and assert H1's
already-computed rollup is byte-identical -- proving structurally (not
just by value comparison) that H1 has zero coupling to H2's PRs.
"""
from __future__ import annotations

import copy

import pytest

from collector.dora import compute_dora_metrics

H1_MONTHS = ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"]
H2_MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def _pr(created_at, merged_at, title="feature: add export button", number=1):
    return {
        "number": number,
        "title": title,
        "created_at": created_at,
        "merged_at": merged_at,
        "base": {"ref": "main"},
    }


def _build_fixture():
    """H1: 4 PRs, only 1 flagged as a change failure (hotfix), average
    lead time in the multi-day range. H2: 2 PRs, both flagged as change
    failures (revert, rollback), average lead time under a day --
    deliberately inverted signal shape so a bug that blended halves or
    leaked either direction would flip a rate, not just shift a
    magnitude."""
    return [
        # H1 -- 2025-07..2025-12
        _pr("2025-07-01T00:00:00Z", "2025-07-06T00:00:00Z", title="feature: export", number=1),
        _pr("2025-08-01T00:00:00Z", "2025-08-06T00:00:00Z", title="chore: bump deps", number=2),
        _pr("2025-09-01T00:00:00Z", "2025-09-06T00:00:00Z", title="hotfix: urgent prod fix", number=3),
        _pr("2025-12-31T20:00:00Z", "2025-12-31T22:00:00Z", title="feature: last day of H1", number=4),
        # H2 -- 2026-01..2026-06
        _pr("2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z", title="revert: bad deploy", number=5),
        _pr("2026-06-01T00:00:00Z", "2026-06-01T12:00:00Z", title="rollback broken migration", number=6),
    ]


def test_qa_dora_h1_h2_disjoint_and_correct():
    prs = _build_fixture()

    h1_result = compute_dora_metrics(prs, H1_MONTHS)
    h2_result = compute_dora_metrics(prs, H2_MONTHS)

    # H1 sees only its 4 PRs -- not H2's 2.
    assert h1_result["deploy_frequency_per_month"] == pytest.approx(4 / 6)
    assert h1_result["change_failure_rate"] == pytest.approx(1 / 4)  # only PR #3

    # H2 sees only its own 2 PRs -- not the pooled 6.
    assert h2_result["deploy_frequency_per_month"] == pytest.approx(2 / 6)
    assert h2_result["change_failure_rate"] == pytest.approx(1.0)  # both flagged

    # Lead times: H1's long-lead PRs must not be diluted by H2's short
    # ones, and vice versa.
    expected_h1_lead = (5.0 + 5.0 + 5.0 + (2 / 24)) / 4
    assert h1_result["lead_time_days"] == pytest.approx(expected_h1_lead)
    assert h2_result["lead_time_days"] == pytest.approx(0.5)

    assert h1_result != h2_result

    # Full-year pooled result must differ from either half alone -- proves
    # the halves aren't just copies of the full-year rollup.
    full_year_result = compute_dora_metrics(prs, H1_MONTHS + H2_MONTHS)
    assert full_year_result["deploy_frequency_per_month"] == pytest.approx(6 / 12)
    assert full_year_result != h1_result
    assert full_year_result != h2_result


def test_qa_dora_mutation_isolation_h1_unaffected_by_rewriting_h2_prs():
    """Structural proof of zero cross-contamination: compute H1's DORA
    rollup from the pooled PR list, then violently mutate/add a pile of
    H2-only PRs to that same pooled list, recompute, and assert H1's
    rollup is bit-identical to before. If window filtering in
    compute_dora_metrics ever leaked (e.g. used len(prs) instead of
    len(in_window) for the deploy-frequency denominator, or averaged lead
    times/failure flags over unfiltered PRs), this mutation would change
    the "H1" result even though no H1 PR changed."""
    prs_before = _build_fixture()
    h1_before = compute_dora_metrics(prs_before, H1_MONTHS)

    mutated = copy.deepcopy(prs_before)
    for i in range(7, 27):
        month_digit = (i % 6) + 1
        mutated.append(_pr(
            f"2026-0{month_digit}-01T00:00:00Z", f"2026-0{month_digit}-01T01:00:00Z",
            title="revert: mutation storm", number=i,
        ))

    h1_after = compute_dora_metrics(mutated, H1_MONTHS)
    h2_after = compute_dora_metrics(mutated, H2_MONTHS)

    assert h1_after == h1_before
    assert h2_after["deploy_frequency_per_month"] > 3.0  # H2 clearly did change


def test_qa_dora_boundary_prs_land_in_correct_half():
    """A PR merged at 2025-12-31T23:59:59Z (last second of H1) must count
    toward H1 only; a PR merged at 2026-01-01T00:00:01Z (first second of
    H2) must count toward H2 only -- no smearing across the boundary."""
    boundary_prs = [
        _pr("2025-12-31T20:00:00Z", "2025-12-31T23:59:59Z", number=1),
        _pr("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", number=2),
    ]

    h1_result = compute_dora_metrics(boundary_prs, H1_MONTHS)
    h2_result = compute_dora_metrics(boundary_prs, H2_MONTHS)

    assert h1_result["deploy_frequency_per_month"] == pytest.approx(1 / 6)
    assert h2_result["deploy_frequency_per_month"] == pytest.approx(1 / 6)
