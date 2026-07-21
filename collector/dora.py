"""DORA proxy metrics: deploy frequency, lead time for changes,
change-failure rate, and MTTR (always N/A), per spec.md S5/S14 /
implementation-plan.md S4 step 8.

Built on top of github_source.py's already-established PR fetch/cache/auth
machinery (fetch_merged_prs, GitHubClient, the disk/memory cache stores,
_resolve_token, _parse_iso, and the dual-org fault-isolation pattern)
rather than duplicating it. Unlike github_source.py's MonthlyGitHubMetrics
(which collapses PRs into per-developer-per-month rates and drops PR
titles), the change-failure-rate heuristic needs each merged PR's raw
title, so this module re-fetches merged PRs itself instead of consuming
bucketing.py's already-collapsed output. Its disk cache reuses
github_source.py's exact cache location and cache key ("pulls" under
data/raw/<repo>/github/), so a real run never re-fetches PRs
github_source.py has already cached.

`mttr` is always emitted as the JSON null sentinel (`None`) -- spec.md S5:
"MTTR = N/A (no incident source -- honestly labeled, not faked)." No code
path in this module ever computes or estimates it (METRIC-DORA-005).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from collector.branch import detect_default_branch
from collector.git_source import default_window_months
from collector.github_source import (
    DEFAULT_CACHE_ROOT,
    GitHubClient,
    GitHubSourceError,
    _DiskCacheStore,
    _MemoryCacheStore,
    _parse_iso,
    _resolve_token,
    fetch_merged_prs,
)

logger = logging.getLogger(__name__)

# spec.md S5/GAP-2: title-keyword heuristic for change-failure rate.
# Case-insensitive; "fix regression" is a two-word phrase, the others are
# single tokens matched anywhere in the title -- deliberately loose so
# real-world titles like "Revert: bad deploy" or "Hotfix for prod outage"
# still match, while a normal "fix: typo in README" or "feature: add
# export button" does not (METRIC-DORA-002/003/004).
_CHANGE_FAILURE_KEYWORDS = re.compile(r"\b(revert|hotfix|rollback|fix regression)\b", re.IGNORECASE)


class DoraSourceError(RuntimeError):
    """Raised when DORA PR collection cannot proceed at all (should not
    normally escape collect_repo_dora_prs, which fault-isolates like
    github_source.py's collect_repo_github_metrics)."""


@dataclass(frozen=True)
class DoraMetrics:
    """One DORA-proxy summary over some window (full year, H1, or H2).
    `mttr` is always None -- spec.md S5's honestly-labeled N/A sentinel,
    matching the ci_pass_rate/composite null-sentinel convention used
    throughout git_source.py/github_source.py/bucketing.py."""

    deploy_frequency_per_month: float = 0.0
    lead_time_days: float = 0.0
    change_failure_rate: float = 0.0
    mttr: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "deploy_frequency_per_month": self.deploy_frequency_per_month,
            "lead_time_days": self.lead_time_days,
            "change_failure_rate": self.change_failure_rate,
            "mttr": self.mttr,
        }


def is_change_failure(title: Optional[str]) -> bool:
    """spec.md S5/GAP-2 keyword heuristic: revert/hotfix/rollback/fix
    regression anywhere in the PR title (case-insensitive) flags a change
    failure. A missing/empty title is never flagged."""
    if not title:
        return False
    return bool(_CHANGE_FAILURE_KEYWORDS.search(title))


def lead_time_days(pr: dict) -> Optional[float]:
    """merged_at - created_at in days for one raw GitHub PR dict
    (METRIC-DORA-001). None if either timestamp is missing/unparseable --
    a closed-without-merge PR should already be filtered out by the
    caller, but this stays defensive rather than raising."""
    merged_at = _parse_iso(pr.get("merged_at"))
    created_at = _parse_iso(pr.get("created_at"))
    if merged_at is None or created_at is None:
        return None
    return (merged_at - created_at).total_seconds() / 86400


def filter_merged_to_branch(
    prs: Iterable[dict], branch: str, window_months: Optional[list] = None
) -> list:
    """Raw PR dicts -> only those merged to `branch`, and (if
    window_months is given) merged within that window. Mirrors
    github_source.py's _populate_from_api merged-ness/base-branch filter
    (IDENT-GH-001) so dora.py and github_source.py agree on which PRs
    count as a deploy."""
    month_set = set(window_months) if window_months is not None else None
    filtered = []
    for pr in prs:
        merged_at = _parse_iso(pr.get("merged_at"))
        if merged_at is None:
            continue
        base_ref = (pr.get("base") or {}).get("ref")
        if base_ref != branch:
            continue
        if month_set is not None:
            month = f"{merged_at.year:04d}-{merged_at.month:02d}"
            if month not in month_set:
                continue
        filtered.append(pr)
    return filtered


def compute_monthly_deploy_frequency(prs: Iterable[dict], window_months: list) -> dict:
    """{month: count of merged PRs in that month} -- every window month
    present even at 0, matching the zero-fill convention used elsewhere in
    the collector. Consistent with the bucketed prs_merged count
    (METRIC-DORA-006) since it counts the exact same already
    branch-filtered PRs."""
    counts = {month: 0 for month in window_months}
    for pr in prs:
        merged_at = _parse_iso(pr.get("merged_at"))
        if merged_at is None:
            continue
        month = f"{merged_at.year:04d}-{merged_at.month:02d}"
        if month in counts:
            counts[month] += 1
    return counts


