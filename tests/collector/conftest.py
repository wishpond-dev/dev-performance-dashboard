"""Shared pytest fixtures for the collector test suite: a small, fully
deterministic on-disk git repo builder used by the git_source.py tests
(IDENT-GIT-001..009) so they don't depend on the real 8 project repos'
ever-changing history. Committed as `tests/collector/fixtures/`-style
"small committed git fixture repos" per implementation-plan.md §6, just
built on the fly in `tmp_path` instead of checked into the repo, since
every test needs different, exactly-controlled commit dates/authors/diffs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

_GIT_ENV_EXTRA = {"GIT_TERMINAL_PROMPT": "0"}


def _run(repo_path: Path, args: list, extra_env: Optional[dict] = None) -> None:
    env = dict(os.environ, **_GIT_ENV_EXTRA, **(extra_env or {}))
    result = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, f"git {args} failed in {repo_path}: {result.stderr}"


def _author_env(author_name: str, author_email: str, date: str) -> dict:
    return {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": date,
    }


class GitRepoBuilder:
    """Builds a real, tiny git repo at `path` that tests can drive commit by
    commit with exact author/date/content control. `path` is exposed
    unchanged for passing straight to `collect_repo_git_metrics`."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        _run(path, ["init", "-q", "-b", "main"])
        _run(path, ["config", "user.name", "Fixture Builder"])
        _run(path, ["config", "user.email", "fixture@example.com"])

    def commit(self, *, message: str, author_name: str, author_email: str, date: str, files: dict) -> None:
        """`date` is a full ISO-8601 string (e.g. "2025-07-15T10:00:00+00:00"
        or with a non-UTC offset). `files` maps repo-relative path -> full
        content (written/overwritten, then staged)."""
        for rel_path, content in files.items():
            file_path = self.path / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            _run(self.path, ["add", rel_path])
        _run(self.path, ["commit", "-q", "-m", message], extra_env=_author_env(author_name, author_email, date))

    def delete(self, *, path: str, message: str, author_name: str, author_email: str, date: str) -> None:
        """Removes a previously-committed file -- produces a pure
        lines_removed diff with no lines_added, for churn/net-math tests."""
        _run(self.path, ["rm", "-q", path])
        _run(self.path, ["commit", "-q", "-m", message], extra_env=_author_env(author_name, author_email, date))

    def checkout_new_branch(self, name: str) -> None:
        _run(self.path, ["checkout", "-q", "-b", name])

    def checkout(self, name: str) -> None:
        _run(self.path, ["checkout", "-q", name])

    def merge(self, *, branch: str, message: str, author_name: str, author_email: str, date: str) -> None:
        """A real, non-fast-forward merge commit -- the fixture for
        verifying merge-strategy commits are excluded from git_source.py's
        counting (see git_source.py's module docstring)."""
        _run(
            self.path,
            ["merge", "--no-ff", "-q", "-m", message, branch],
            extra_env=_author_env(author_name, author_email, date),
        )


@pytest.fixture
def git_repo(tmp_path) -> GitRepoBuilder:
    return GitRepoBuilder(tmp_path / "repo")
