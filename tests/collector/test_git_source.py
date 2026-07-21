"""Unit/integration tests for collector/git_source.py -- see
test-plan-identity.md IDENT-GIT-001, 002, 003, 004, 008, 009. (IDENT-GIT-005
and 006 live in test_empty_month.py; IDENT-GIT-007 lives in
test_month_bucketing.py -- both are also git_source.py's own tests, just
filed under the pre-existing stub names those specific edge cases match.)
"""

from __future__ import annotations

import logging

from collector.git_source import collect_repo_git_metrics
from collector.identity import IdentityMap, Person

WINDOW = ["2025-07", "2025-08"]

ROSTER = [
    Person("Alejandro Medina", "amedwishpond"),
    Person("Amir Pourjabbari", "mc4future"),
]


def _identity_map(**overrides) -> IdentityMap:
    roster = overrides.pop("roster", ROSTER)
    defaults = dict(
        roster=roster,
        aliases={
            "alejandro@example.com": "amedwishpond",
            "alejandro.alt@example.com": "amedwishpond",
            "amir@example.com": "mc4future",
        },
        github_logins={},
        bots=frozenset(["cursor[bot]"]),
        departed_emails={"departed@example.com": "Old Dev"},
        departed_count=1,
        people_by_handle={p.handle: p for p in roster},
        unmapped_reasons={},
    )
    defaults.update(overrides)
    return IdentityMap(**defaults)