def compute_dora_metrics(prs: Iterable[dict], window_months: list) -> dict:
    """Pure aggregator: already branch-filtered raw PR dicts (a single
    repo's, or several repos' pooled together) + the window's month list
    -> one DoraMetrics dict.

    `deploy_frequency_per_month` is total deploys / number of window
    months (a rate, so an H1-only vs H2-only vs full-year window are
    directly comparable, and each reflects only its own months'
    merged PRs -- METRIC-DORA-007). `lead_time_days` and
    `change_failure_rate` are computed over only the PRs that actually
    fall in `window_months` -- a PR list containing out-of-window entries
    is filtered here defensively (callers are expected to have already
    filtered via filter_merged_to_branch, but this must never silently
    include the wrong window). Zero deploys in the window yields the
    DoraMetrics defaults (0.0/0.0/0.0/None) -- a defined value, never a
    NaN or divide-by-zero crash."""
    month_set = set(window_months)
    in_window = []
    for pr in prs:
        parsed = _parse_iso(pr.get("merged_at"))
        if parsed is not None and f"{parsed.year:04d}-{parsed.month:02d}" in month_set:
            in_window.append(pr)

    deploys = len(in_window)
    if deploys == 0 or not window_months:
        return DoraMetrics().to_dict()

    lead_times = [lt for pr in in_window for lt in [lead_time_days(pr)] if lt is not None]
    avg_lead_time = sum(lead_times) / len(lead_times) if lead_times else 0.0

    failures = sum(1 for pr in in_window if is_change_failure(pr.get("title")))
    change_failure_rate = failures / deploys

    deploy_frequency_per_month = deploys / len(window_months)

    return DoraMetrics(
        deploy_frequency_per_month=deploy_frequency_per_month,
        lead_time_days=avg_lead_time,
        change_failure_rate=change_failure_rate,
        mttr=None,
    ).to_dict()


def collect_repo_dora_prs(
    repo: dict,
    window_months: Optional[list] = None,
    default_branch: Optional[str] = None,
    token: Optional[str] = None,
    cache_root: Optional[str] = None,
    client: Optional[GitHubClient] = None,
) -> list:
    """Fetch and branch/window-filter one repo's merged PRs for DORA
    proxies. Shares github_source.py's cache location and cache key
    ("pulls" under data/raw/<repo>/github/) so a real run never re-fetches
    PRs github_source.py has already cached.

    Per-repo fault isolation matches collect_repo_github_metrics: any
    auth/rate-limit/unexpected failure is caught, logged, and this repo
    contributes an empty PR list rather than aborting the run.
    """
    months = window_months if window_months is not None else default_window_months()
    repo_name = repo.get("name") or repo["full_name"].split("/")[-1]
    store = (
        _MemoryCacheStore()
        if cache_root is None
        else _DiskCacheStore(Path(cache_root) / repo_name / "github")
    )

    try:
        branch = default_branch or detect_default_branch(repo["local_path"])
        tok = token if token is not None else _resolve_token(repo.get("local_path"))
        gh = client or GitHubClient(token=tok)
        raw_prs = fetch_merged_prs(gh, repo["full_name"], store)
    except GitHubSourceError:
        logger.exception("DORA PR collection failed for repo %s; contributing no PRs", repo_name)
        return []
    except Exception:  # noqa: BLE001 -- any other failure must not abort the run
        logger.exception("unexpected error collecting DORA PRs for repo %s; contributing no PRs", repo_name)
        return []

    return filter_merged_to_branch(raw_prs, branch, window_months=months)


def collect_all_dora_prs(
    repos: Iterable[dict],
    window_months: Optional[list] = None,
    cache_root: str = DEFAULT_CACHE_ROOT,
) -> dict:
    """Batch wrapper over collect_repo_dora_prs, mirroring
    github_source.py's collect_all_github_metrics convention. Returns
    {repo_name: [pr, ...]}, each list already branch/window-filtered."""
    results = {}
    for repo in repos:
        name = repo.get("name") or repo.get("full_name") or repo.get("local_path")
        results[name] = collect_repo_dora_prs(repo, window_months=window_months, cache_root=cache_root)
    return results


def build_team_dora_metrics(
    repos: Iterable[dict],
    window_months: Optional[list] = None,
    cache_root: str = DEFAULT_CACHE_ROOT,
) -> dict:
    """Top-level entrypoint: fetch every repo's filtered merged PRs and
    pool them into one team-level DORA object for this window (full year,
    H1, or H2 -- METRIC-DORA-007 is just this function called with a
    6-month window_months instead of all 12)."""
    months = window_months if window_months is not None else default_window_months()
    per_repo = collect_all_dora_prs(repos, window_months=months, cache_root=cache_root)
    pooled = [pr for prs in per_repo.values() for pr in prs]
    return compute_dora_metrics(pooled, months)


if __name__ == "__main__":
    import json
    import sys

    repos_config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"

    with open(repos_config_path, encoding="utf-8") as f:
        repos_list = json.load(f)["repos"]

    full_window = default_window_months()
    team_dora = build_team_dora_metrics(repos_list, window_months=full_window)
    print(
        f"deploy_frequency_per_month={team_dora['deploy_frequency_per_month']:.2f}  "
        f"lead_time_days={team_dora['lead_time_days']:.2f}  "
        f"change_failure_rate={team_dora['change_failure_rate']:.3f}  "
        f"mttr={team_dora['mttr']}"
    )
