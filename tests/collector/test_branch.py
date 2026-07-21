"""Unit/integration tests for collector/branch.py -- see
test-plan-identity.md IDENT-BRANCH-001..005.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from collector.branch import classify_merge_strategy, detect_default_branch

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def _run(cwd: Path, args: list) -> None:
    env = dict(os.environ, **_GIT_ENV)
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"


def _make_origin_with_clone(tmp_path: Path, default_branch: str) -> Path:
    """Builds a bare 'origin' repo whose default branch is `default_branch`,
    pushes one commit to it, then clones it -- so the clone's
    refs/remotes/origin/HEAD symref genuinely reflects that default branch,
    exactly as it would for one of the project's real cloned repos."""
    bare = tmp_path / "origin.git"
    _run(tmp_path, ["init", "--bare", "-q", "-b", default_branch, str(bare)])

    work = tmp_path / "work"
    _run(tmp_path, ["init", "-q", "-b", default_branch, str(work)])
    _run(work, ["config", "user.name", "Fixture Builder"])
    _run(work, ["config", "user.email", "fixture@example.com"])
    (work / "README.md").write_text("fixture\n", encoding="utf-8")
    _run(work, ["add", "README.md"])
    _run(work, ["commit", "-q", "-m", "initial commit"])
    _run(work, ["remote", "add", "origin", str(bare)])
    _run(work, ["push", "-q", "origin", default_branch])

    clone = tmp_path / "clone"
    _run(tmp_path, ["clone", "-q", str(bare), str(clone)])
    return clone


def test_main_default_branch_detected_as_main(tmp_path):
    """IDENT-BRANCH-001: a repo whose default branch is `main` is
    detected as `main`, not assumed to be `master`."""
    repo_path = _make_origin_with_clone(tmp_path, "main")

    detected = detect_default_branch(str(repo_path))

    assert detected == "main"


def test_master_default_branch_detected_as_master(tmp_path):
    """IDENT-BRANCH-002: a repo whose default branch is `master` is
    detected as `master`."""
    repo_path = _make_origin_with_clone(tmp_path, "master")

    detected = detect_default_branch(str(repo_path))

    assert detected == "master"


def test_merge_commit_history_classified_as_merge_strategy(git_repo):
    """IDENT-BRANCH-003: a repo with merge-commit history on its default
    branch is classified as merge-strategy."""
    git_repo.commit(
        message="Initial commit",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-01T10:00:00+00:00",
        files={"README.md": "hello\n"},
    )
    git_repo.checkout_new_branch("feature/add-thing")
    git_repo.commit(
        message="Add the thing",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T10:00:00+00:00",
        files={"thing.py": "print('thing')\n"},
    )
    git_repo.checkout("main")
    git_repo.merge(
        branch="feature/add-thing",
        message="Merge pull request #1 from org/feature/add-thing",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-03T10:00:00+00:00",
    )

    strategy = classify_merge_strategy(str(git_repo.path), branch="main")

    assert strategy == "merge"


def test_squash_merge_history_classified_as_squash_strategy(git_repo):
    """IDENT-BRANCH-004: a repo with squash-merge history (one commit per
    PR, no merge commits) is classified as squash-strategy."""
    git_repo.commit(
        message="Initial commit",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-01T10:00:00+00:00",
        files={"README.md": "hello\n"},
    )
    # GitHub's squash-and-merge never produces a merge commit -- the PR's
    # commits are squashed into a single commit landed directly on main,
    # with the PR title + "(#N)" as the subject. Simulate two such PRs.
    git_repo.commit(
        message="Add the thing (#1)",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T10:00:00+00:00",
        files={"thing.py": "print('thing')\n"},
    )
    git_repo.commit(
        message="Fix the other thing (#2)",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-03T10:00:00+00:00",
        files={"other.py": "print('other')\n"},
    )

    strategy = classify_merge_strategy(str(git_repo.path), branch="main")

    assert strategy == "squash"


def test_merge_strategy_pr_counted_once_via_first_parent_walk(git_repo):
    """IDENT-MERGE-001: for a merge-strategy repo, one logical PR (feature
    branch + merge commit) is counted exactly once, not once per commit on
    the branch."""
    from collector.branch import iter_first_parent_commits

    git_repo.commit(
        message="Initial commit",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-01T10:00:00+00:00",
        files={"README.md": "hello\n"},
    )
    git_repo.checkout_new_branch("feature/add-thing")
    git_repo.commit(
        message="Add thing step 1",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T10:00:00+00:00",
        files={"thing.py": "print('thing')\n"},
    )
    git_repo.commit(
        message="Add thing step 2",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T11:00:00+00:00",
        files={"thing.py": "print('thing')\nprint('more')\n"},
    )
    git_repo.commit(
        message="Add thing step 3",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T12:00:00+00:00",
        files={"thing.py": "print('thing')\nprint('more')\nprint('done')\n"},
    )
    git_repo.checkout("main")
    git_repo.merge(
        branch="feature/add-thing",
        message="Merge pull request #7 from org/feature/add-thing",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-03T10:00:00+00:00",
    )

    rows = list(iter_first_parent_commits(str(git_repo.path), branch="main"))

    # The 3 feature-branch commits are absorbed into the merge commit --
    # only the initial commit and the merge commit itself should surface
    # from the --first-parent walk, never the 3 commits it absorbed.
    assert len(rows) == 2
    dedupe_keys = [row["dedupe_key"] for row in rows]
    assert dedupe_keys.count("pr:7") == 1

    merge_row = next(row for row in rows if row["dedupe_key"] == "pr:7")
    assert merge_row["is_merge_commit"] is True
    assert merge_row["pr_number"] == 7