# ---------------------------------------------------------------------------
# IDENT-GIT-001: commit count per developer per month matches a hand count
# ---------------------------------------------------------------------------
def test_ident_git_001_commit_count_matches_hand_count(git_repo):
    git_repo.commit(
        message="c1", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00", files={"a.py": "x\n"},
    )
    git_repo.commit(
        message="c2", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-20T10:00:00+00:00", files={"b.py": "x\n"},
    )
    git_repo.commit(
        message="c3", author_name="Amir Pourjabbari", author_email="amir@example.com",
        date="2025-07-10T10:00:00+00:00", files={"c.py": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    assert result["amedwishpond"]["2025-07"]["commits"] == 2
    assert result["mc4future"]["2025-07"]["commits"] == 1
    assert result["amedwishpond"]["2025-08"]["commits"] == 0


# ---------------------------------------------------------------------------
# IDENT-GIT-002: lines added / removed / net churn from fixture diffs
# ---------------------------------------------------------------------------
def test_ident_git_002_lines_added_removed_net(git_repo):
    git_repo.commit(
        message="add a 5-line file", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00", files={"a.txt": "one\ntwo\nthree\nfour\nfive\n"},
    )
    git_repo.commit(
        message="add a 3-line file", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-06T10:00:00+00:00", files={"b.txt": "x\ny\nz\n"},
    )
    git_repo.delete(
        path="a.txt", message="remove the 5-line file", author_name="Alejandro Medina",
        author_email="alejandro@example.com", date="2025-07-07T10:00:00+00:00",
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)
    row = result["amedwishpond"]["2025-07"]

    assert row["commits"] == 3
    assert row["lines_added"] == 8  # 5 + 3
    assert row["lines_removed"] == 5
    assert row["net"] == 3  # 8 - 5


# ---------------------------------------------------------------------------
# IDENT-GIT-003: active-days = distinct UTC calendar days, not commit count
# ---------------------------------------------------------------------------
def test_ident_git_003_active_days_counts_distinct_days_not_commits(git_repo):
    git_repo.commit(
        message="morning", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T09:00:00+00:00", files={"a.txt": "x\n"},
    )
    git_repo.commit(
        message="evening same day", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T18:00:00+00:00", files={"b.txt": "x\n"},
    )
    git_repo.commit(
        message="next day", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-06T09:00:00+00:00", files={"c.txt": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)
    row = result["amedwishpond"]["2025-07"]

    assert row["commits"] == 3
    assert row["active_days"] == 2


# ---------------------------------------------------------------------------
# IDENT-GIT-004: test-touch rate applies the repo-agnostic path rule
# ---------------------------------------------------------------------------
def test_ident_git_004_test_touch_rate_applies_path_rule(git_repo):
    git_repo.commit(
        message="prod change", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T09:00:00+00:00", files={"src/app.py": "x\n"},
    )
    git_repo.commit(
        message="test/ dir change", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-06T09:00:00+00:00", files={"tests/test_app.py": "x\n"},
    )
    git_repo.commit(
        message=".spec. file change", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-07T09:00:00+00:00", files={"src/app.spec.js": "x\n"},
    )
    git_repo.commit(
        message="__tests__ dir change", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-08T09:00:00+00:00", files={"__tests__/app.js": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)
    row = result["amedwishpond"]["2025-07"]

    assert row["commits"] == 4
    assert row["test_touch_rate"] == 3 / 4  # 3 of 4 commits touch a test-marked path


# ---------------------------------------------------------------------------
# IDENT-GIT-008: two git author emails for the same roster member merge
# into that one developer's metrics, not split into two people
# ---------------------------------------------------------------------------
def test_ident_git_008_multiple_aliases_merge_into_one_developer(git_repo):
    git_repo.commit(
        message="via primary alias", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T09:00:00+00:00", files={"a.txt": "x\n"},
    )
    git_repo.commit(
        message="via secondary alias", author_name="Alejandro M", author_email="alejandro.alt@example.com",
        date="2025-07-06T09:00:00+00:00", files={"b.txt": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    assert result["amedwishpond"]["2025-07"]["commits"] == 2
    assert set(result.keys()) == {"amedwishpond", "mc4future"}  # not a 3rd bucket


# ---------------------------------------------------------------------------
# IDENT-GIT-009 (integration): roster + bot + unmapped in one pass ->
# only resolvable roster members get rows; bot/unmapped excluded and the
# unmapped author is logged.
# ---------------------------------------------------------------------------
def test_ident_git_009_mixed_batch_excludes_bot_and_unmapped_and_logs(git_repo, caplog):
    git_repo.commit(
        message="roster commit", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T09:00:00+00:00", files={"a.txt": "x\n"},
    )
    git_repo.commit(
        message="bot commit", author_name="cursor[bot]", author_email="bot@example.com",
        date="2025-07-06T09:00:00+00:00", files={"b.txt": "x\n"},
    )
    git_repo.commit(
        message="unmapped commit", author_name="Random Person", author_email="random@nowhere.example",
        date="2025-07-07T09:00:00+00:00", files={"c.txt": "x\n"},
    )

    with caplog.at_level(logging.WARNING):
        result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    assert result["amedwishpond"]["2025-07"]["commits"] == 1
    assert result["mc4future"]["2025-07"]["commits"] == 0
    total_commits = sum(row["commits"] for months in result.values() for row in months.values())
    assert total_commits == 1  # bot + unmapped commits never appear anywhere

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("random@nowhere.example" in r.getMessage() for r in warnings)
    assert not any("cursor[bot]" in r.getMessage() for r in warnings)  # bots aren't logged, just filtered


# ---------------------------------------------------------------------------
# Anti-duplication design check (the task's explicit "not inflated by
# squash/merge duplication" requirement): a merge commit itself is not
# counted as a 3rd commit for whoever performed the merge.
# ---------------------------------------------------------------------------
def test_merge_commit_itself_is_excluded_from_commit_count(git_repo):
    git_repo.commit(
        message="init", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-01T09:00:00+00:00", files={"base.txt": "x\n"},
    )
    git_repo.checkout_new_branch("feature")
    git_repo.commit(
        message="feature work", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-02T09:00:00+00:00", files={"feature.txt": "x\n"},
    )
    git_repo.checkout("main")
    git_repo.merge(
        branch="feature", message="Merge pull request #1 from org/feature",
        author_name="Amir Pourjabbari", author_email="amir@example.com", date="2025-07-03T09:00:00+00:00",
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    assert result["amedwishpond"]["2025-07"]["commits"] == 2  # init + feature work
    assert result["mc4future"]["2025-07"]["commits"] == 0  # the merge action itself doesn't count
