"""Per-Repo Breakdown, H1-vs-H2 & Developer Filter E2E tests (test-plan.md
"Per-Repo Breakdown, H1-vs-H2 & Developer Filter" area). Covers
representative smoke + state-traversal IDs: REPOFILTER-E2E-001/002/003/
004/005/006.

TASK-004-017 fix: `data/metrics.json`'s `developers[].active_repos` is now
emitted by `collector/persist.py` (`_developer_active_repos`, derived from
`by_repo`'s per-developer commit/PR/review activity) -- previously absent,
which left every developer-filter selection dimming all 8 repo rows and
selecting none (TASK-004-016's real-build finding #2). The tests below
against `loaded_page` still only assert the structural invariant (no repo
ever dropped, `selected + dimmed == 8`, scope label updates, clears fully)
since that held even before the fix; `test_repofilter_e2e_003` additionally
now asserts the selected count matches the real `active_repos` list length,
which was impossible to assert meaningfully before this fix. A separate
fixture-driven test at the bottom locks in the highlight LOGIC itself
(selected == that developer's active_repos, dimmed == the rest) against a
synthetic `metrics.json`, independent of whatever the real collector
output happens to contain.
"""
from __future__ import annotations

import importlib.util
import json
import math

import pytest

pytestmark = pytest.mark.e2e


def test_repofilter_e2e_001_golden_path_read_filter_clear_zero_console_errors(loaded_page):
    page = loaded_page
    assert page.locator(".repo-row").count() == 8
    assert page.locator("#h1h2Grid > *").count() >= 4

    handle = page.locator(".scorecard").first.get_attribute("data-handle")
    page.locator(".scorecard").first.click()
    page.wait_for_timeout(150)
    assert page.locator(".repo-row").count() == 8  # never drops a repo while filtered

    page.locator(".scorecard").first.click()  # clear
    page.wait_for_timeout(150)
    assert page.console_errors == []
    assert page.page_errors == []
    assert handle


def test_repofilter_e2e_002_default_state_is_team_totals_no_highlight(loaded_page):
    page = loaded_page
    assert page.locator("#h1h2Scope").inner_text() == "Team totals"
    assert page.locator(".repo-row.selected, .repo-row.dimmed").count() == 0


def test_repofilter_e2e_003_selecting_a_developer_updates_scope_and_keeps_all_rows(loaded_page, metrics_data):
    page = loaded_page
    dev = metrics_data["developers"][0]
    card = page.locator(f'.scorecard[data-handle="{dev["handle"]}"]')
    card.click()
    page.wait_for_timeout(150)

    assert page.locator("#h1h2Scope").inner_text() == f'Filtered: {dev["name"]}'
    assert page.locator(".repo-row").count() == 8
    # No stale .selected left un-decided: every row is classified one way or the other.
    selected = page.locator(".repo-row.selected").count()
    dimmed = page.locator(".repo-row.dimmed").count()
    assert selected + dimmed == 8
    # TASK-004-017 regression guard: with active_repos now populated by the
    # real collector, the selected count must match it exactly (not 0/8,
    # the pre-fix inert state).
    assert selected == len(dev.get("active_repos") or [])

    card.click()  # clear
    page.wait_for_timeout(150)


def test_repofilter_e2e_004_switching_developers_leaves_no_stale_highlight(loaded_page, metrics_data):
    page = loaded_page
    first, second = metrics_data["developers"][0], metrics_data["developers"][1]

    page.locator(f'.scorecard[data-handle="{first["handle"]}"]').click()
    page.wait_for_timeout(150)
    page.locator(f'.scorecard[data-handle="{second["handle"]}"]').click()
    page.wait_for_timeout(150)

    assert page.locator("#h1h2Scope").inner_text() == f'Filtered: {second["name"]}'
    assert page.locator(".repo-row").count() == 8
    assert page.locator(".repo-row.selected").count() + page.locator(".repo-row.dimmed").count() == 8

    page.locator(f'.scorecard[data-handle="{second["handle"]}"]').click()  # clear
    page.wait_for_timeout(150)
    assert page.locator("#h1h2Scope").inner_text() == "Team totals"


