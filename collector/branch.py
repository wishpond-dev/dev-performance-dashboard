"""Per-repo default-branch and merge-strategy detection.

Nothing here is ever hardcoded per repo. `config/repos.json` intentionally
ships `default_branch: null` for all 8 repos (see spec.md §6) -- this module
is what fills that in, at runtime, by inspecting the actual local clone.

Two things are detected, independently, per repo:

1. **Default branch** (`main` vs `master` vs anything else) via
   `detect_default_branch()`.
2. **Merge strategy** (`merge` / `squash` / `mixed`) via
   `classify_merge_strategy()`, by inspecting real commit-graph structure
   (parent counts) plus the commit-subject convention GitHub's squash-merge
   uses -- never assumed from the repo name or org.

It also exposes `pr_dedupe_key()` / `iter_first_parent_commits()` so
`git_source.py` and `github_source.py` can both agree on "one logical PR,
counted once" regardless of which merge strategy a given repo used.

Credential hygiene: the only git subcommand here that touches the network
(`git remote show origin`) never has its raw output logged or included in
any exception message -- the remote's fetch/push URLs embed a live token
(see implementation-plan.md §0.4) and must never be echoed. `git remote -v`
is never invoked.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

# A GitHub squash-and-merge commit gets the PR's title as its subject with
# " (#123)" appended. A "Create a merge commit" merge gets the standard
# "Merge pull request #123 from org/branch" subject. Both are used to
# recover the logical PR number a git commit belongs to.
_MERGE_COMMIT_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH_SUFFIX_PR_RE = re.compile(r"\(#(\d+)\)\s*$")

# A repo counts as having meaningful squash-merge evidence (not just one
# stray commit message that happens to reference an issue number) once at
# least this fraction of its non-merge commits carry the squash suffix.
# Below this, a repo with any real merge commits is classified "merge";
# at/above it, "mixed" (both conventions genuinely present in the history --
# expected per spec.md §6, since conventions changed over the year).
SQUASH_EVIDENCE_THRESHOLD = 0.05

_GIT_ENV_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}


class BranchDetectionError(RuntimeError):
    """Raised when a repo's default branch cannot be determined by any method."""


