"""METRIC-PERSIST-001..008 -- data-contract/persistence tests for
persist.py. See test-plan-metrics.md for the exact acceptance-case wording
each test below implements. Fixtures are hand-written JSON matching
git_source.py's/github_source.py's documented raw-source shape rather than
real git/GitHub collection, per test-plan-metrics.md GAP-3.
"""

from __future__ import annotations

import json

import pytest

from collector import persist
from collector.git_source import default_window_months
from collector.identity import EXPECTED_ROSTER, IdentityMap, Person

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

ROSTER_9 = [Person(name, handle) for name, handle in EXPECTED_ROSTER]
HANDLES = [p.handle for p in ROSTER_9]
MONTHS = default_window_months()


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


def _repo_fixture(name="repo-a", org="test-org", default_branch="main", merge_strategy="merge") -> dict:
    return {
        "name": name, "org": org, "full_name": f"{org}/{name}",
        "local_path": f"/nonexistent/{name}", "default_branch": default_branch,
        "merge_strategy": merge_strategy,
    }


def _build_document(repos=None, git_metrics_by_repo=None, github_metrics_by_repo=None, weights=None):
    repos = repos if repos is not None else [_repo_fixture()]
    if git_metrics_by_repo is None:
        git_metrics_by_repo = {r["name"]: _full_matrix(_zero_git_row) for r in repos}
    if github_metrics_by_repo is None:
        github_metrics_by_repo = {r["name"]: _full_matrix(_zero_github_row) for r in repos}
    document, _consolidated = persist.collect_and_assemble(
        repos, _identity_map(), git_metrics_by_repo, github_metrics_by_repo,
        weights or WEIGHTS, window_months=MONTHS, generated_at="2026-07-19T00:00:00Z",
    )
    return document


def test_metric_persist_001_roster_identical_order_everywhere():
    document = _build_document()
    assert [p["handle"] for p in document["roster"]] == HANDLES
    assert [p["name"] for p in document["roster"]] == [p.name for p in ROSTER_9]
    assert [d["handle"] for d in document["developers"]] == HANDLES
    assert [d["name"] for d in document["developers"]] == [p.name for p in ROSTER_9]


def test_metric_persist_002_ci_pass_rate_null_distinguishable_from_zero():
    github_metrics = {"repo-a": _full_matrix(_zero_github_row)}
    # amedwishpond: zero check runs all window (stays None). mc4future: checks
    # ran in 2025-07 and all failed (0.0) -- a genuinely different state.
    github_metrics["repo-a"]["mc4future"]["2025-07"] = _zero_github_row("2025-07", ci_pass_rate=0.0, prs_merged=1)
    document = _build_document(github_metrics_by_repo=github_metrics)

    by_handle = {d["handle"]: d for d in document["developers"]}
    assert by_handle["amedwishpond"]["monthly"][0]["ci_pass_rate"] is None
    assert by_handle["mc4future"]["monthly"][0]["ci_pass_rate"] == 0.0
    assert by_handle["mc4future"]["monthly"][0]["ci_pass_rate"] is not None


def test_metric_persist_003_mttr_null_end_to_end_in_serialized_json():
    document = _build_document()
    assert document["team"]["dora"]["mttr"] is None
    assert "mttr" in document["team"]["dora"]

    serialized = json.dumps(document)
    round_tripped = json.loads(serialized)
    assert round_tripped["team"]["dora"]["mttr"] is None
    assert "mttr" in round_tripped["team"]["dora"]


def test_metric_persist_004_required_top_level_keys_present():
    document = _build_document()
    required = {
        "generated_at", "window", "roster", "repos", "score_weights",
        "team", "developers", "repo_breakdown",
    }
    assert required <= set(document.keys())


def test_metric_persist_005_window_months_exactly_12_in_order():
    document = _build_document()
    expected = [
        "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    ]
    assert document["window"]["months"] == expected
    assert document["window"]["start"] == "2025-07"
    assert document["window"]["end"] == "2026-06"


def test_metric_persist_006_raw_json_written_per_repo_per_month(tmp_path):
    repos = [_repo_fixture("repo-a"), _repo_fixture("repo-b")]
    git_metrics = {r["name"]: _full_matrix(_zero_git_row) for r in repos}
    git_metrics["repo-a"]["amedwishpond"]["2025-07"] = _zero_git_row("2025-07", commits=5)
    github_metrics = {r["name"]: _full_matrix(_zero_github_row) for r in repos}

    persist.persist_all(
        repos, _identity_map(), git_metrics, github_metrics, WEIGHTS,
        data_dir=str(tmp_path), window_months=MONTHS, generated_at="2026-07-19T00:00:00Z",
    )

    for repo in repos:
        for month in MONTHS:
            raw_path = tmp_path / "raw" / repo["name"] / f"{month}.json"
            assert raw_path.is_file(), f"missing {raw_path}"
            with open(raw_path, encoding="utf-8") as f:
                payload = json.load(f)  # independently valid JSON
            assert payload["repo"] == repo["name"]
            assert payload["month"] == month
            assert set(payload["developers"].keys()) == set(HANDLES)
            row = payload["developers"]["amedwishpond"]
            assert set(row.keys()) == {
                "month", "commits", "lines_added", "lines_removed", "net", "active_days",
                "test_touch_rate", "prs_merged", "cycle_time_days", "reviews_given",
                "review_turnaround_hours", "change_request_rate", "ci_pass_rate", "composite",
            }
    with open(tmp_path / "raw" / "repo-a" / "2025-07.json", encoding="utf-8") as f:
        july_repo_a = json.load(f)
    assert july_repo_a["developers"]["amedwishpond"]["commits"] == 5


def test_metric_persist_007_markdown_summaries_written(tmp_path):
    repos = [_repo_fixture()]
    persist.persist_all(
        repos, _identity_map(),
        {r["name"]: _full_matrix(_zero_git_row) for r in repos},
        {r["name"]: _full_matrix(_zero_github_row) for r in repos},
        WEIGHTS, data_dir=str(tmp_path), window_months=MONTHS, generated_at="2026-07-19T00:00:00Z",
    )

    team_md = (tmp_path / "summaries" / "team.md").read_text(encoding="utf-8")
    assert "Team Summary" in team_md

    for person in ROSTER_9:
        slug = persist._slugify(person.name)
        dev_md = (tmp_path / "summaries" / f"{slug}.md").read_text(encoding="utf-8")
        assert person.name in dev_md
        assert person.handle in dev_md


def test_metric_persist_008_score_weights_exactly_match_config():
    custom_weights = {"commits": 0.11, "prs": 0.44, "reviews": 0.20, "tests": 0.15, "ci": 0.10}
    document = _build_document(weights=custom_weights)
    assert document["score_weights"] == custom_weights
    assert document["score_weights"] is not custom_weights  # a real copy, not aliasing the input
