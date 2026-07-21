"""IDENT-GIT-005/006 -- month-before-first-commit and zero-commit-repo
complete zero rows. See test-plan-identity.md. (Cross-source "empty month"
bucketing in bucketing.py is a sibling area's test per test-plan-identity.md
GAP-2 -- this file covers only git_source.py's own zero-fill behavior.)
"""

from __future__ import annotations

from collector.git_source import collect_repo_git_metrics
from collector.identity import IdentityMap, Person

WINDOW = ["2025-07", "2025-08", "2025-09"]

ROSTER = [Person("Alejandro Medina", "amedwishpond")]


def _identity_map() -> IdentityMap:
    return IdentityMap(
        roster=ROSTER,
        aliases={"alejandro@example.com": "amedwishpond"},
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER},
        unmapped_reasons={},
    )


_ZERO_ROW_KEYS = {"month", "commits", "lines_added", "lines_removed", "net", "active_days", "test_touch_rate"}


def test_ident_git_005_month_before_first_commit_is_complete_zero_row(git_repo):
    # First-ever commit is in September; July/August must still exist as
    # complete, all-zero rows -- every key present, no crash.
    git_repo.commit(
        message="first ever commit", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-09-10T09:00:00+00:00", files={"a.txt": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    for month in ("2025-07", "2025-08"):
        row = result["amedwishpond"][month]
        assert set(row.keys()) == _ZERO_ROW_KEYS
        assert row == {
            "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
            "net": 0, "active_days": 0, "test_touch_rate": 0.0,
        }
    assert result["amedwishpond"]["2025-09"]["commits"] == 1


def test_ident_git_006_repo_with_zero_commits_in_window_is_all_zero(git_repo):
    # Repo exists and has history, but entirely outside the reporting window.
    git_repo.commit(
        message="way before the window", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2024-01-01T09:00:00+00:00", files={"a.txt": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    for month in WINDOW:
        row = result["amedwishpond"][month]
        assert set(row.keys()) == _ZERO_ROW_KEYS
        assert row == {
            "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
            "net": 0, "active_days": 0, "test_touch_rate": 0.0,
        }