def _run_git(repo_path: str, args: list[str], timeout: int = 15, env: Optional[dict] = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        # stderr from these (local, non-network) subcommands never contains
        # a remote URL, so it's safe to surface for debugging.
        raise BranchDetectionError(
            f"git {' '.join(args)} failed in {repo_path}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _branch_from_symbolic_ref(repo_path: str) -> Optional[str]:
    """Primary method: read the locally-cached `refs/remotes/origin/HEAD`
    symref. Local-only, no network call, cannot leak the remote URL."""
    try:
        out = _run_git(repo_path, ["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])
    except BranchDetectionError:
        return None
    if not out:
        return None
    # out looks like "origin/main"
    return out.split("/", 1)[1] if "/" in out else out


def _branch_from_remote_show(repo_path: str) -> Optional[str]:
    """Fallback: `git remote show origin` contacts the network and its
    output includes the (possibly token-embedded) fetch/push URLs. Extract
    only the "HEAD branch:" line and discard everything else immediately --
    never log or raise the full output."""
    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=20,
            env={**_GIT_ENV_NO_PROMPT},
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("HEAD branch:"):
            branch = line.split(":", 1)[1].strip()
            return branch or None
    return None


def _remote_branch_exists(repo_path: str, branch: str) -> bool:
    try:
        out = _run_git(repo_path, ["rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"])
    except BranchDetectionError:
        return False
    return bool(out)


def _current_local_branch(repo_path: str) -> Optional[str]:
    try:
        out = _run_git(repo_path, ["symbolic-ref", "--short", "-q", "HEAD"])
    except BranchDetectionError:
        return None
    return out or None


def detect_default_branch(repo_path: str) -> str:
    """Detect a repo's actual default branch. Never guesses `main`/`master`
    -- tries, in order: the cached remote HEAD symref (fast, local, safe),
    `git remote show origin` (network, safe-extracted), whichever of
    `origin/main`/`origin/master` actually exists, then the checked-out
    branch. Raises BranchDetectionError only if all of these fail."""
    for method in (
        lambda: _branch_from_symbolic_ref(repo_path),
        lambda: _branch_from_remote_show(repo_path),
        lambda: next((b for b in ("main", "master") if _remote_branch_exists(repo_path, b)), None),
        lambda: _current_local_branch(repo_path),
    ):
        branch = method()
        if branch:
            return branch
    raise BranchDetectionError(f"could not detect default branch for {repo_path}")


def _rev_list_count(repo_path: str, branch: str, merges_only: bool = False) -> int:
    args = ["rev-list", "--count", branch]
    if merges_only:
        args.insert(1, "--merges")
    out = _run_git(repo_path, args)
    return int(out) if out else 0


def _count_squash_suffix_subjects(repo_path: str, branch: str) -> int:
    out = _run_git(repo_path, ["log", branch, "--no-merges", "--format=%s"])
    if not out:
        return 0
    return sum(1 for subject in out.split("\n") if _SQUASH_SUFFIX_PR_RE.search(subject))


def classify_merge_strategy(repo_path: str, branch: Optional[str] = None) -> str:
    """Classify a repo's merge strategy from real commit-history structure,
    not from convention/guesswork:

    - `"squash"` -- no merge commits (2+ parents) anywhere in the branch's
      history. Every PR (if any) was squashed to one linear commit.
    - `"merge"`  -- has merge commits, with no meaningful squash-suffix
      evidence among the non-merge commits.
    - `"mixed"`  -- has merge commits AND enough squash-suffixed non-merge
      commits (>= SQUASH_EVIDENCE_THRESHOLD of non-merge commits) to show
      both conventions were genuinely used. Deterministic: the same repo
      state always classifies the same way.
    """
    branch = branch or detect_default_branch(repo_path)
    total = _rev_list_count(repo_path, branch)
    if total == 0:
        return "squash"
    merges = _rev_list_count(repo_path, branch, merges_only=True)
    if merges == 0:
        return "squash"
    non_merge = total - merges
    squash_evidence = _count_squash_suffix_subjects(repo_path, branch)
    squash_ratio = (squash_evidence / non_merge) if non_merge else 0.0
    if squash_ratio >= SQUASH_EVIDENCE_THRESHOLD:
        return "mixed"
    return "merge"


def extract_pr_number(subject: str) -> Optional[int]:
    """Recover the GitHub PR number a commit subject represents, whether
    the repo uses merge commits or squash merges. Returns None for a plain
    direct-push commit with no associated PR."""
    m = _MERGE_COMMIT_PR_RE.match(subject)
    if m:
        return int(m.group(1))
    m = _SQUASH_SUFFIX_PR_RE.search(subject)
    if m:
        return int(m.group(1))
    return None


def is_squash_commit(subject: str) -> bool:
    """True if this commit subject looks like a GitHub squash-merge commit
    (ends with '(#NNN)'). Used by git_source.py to identify squash commits
    whose original feature-branch commits should be fetched from the GitHub
    API instead of counting the single squash commit."""
    return bool(_SQUASH_SUFFIX_PR_RE.search(subject))


def pr_dedupe_key(sha: str, subject: str) -> str:
    """Stable key identifying the logical PR (or direct-push commit) a git
    commit represents. `git_source.py` and `github_source.py` should dedupe
    on this key -- not on raw commit count -- so a merge-strategy PR (one
    merge commit + N feature-branch commits) and a squash-strategy PR (one
    commit) both count exactly once, and so a commit found by walking local
    git history can be correlated with the matching PR fetched from the
    GitHub API."""
    pr_number = extract_pr_number(subject)
    return f"pr:{pr_number}" if pr_number is not None else f"sha:{sha}"


def iter_first_parent_commits(repo_path: str, branch: Optional[str] = None) -> Iterator[dict]:
    """Walk only the mainline (`--first-parent`) history of `branch` (or the
    repo's detected default branch). This is the structural half of PR
    dedup: for a merge-strategy PR only its merge commit appears here (the
    feature-branch commits it absorbed are skipped), and for a
    squash-strategy PR its single squash commit appears here -- so counting
    rows from this walk already yields "one row per logical PR/push" without
    needing to look at `pr_dedupe_key` at all. The key is included on every
    row anyway, for callers that need to correlate against GitHub API data.
    """
    branch = branch or detect_default_branch(repo_path)
    fmt = "%H\x1f%P\x1f%an\x1f%ae\x1f%aI\x1f%s"
    out = _run_git(repo_path, ["log", "--first-parent", f"--format={fmt}", branch])
    if not out:
        return
    for line in out.split("\n"):
        if not line:
            continue
        sha, parents, author_name, author_email, author_date, subject = line.split("\x1f", 5)
        parent_list = parents.split() if parents else []
        yield {
            "sha": sha,
            "parents": parent_list,
            "is_merge_commit": len(parent_list) >= 2,
            "author_name": author_name,
            "author_email": author_email,
            "author_date": author_date,
            "subject": subject,
            "pr_number": extract_pr_number(subject),
            "dedupe_key": pr_dedupe_key(sha, subject),
        }


@dataclass(frozen=True)
class RepoBranchInfo:
    name: str
    local_path: str
    default_branch: str
    merge_strategy: str  # "merge" | "squash" | "mixed"


def detect_repo(local_path: str, name: Optional[str] = None) -> RepoBranchInfo:
    """Detect both the default branch and merge strategy for one repo."""
    default_branch = detect_default_branch(local_path)
    merge_strategy = classify_merge_strategy(local_path, branch=default_branch)
    return RepoBranchInfo(
        name=name or Path(local_path).name,
        local_path=local_path,
        default_branch=default_branch,
        merge_strategy=merge_strategy,
    )


def detect_all(repos: Iterable[dict]) -> list[RepoBranchInfo]:
    """Detect branch/strategy for every repo entry from config/repos.json
    (each dict needs at least `local_path`; `name` is used if present).
    `default_branch` in repos.json stays `null` on disk -- this populates it
    in memory, at runtime, for collect.py/persist.py to use when building
    metrics.json's `repos` array. repos.json itself is never rewritten."""
    results = []
    for repo in repos:
        try:
            results.append(detect_repo(repo["local_path"], name=repo.get("name")))
        except BranchDetectionError:
            logger.exception("branch detection failed for repo %s", repo.get("name", repo.get("local_path")))
            raise
    return results


if __name__ == "__main__":
    import json
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"
    with open(config_path, encoding="utf-8") as f:
        repos_config = json.load(f)["repos"]

    for info in detect_all(repos_config):
        print(f"{info.name:28s} default_branch={info.default_branch:8s} merge_strategy={info.merge_strategy}")