def test_repofilter_e2e_005_any_developer_still_renders_all_8_rows(loaded_page, metrics_data):
    page = loaded_page
    last_dev = metrics_data["developers"][-1]
    page.locator(f'.scorecard[data-handle="{last_dev["handle"]}"]').click()
    page.wait_for_timeout(150)

    assert page.locator(".repo-row").count() == 8

    page.locator(f'.scorecard[data-handle="{last_dev["handle"]}"]').click()  # clear
    page.wait_for_timeout(150)
    assert page.locator("#h1h2Scope").inner_text() == "Team totals"


def test_repofilter_e2e_006_clearing_filter_reverts_both_panels_to_team(loaded_page, metrics_data):
    page = loaded_page
    dev = metrics_data["developers"][0]
    card = page.locator(f'.scorecard[data-handle="{dev["handle"]}"]')

    card.click()
    page.wait_for_timeout(150)
    assert page.locator("#h1h2Scope").inner_text() != "Team totals"

    card.click()
    page.wait_for_timeout(150)
    assert page.locator("#h1h2Scope").inner_text() == "Team totals"
    assert page.locator(".repo-row.selected, .repo-row.dimmed").count() == 0


def test_repofilter_highlight_logic_against_fixture_with_active_repos(site_dir, tmp_path, browser):
    """Fixture-driven (not the real build): proves applyRepoFilterHighlight
    itself is correct -- selected rows == the filtered developer's
    active_repos, dimmed == the complement -- using a synthetic
    metrics.json that populates developers[].active_repos, since the real
    collector output currently never does (see module docstring)."""
    spec = importlib.util.spec_from_file_location("site_build", site_dir / "build.py")
    build_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_module)

    fixture = {
        "generated_at": "2026-07-19T00:00:00Z",
        "window": {"start": "2025-07", "end": "2025-07", "months": ["2025-07"]},
        "roster": [{"name": "Test Dev", "handle": "testdev", "initials": "TD"}],
        "repos": [{"name": "repo-a", "org": "test-org"}, {"name": "repo-b", "org": "test-org"}],
        "score_weights": {"commits": 0.2, "prs": 0.3, "reviews": 0.25, "tests": 0.15, "ci": 0.1},
        "team": {
            "monthly": [{"month": "2025-07", "commits": 1, "prs_merged": 0, "reviews": 0, "active_devs": 1, "cycle_time_days": 0, "ci_pass_rate": None}],
            "h1": {"commits_per_mo": 1, "prs_per_mo": 0, "cycle_time_days": 0, "ci_pass_rate": None},
            "h2": {"commits_per_mo": 1, "prs_per_mo": 0, "cycle_time_days": 0, "ci_pass_rate": None},
            "dora": {"deploy_frequency_per_month": 0, "lead_time_days": 0, "change_failure_rate": 0, "mttr": None},
        },
        "developers": [{
            "name": "Test Dev", "handle": "testdev", "initials": "TD",
            "composite": {"score": 50, "signals": {"commits": 0.5, "prs": 0.5, "reviews": 0.5, "tests": 0.5, "ci": 0.5}},
            "review_share": 1.0,
            "active_repos": ["repo-a"],
            "monthly": [{"month": "2025-07", "commits": 1, "lines_added": 1, "lines_removed": 0, "net": 1, "active_days": 1, "test_touch_rate": 0, "prs_merged": 0, "cycle_time_days": 0, "reviews_given": 0, "review_turnaround_hours": 0, "change_request_rate": 0, "ci_pass_rate": None, "composite": 50}],
            "h1": {"commits": 1, "composite": 50}, "h2": {"commits": 1, "composite": 50},
        }],
        "repo_breakdown": [
            {"repo": "repo-a", "commits": 1, "prs": 0, "reviews": 0, "ci_pass_rate": None, "active_devs": 1},
            {"repo": "repo-b", "commits": 0, "prs": 0, "reviews": 0, "ci_pass_rate": None, "active_devs": 0},
        ],
    }
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps(fixture), encoding="utf-8")
    out = tmp_path / "dist" / "index.html"
    build_module.build(site_dir, metrics_path, out)

    page = browser.new_page()
    page.goto(out.resolve().as_uri())
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    page.locator('.scorecard[data-handle="testdev"]').click()
    page.wait_for_timeout(150)

    assert page.locator('.repo-row[data-repo="repo-a"].selected').count() == 1
    assert page.locator('.repo-row[data-repo="repo-b"].dimmed').count() == 1
    page.close()


