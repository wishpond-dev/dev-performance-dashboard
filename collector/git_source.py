"""Local git metrics: per-repo/month/developer commit count, lines
added/removed/net churn, active-days, and test-touch rate.

spec.md §5/§10 define the metric families and the fixed 12-UTC-month
reporting window (Jul 2025 - Jun 2026); implementation-plan.md §4 step 5
assigns this module the job of computing them from the 8 local clones,
zero-filling repos/months with no history so every row is complete
(never a missing key).

Design note on "not inflated by squash/merge duplication" (the task
requirement that motivated importing branch.py here): a plain `git log
--all` would double-count squash-merged PRs whose original feature-branch
commits are still reachable via a stale local branch/remote-tracking ref
(the squash commit on the default branch AND the same content's original
commits both get counted). A plain `git log <default_branch>` with no
`--no-merges` would instead over-count on merge-strategy repos, crediting
the person who clicked "merge" with an extra commit on top of every
already-counted feature-branch commit. Both are avoided by walking only
`branch.detect_default_branch()`'s branch (never `--all`, so an orphaned
squash source branch is invisible) with `--no-merges` (so the merge action
itself, which carries no authored diff, is never counted). This is
deliberately NOT `branch.iter_first_parent_commits()` -- that walk is
correct for "one row per logical PR" (github_source.py's job) but would
silently drop nearly all real content for merge-strategy repos here: a
merge commit's default diff (without `-m`) is empty, so if merge commits
were the only rows visited, lines/active-days/test-touch would collapse to
near-zero for every repo that merges instead of squashes.

Squash-merge handling: commits whose subject ends with '(#NNN)' (GitHub's
squash-merge convention) are tracked separately as `squash_commits` and
excluded from the final `commits` count. Their original feature-branch
commits — the actual development work — are fetched from the GitHub
/pulls/{n}/commits API by github_source.py and counted as `pr_commits`.
This restores the commit metric's meaning for squash-merge repos: a PR
with 15 feature-branch commits contributes 15, not 1. For merge-strategy
repos (no squash suffix), feature-branch commits are already on the default
branch and counted normally — no PR-commit fetching needed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from collector.branch import detect_default_branch, is_squash_commit
from collector.identity import IdentityMap, RawAuthor, resolve_authors

logger = logging.getLogger(__name__)

# spec.md §10: the dashboard's fixed 12 UTC-calendar-month reporting window.
WINDOW_START_MONTH = "2025-07"
WINDOW_END_MONTH = "2026-06"

# spec.md §5 / implementation-plan.md §5: repo-agnostic test-touch heuristic,
# defined once here rather than per repo. Substring match on the full path,
# matching the spec's literal wording ("path contains test/spec/__tests__");
# `__tests__` is a subset of `test` but kept explicit since it's spec-named.
_TEST_PATH_MARKERS = ("test", "spec", "__tests__")

_GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

# ASCII Unit/Start-of-heading separators -- won't collide with real commit
# subjects/author names, unlike a printable delimiter such as "|".
_FIELD_SEP = "\x1f"
_COMMIT_MARKER = "\x01"
_LOG_FORMAT = f"{_COMMIT_MARKER}%H{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}%aI{_FIELD_SEP}%s"

# git reports an unborn/empty branch as a nonzero exit with one of these
# messages (wording varies slightly by git version) -- that is a valid
# zero-commit state (IDENT-GIT-006), not a real failure.
_EMPTY_BRANCH_MARKERS = ("unknown revision", "does not have any commits yet", "bad revision")


class GitSourceError(RuntimeError):
    """Raised when `git log` fails for a reason other than an empty/unborn branch."""


def default_window_months(start: str = WINDOW_START_MONTH, end: str = WINDOW_END_MONTH) -> list[str]:
    """The inclusive list of "YYYY-MM" strings from `start` to `end`."""
    start_year, start_month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    months = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


@dataclass(frozen=True)
class MonthlyGitMetrics:
    """One complete developer/month row -- every field always present, per
    the data contract's "no missing keys, ever" rule (implementation-plan.md
    §3)."""

    month: str
    commits: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    net: int = 0
    active_days: int = 0
    test_touch_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "commits": self.commits,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "net": self.net,
            "active_days": self.active_days,
            "test_touch_rate": self.test_touch_rate,
        }


@dataclass
class _MonthAccumulator:
    """Mutable per-(developer, month) accumulator; finalized into an
    immutable MonthlyGitMetrics once the commit walk is done.

    `squash_commits` tracks commits that are squash-merge artifacts (subject
    ends with '(#NNN)'). These are excluded from the final `commits` count
    because their original feature-branch commits — the real work — are
    fetched from the GitHub /pulls/{n}/commits API by github_source.py and
    counted there as `pr_commits`. Without this split, squash-merge repos
    would show 1 commit per PR regardless of how many commits the developer
    actually authored on the feature branch."""

    commits: int = 0
    squash_commits: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    active_days: set = field(default_factory=set)
    test_touch_commits: int = 0

    def finalize(self, month: str) -> MonthlyGitMetrics:
        real_commits = self.commits - self.squash_commits
        rate = (self.test_touch_commits / self.commits) if self.commits else 0.0
        return MonthlyGitMetrics(
            month=month,
            commits=real_commits,
            lines_added=self.lines_added,
            lines_removed=self.lines_removed,
            net=self.lines_added - self.lines_removed,
            active_days=len(self.active_days),
            test_touch_rate=rate,
        )


@dataclass(frozen=True)
class _RawCommit:
    sha: str
    author_name: str
    author_email: str
    author_date: datetime  # tz-aware, already converted to UTC
    subject: str
    paths: tuple
    lines_added: int
    lines_removed: int


def _run_git_log(repo_path: str, branch: str) -> str:
    result = subprocess.run(
        ["git", "log", branch, "--no-merges", "--numstat", f"--format={_LOG_FORMAT}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=120,
        env=_GIT_ENV_NO_PROMPT,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if any(marker in stderr for marker in _EMPTY_BRANCH_MARKERS):
            return ""
        raise GitSourceError(f"git log failed in {repo_path}: {stderr}")
    return result.stdout


def _parse_git_log(raw: str) -> list:
    """Parse `_run_git_log`'s output into one `_RawCommit` per commit,
    aggregating that commit's `--numstat` lines (one per changed file) as
    they follow its header line."""
    commits: list = []
    sha = author_name = author_email = subject = ""
    author_date: Optional[datetime] = None
    paths: list = []
    lines_added = lines_removed = 0

    def _flush() -> None:
        if sha:
            commits.append(
                _RawCommit(
                    sha=sha,
                    author_name=author_name,
                    author_email=author_email,
                    author_date=author_date,
                    subject=subject,
                    paths=tuple(paths),
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                )
            )

    for line in raw.split("\n"):
        if line.startswith(_COMMIT_MARKER):
            _flush()
            sha, author_name, author_email, author_date_str, subject = line[1:].split(_FIELD_SEP, 4)
            author_date = datetime.fromisoformat(author_date_str).astimezone(timezone.utc)
            paths = []
            lines_added = lines_removed = 0
        elif line.strip():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added_str, removed_str, path = parts
            if added_str != "-":
                lines_added += int(added_str)
            if removed_str != "-":
                lines_removed += int(removed_str)
            paths.append(path)
    _flush()
    return commits


def _touches_test_path(paths: Iterable[str]) -> bool:
    return any(marker in path.lower() for path in paths for marker in _TEST_PATH_MARKERS)


def collect_repo_git_metrics(
    repo_path: str,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
    default_branch: Optional[str] = None,
) -> dict:
    """Compute this repo's per-developer-per-month git metrics.

    Returns `{roster_handle: {month: MonthlyGitMetrics.to_dict()}}`,
    complete for every one of `identity_map.roster`'s 9 people and every
    month in `window_months` (default: the spec's 12-month window) --
    zero-filled wherever the repo/month/developer has no matching commits.
    This is what makes IDENT-GIT-005 (month before first commit) and
    IDENT-GIT-006 (repo with zero commits in the window) trivially
    satisfied: the full matrix is scaffolded before the commit walk ever
    runs, so "no commits found" and "no scaffold entry" never both occur --
    every developer/month key always exists.

    Non-roster commits (bots, departed devs, unmapped authors) are resolved
    via `identity.resolve_authors` and excluded from every row; unmapped
    authors are logged (identity.py's own behavior) so gaps stay visible
    rather than silently mis-attributed or silently dropped (IDENT-GIT-009).
    """
    months = window_months if window_months is not None else default_window_months()
    month_set = set(months)
    branch = default_branch or detect_default_branch(repo_path)

    accumulators = {
        person.handle: {month: _MonthAccumulator() for month in months} for person in identity_map.roster
    }

    raw = _run_git_log(repo_path, branch)
    commits = _parse_git_log(raw)

    # repo/month context on each RawAuthor lets resolve_authors() dedupe its
    # unmapped-author WARNING per (repo, month) rather than once for this
    # whole (multi-month) batch, so the same unmapped author across two
    # different months of this repo each gets logged (IDENT-EDGE-002).
    repo_name = os.path.basename(repo_path.rstrip("/\\"))
    resolved = resolve_authors(
        identity_map,
        [
            RawAuthor(
                name=c.author_name,
                email=c.author_email,
                repo=repo_name,
                month=f"{c.author_date.year:04d}-{c.author_date.month:02d}",
            )
            for c in commits
        ],
    )

    for commit, result in zip(commits, resolved):
        if result.category != "roster":
            continue
        month = f"{commit.author_date.year:04d}-{commit.author_date.month:02d}"
        if month not in month_set:
            continue
        acc = accumulators[result.person.handle][month]
        acc.commits += 1
        if is_squash_commit(commit.subject):
            acc.squash_commits += 1
        acc.lines_added += commit.lines_added
        acc.lines_removed += commit.lines_removed
        acc.active_days.add(commit.author_date.date())
        if _touches_test_path(commit.paths):
            acc.test_touch_commits += 1

    return {
        handle: {month: acc.finalize(month).to_dict() for month, acc in month_accs.items()}
        for handle, month_accs in accumulators.items()
    }


def collect_all_git_metrics(
    repos: Iterable[dict],
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
) -> dict:
    """Convenience wrapper over `collect_repo_git_metrics` for every repo
    entry from config/repos.json (each dict needs at least `local_path`;
    `name` is used as the result key if present). Mirrors branch.py's
    detect_repo()/detect_all() single-vs-batch convention."""
    results = {}
    for repo in repos:
        name = repo.get("name", repo["local_path"])
        results[name] = collect_repo_git_metrics(
            repo["local_path"], identity_map, window_months=window_months
        )
    return results


if __name__ == "__main__":
    import json
    import sys

    from collector.identity import load_identity_map, validate_identity_map

    repos_config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"
    identity_map_path = sys.argv[2] if len(sys.argv) > 2 else "config/identity-map.json"

    with open(repos_config_path, encoding="utf-8") as f:
        repos_list = json.load(f)["repos"]

    loaded_map = load_identity_map(identity_map_path)
    validate_identity_map(loaded_map)

    all_metrics = collect_all_git_metrics(repos_list, loaded_map)
    for repo_name, per_dev in all_metrics.items():
        total_commits = sum(row["commits"] for months in per_dev.values() for row in months.values())
        print(f"{repo_name:28s} total_commits(9-roster, window)={total_commits}")