def test_mixed_merge_and_squash_history_classified_deterministically(git_repo):
    """IDENT-BRANCH-005: a repo with mixed merge/squash history in its
    commit log still classifies deterministically per the documented
    tie-break rule (same fixture -> same classification on repeat runs)."""
    git_repo.commit(
        message="Initial commit",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-01T10:00:00+00:00",
        files={"README.md": "hello\n"},
    )
    # A real merge commit, same as the merge-strategy fixture above.
    git_repo.checkout_new_branch("feature/add-thing")
    git_repo.commit(
        message="Add the thing",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T10:00:00+00:00",
        files={"thing.py": "print('thing')\n"},
    )
    git_repo.checkout("main")
    git_repo.merge(
        branch="feature/add-thing",
        message="Merge pull request #1 from org/feature/add-thing",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-03T10:00:00+00:00",
    )
    # Later PRs on the same repo switched convention to squash-and-merge --
    # enough squash-suffixed non-merge commits to clear
    # SQUASH_EVIDENCE_THRESHOLD (>= 5% of non-merge commits), so both
    # conventions are genuinely present in the history.
    git_repo.commit(
        message="Fix login bug (#2)",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-04T10:00:00+00:00",
        files={"login.py": "print('login')\n"},
    )
    git_repo.commit(
        message="Add search feature (#3)",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00",
        files={"search.py": "print('search')\n"},
    )

    first = classify_merge_strategy(str(git_repo.path), branch="main")
    second = classify_merge_strategy(str(git_repo.path), branch="main")

    assert first == "mixed"
    assert second == "mixed"


def test_squash_strategy_pr_counted_once_matching_merge_strategy_repo(git_repo, tmp_path):
    """IDENT-MERGE-002: for a squash-strategy repo, one logical PR (squashed
    to a single commit) is counted exactly once, matching the merge-strategy
    repo's count for an equivalent PR."""
    from collector.branch import iter_first_parent_commits

    def _run_as(cwd: Path, args: list, author_name: str, author_email: str, date: str) -> None:
        env = dict(
            os.environ,
            **_GIT_ENV,
            GIT_AUTHOR_NAME=author_name,
            GIT_AUTHOR_EMAIL=author_email,
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_NAME=author_name,
            GIT_COMMITTER_EMAIL=author_email,
            GIT_COMMITTER_DATE=date,
        )
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
        )
        assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"

    # Squash-strategy repo: PR #9 lands as a single squashed commit on main,
    # no merge commit at all.
    git_repo.commit(
        message="Initial commit",
        author_name="Alejandro Medina",
        author_email="alejandro@example.com",
        date="2025-07-01T10:00:00+00:00",
        files={"README.md": "hello\n"},
    )
    git_repo.commit(
        message="Add the thing (#9)",
        author_name="Amir Pourjabbari",
        author_email="amir@example.com",
        date="2025-07-02T10:00:00+00:00",
        files={"thing.py": "print('thing')\n"},
    )

    # An equivalent merge-strategy repo: the same logical PR #9, landed via
    # a feature branch + merge commit instead of a squash commit. Built with
    # raw git commands (not the `git_repo`/GitRepoBuilder fixture) so this
    # test only needs a second independent repo path, no cross-conftest
    # import (tests/site_build/ ships its own conftest.py under the same
    # module name).
    merge_repo_path = tmp_path / "repo_merge"
    merge_repo_path.mkdir()
    _run(merge_repo_path, ["init", "-q", "-b", "main"])
    _run(merge_repo_path, ["config", "user.name", "Fixture Builder"])
    _run(merge_repo_path, ["config", "user.email", "fixture@example.com"])

    (merge_repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _run(merge_repo_path, ["add", "README.md"])
    _run_as(
        merge_repo_path,
        ["commit", "-q", "-m", "Initial commit"],
        "Alejandro Medina", "alejandro@example.com", "2025-07-01T10:00:00+00:00",
    )

    _run(merge_repo_path, ["checkout", "-q", "-b", "feature/add-thing"])
    (merge_repo_path / "thing.py").write_text("print('thing')\n", encoding="utf-8")
    _run(merge_repo_path, ["add", "thing.py"])
    _run_as(
        merge_repo_path,
        ["commit", "-q", "-m", "Add the thing"],
        "Amir Pourjabbari", "amir@example.com", "2025-07-02T10:00:00+00:00",
    )

    _run(merge_repo_path, ["checkout", "-q", "main"])
    _run_as(
        merge_repo_path,
        ["merge", "--no-ff", "-q", "-m", "Merge pull request #9 from org/feature/add-thing", "feature/add-thing"],
        "Alejandro Medina", "alejandro@example.com", "2025-07-03T10:00:00+00:00",
    )

    squash_rows = list(iter_first_parent_commits(str(git_repo.path), branch="main"))
    merge_rows = list(iter_first_parent_commits(str(merge_repo_path), branch="main"))

    # PR #9 surfaces exactly once from each repo's first-parent walk,
    # regardless of whether it landed as a squash commit or a merge commit --
    # never once per underlying feature-branch commit, never zero times.
    squash_pr_rows = [row for row in squash_rows if row["dedupe_key"] == "pr:9"]
    merge_pr_rows = [row for row in merge_rows if row["dedupe_key"] == "pr:9"]
    assert len(squash_pr_rows) == 1
    assert len(merge_pr_rows) == 1
    assert squash_pr_rows[0]["pr_number"] == 9
    assert merge_pr_rows[0]["pr_number"] == 9

    # The squash-strategy repo's count for this PR matches the
    # merge-strategy repo's count for the equivalent PR.
    assert len(squash_pr_rows) == len(merge_pr_rows)