# ---- TASK-004-038 regression: H1-vs-H2 key-mismatch (commits_per_mo vs
# commits between team.h1/h2 and developers[].h1/h2) ------------------------
#
# TASK-005-173 (REPOFILTER-E2E-001) reported: filtering by a developer
# blanked all 4 H1-vs-H2 cards to "-- -> --" even though that developer has
# real commit data. TASK-005-185 (REPOFILTER-UX-E-002) reported: even the
# default *unfiltered* team view rendered wrong values (Cycle time H1 =
# exactly 0.0d, CI pass H1 blank), suggesting the same mismatch also
# corrupted the team-level aggregation path, not just the per-developer one.
#
# Both symptoms trace to one root cause: `developers[].h1`/`.h2` only ever
# carry `{commits, composite}` (collector/persist.py's `_developer_half`),
# while `team.h1`/`.h2` carry the full `{commits_per_mo, prs_per_mo,
# cycle_time_days, ci_pass_rate}` shape -- a card builder that reads one
# object's key names off the other blanks out. The tests below pin the fix
# at two levels: the pure `buildH1H2Cards` logic (fast, deterministic,
# independent of whatever the real collector output happens to contain) and
# the real built dashboard for both the unfiltered and filtered states.

def _fmt_h1h2(value, kind):
    """Mirrors dashboard.js's formatH1H2Value for building expected strings.
    Uses math.floor(x + 0.5) to match JavaScript's Math.round() behavior
    (rounds .5 up), unlike Python's built-in round() (banker's rounding)."""
    if value is None:
        return "—"
    if kind == "days":
        return f"{value:.1f}d"
    if kind == "point":
        return f"{math.floor(value * 100 + 0.5)}%"
    return f"{math.floor(value + 0.5):,}"


def test_repofilter_buildh1h2cards_team_view_reads_h1h2key_not_devkey(loaded_page):
    """TASK-005-185 regression: with no teamH1/teamH2 args (the unfiltered
    team view), buildH1H2Cards must read team.h1/h2's own field names
    (commits_per_mo, prs_per_mo, cycle_time_days, ci_pass_rate) -- not the
    per-developer devKey names (commits) -- so a real nonzero H1 doesn't
    render as a bogus 0.0/blank."""
    page = loaded_page
    result = page.evaluate(
        "() => window.DashboardShell.buildH1H2Cards("
        "{commits_per_mo: 105, prs_per_mo: 0, cycle_time_days: 2.3, ci_pass_rate: null},"
        "{commits_per_mo: 126, prs_per_mo: 1, cycle_time_days: 28.9, ci_pass_rate: 0.94})"
    )
    by_key = {c["key"]: c for c in result}
    assert by_key["commits"]["text"] == "105 → 126"
    assert by_key["prs"]["text"] == "0 → 1"
    assert by_key["cycle"]["text"] == "2.3d → 28.9d"
    assert by_key["ci"]["text"] == "— → 94%"
    for card in result:
        assert not card["label"].endswith("(team)")


def test_repofilter_buildh1h2cards_dev_scoped_fallback_never_blank(loaded_page):
    """TASK-005-173 regression: a developer h1/h2 object that only carries
    {commits, composite} (the real collector/persist.py shape) must not
    blank the other 3 cards to "-- -> --" -- they must fall back to the
    team h1/h2 figures via def.h1h2Key, labeled "(team)"."""
    page = loaded_page
    result = page.evaluate(
        "() => window.DashboardShell.buildH1H2Cards("
        "{commits: 10, composite: 50}, {commits: 20, composite: 60},"
        "{commits_per_mo: 999, prs_per_mo: 2, cycle_time_days: 1.5, ci_pass_rate: 0.9},"
        "{commits_per_mo: 999, prs_per_mo: 4, cycle_time_days: 1.0, ci_pass_rate: 0.95})"
    )
    by_key = {c["key"]: c for c in result}
    assert by_key["commits"]["text"] == "10 → 20"  # dev-scoped, not team's 999
    assert by_key["prs"]["text"] == "2 → 4"
    assert by_key["cycle"]["text"] == "1.5d → 1.0d"
    assert by_key["ci"]["text"] == "90% → 95%"
    for card in result:
        assert "— → —" not in card["text"], f"{card['key']} card fully blank: {card['text']!r}"
    assert not by_key["commits"]["label"].endswith("(team)")
    for key in ("prs", "cycle", "ci"):
        assert by_key[key]["label"].endswith("(team)")


def test_repofilter_e2e_unfiltered_h1h2_cards_match_real_team_fixture(loaded_page, metrics_data):
    """TASK-005-185 regression against the real built dashboard: the
    default/unfiltered H1-vs-H2 cards must render team.h1/h2's actual
    values, not a blank or a stray 0.0 from a wrong key lookup."""
    page = loaded_page
    team_h1, team_h2 = metrics_data["team"]["h1"], metrics_data["team"]["h2"]
    expected = {
        "Commits / mo": ("commits_per_mo", "count"),
        "PRs / mo": ("prs_per_mo", "count"),
        "Cycle time": ("cycle_time_days", "days"),
        "CI pass": ("ci_pass_rate", "point"),
    }
    labels = page.locator("#h1h2Grid .h1h2-lab")
    values = page.locator("#h1h2Grid .h1h2-val")
    assert labels.count() == 4
    for i in range(4):
        label = labels.nth(i).inner_text()
        field, kind = expected[label]
        want = f"{_fmt_h1h2(team_h1[field], kind)} → {_fmt_h1h2(team_h2[field], kind)}"
        assert values.nth(i).inner_text() == want


def test_repofilter_e2e_filtered_h1h2_cards_show_dev_commits_not_blank(loaded_page, metrics_data):
    """TASK-005-173 regression against the real built dashboard: selecting
    a developer must not blank all 4 H1-vs-H2 cards. Commits reads the
    developer's own h1/h2.commits; the other 3 fall back to team totals,
    labeled '(team)'."""
    page = loaded_page
    dev = metrics_data["developers"][0]
    card = page.locator(f'.scorecard[data-handle="{dev["handle"]}"]')
    card.click()
    page.wait_for_timeout(200)

    labels = page.locator("#h1h2Grid .h1h2-lab")
    values = page.locator("#h1h2Grid .h1h2-val")
    assert labels.count() == 4

    commits_text = None
    for i in range(4):
        label = labels.nth(i).inner_text()
        value_text = values.nth(i).inner_text()
        assert "— → —" not in value_text, f"{label} fully blank while filtered: {value_text!r}"
        if label.startswith("Commits"):
            commits_text = value_text
        else:
            assert label.endswith("(team)"), f"non-commits card should read '(team)': {label!r}"

    expected_commits = (
        f'{_fmt_h1h2(dev["h1"]["commits"], "count")} → {_fmt_h1h2(dev["h2"]["commits"], "count")}'
    )
    assert commits_text == expected_commits

    card.click()  # clear filter
    page.wait_for_timeout(150)
