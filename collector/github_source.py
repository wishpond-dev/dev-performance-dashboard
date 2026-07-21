"""GitHub API metrics: per-repo/month/developer PR, review, and CI
check-run metrics via the GitHub REST API (`/pulls`, `/pulls/{n}/reviews`,
`/commits/{sha}/check-runs`).

spec.md S6/S14 + implementation-plan.md S4 step 6 assign this module PRs
merged to the repo's *detected* default branch (never assumed -- reuses
branch.detect_default_branch()), reviews-per-PR/turnaround, change-request
rate, and CI pass rate, all cached to `data/raw/<repo>/github/` so re-runs
are cheap and idempotent (spec S6: "caches raw API responses so re-runs are
cheap"), with per-repo fault isolation for the dual-org token-scope risk
(implementation-plan.md S0.3: `salescloser-new-website` may 401/403 even
when the other 7 repos succeed).

Credential hygiene (implementation-plan.md S0.4, matching branch.py's
established pattern): `GH_TOKEN` env var is the primary auth path; the
fallback is the token embedded in the repo's `origin` remote URL, read via
`git config --get remote.origin.url` (never `git remote -v`) with only the
token substring ever kept in memory -- the URL itself is never logged,
printed, or included in any exception message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from collector.branch import detect_default_branch, is_squash_commit
from collector.git_source import default_window_months
from collector.identity import IdentityMap, RawAuthor, resolve_author, resolve_authors

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_CACHE_ROOT = "data/raw"

# Matches the token in `https://<token>@github.com/...` -- only the token
# substring is ever extracted; the rest of the URL is discarded immediately.
_TOKEN_IN_URL_RE = re.compile(r"https://([^@/\s]+)@")

_GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

MAX_RETRIES = 5
_DEFAULT_BACKOFF_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 60.0


class GitHubSourceError(RuntimeError):
    """Raised for unrecoverable GitHub API failures (retries exhausted on a
    rate limit, or any other non-auth HTTP error). Callers doing per-repo
    fault isolation (collect_repo_github_metrics) catch this and zero-fill
    rather than aborting the whole run."""


class GitHubAuthError(GitHubSourceError):
    """401, or 403 with no rate-limit signal at all -- treated as an
    auth/scope failure for this one repo (the dual-org token-scope risk),
    not a rate limit. Callers should zero-fill this repo's metrics and
    continue with the other 7."""


def _resolve_token(local_path: Optional[str] = None) -> Optional[str]:
    """GH_TOKEN env var is primary auth. Fallback: extract the token
    embedded in this repo's `origin` remote URL -- read via
    `git config --get remote.origin.url` (never `git remote -v`) -- and
    keep only the token substring in memory. The URL itself is never
    logged, printed, or placed in any exception message."""
    token = os.environ.get("GH_TOKEN")
    if token:
        return token
    if not local_path:
        return None
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=local_path,
            capture_output=True,
            text=True,
            timeout=10,
            env=_GIT_ENV_NO_PROMPT,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _TOKEN_IN_URL_RE.search(url)
    return match.group(1) if match else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class GitHubClient:
    """Thin HTTP wrapper: auth header, rate-limit backoff/retry, and simple
    page-count pagination. `session` and `sleep_fn` are injectable so tests
    never hit the real network or real-sleep (IDENT-GH-006/007)."""

    def __init__(
        self,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        sleep_fn=time.sleep,
        max_retries: int = MAX_RETRIES,
    ):
        self._session = session or requests.Session()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._session.headers.update(headers)
        self._sleep = sleep_fn
        self._max_retries = max_retries

    def _compute_backoff(self, resp: requests.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                wait = float(reset) - time.time()
                if wait > 0:
                    return min(wait, _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(_DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)

    def _request(self, url: str, params: Optional[dict] = None) -> requests.Response:
        attempt = 0
        while True:
            try:
                resp = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                raise GitHubSourceError(f"network error calling GitHub API: {exc}") from exc

            if resp.status_code == 200:
                return resp

            if resp.status_code == 401:
                raise GitHubAuthError(f"GitHub API auth failure (401) for {url.split('?')[0]}")

            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                is_rate_limit = resp.status_code == 429 or remaining == "0"
                if not is_rate_limit:
                    # 403 with no rate-limit signal at all -- permission/auth
                    # failure, not a rate limit; do not retry.
                    raise GitHubAuthError(f"GitHub API forbidden (403) for {url.split('?')[0]}")
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubSourceError(
                        "GitHub API rate limit exceeded and retries exhausted for "
                        f"{url.split('?')[0]}"
                    )
                wait = self._compute_backoff(resp, attempt)
                logger.warning(
                    "GitHub API rate-limited; backing off %.1fs (attempt %d/%d)",
                    wait, attempt, self._max_retries,
                )
                self._sleep(wait)
                continue

            raise GitHubSourceError(f"GitHub API error {resp.status_code} for {url.split('?')[0]}")

    def get_json(self, url: str, params: Optional[dict] = None):
        return self._request(url, params=params).json()

    def get_paginated(self, url: str, params: Optional[dict] = None) -> list:
        items: list = []
        page = 1
        base_params = dict(params or {})
        per_page = base_params.setdefault("per_page", 100)
        while True:
            query = dict(base_params, page=page)
            data = self._request(url, params=query).json()
            if not isinstance(data, list) or not data:
                break
            items.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return items


def _cache_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / f"{name}.json"


def _load_cache(path: Path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class _DiskCacheStore:
    """Persists to `data/raw/<repo>/github/` (spec.md S6 / implementation-
    plan.md S6: response caching is mandatory for real runs so re-runs are
    cheap). Used whenever a caller supplies an explicit `cache_root`."""

    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir

    def load(self, name: str):
        return _load_cache(_cache_path(self._cache_dir, name))

    def save(self, name: str, data) -> None:
        _save_cache(_cache_path(self._cache_dir, name), data)


class _MemoryCacheStore:
    """Ephemeral, per-call cache with no disk footprint. Used when no
    `cache_root` was explicitly supplied to collect_repo_github_metrics --
    real production entrypoints (collect_all_github_metrics/__main__)
    always resolve and pass an explicit path, so this only applies to
    direct/one-off callers, and must never leak state on disk between
    unrelated invocations that happen to share a nominal repo name."""

    def __init__(self):
        self._data: dict = {}

    def load(self, name: str):
        return self._data.get(name)

    def save(self, name: str, data) -> None:
        self._data[name] = data


def _cached_get_paginated(client: GitHubClient, store, cache_name: str, url: str, params=None) -> list:
    """Cache-first fetch: a hit means zero HTTP calls for this endpoint on
    the second run (IDENT-GH-006) -- `client.get_paginated` is only ever
    called on a genuine cache miss."""
    cached = store.load(cache_name)
    if cached is not None:
        return cached
    data = client.get_paginated(url, params=params)
    store.save(cache_name, data)
    return data


def _cached_get_json(client: GitHubClient, store, cache_name: str, url: str, params=None):
    cached = store.load(cache_name)
    if cached is not None:
        return cached
    data = client.get_json(url, params=params)
    store.save(cache_name, data)
    return data


def fetch_merged_prs(client: GitHubClient, full_name: str, store) -> list:
    """All closed PRs (merged and not) -- merged-ness and base-branch are
    filtered by the caller, not here, since a mocked fixture response is
    exactly this raw shape."""
    url = f"{GITHUB_API_BASE}/repos/{full_name}/pulls"
    return _cached_get_paginated(
        client, store, "pulls", url, params={"state": "closed", "sort": "updated", "direction": "desc"}
    )


def fetch_pr_reviews(client: GitHubClient, full_name: str, pr_number: int, store) -> list:
    url = f"{GITHUB_API_BASE}/repos/{full_name}/pulls/{pr_number}/reviews"
    return _cached_get_paginated(client, store, f"reviews_pr{pr_number}", url)


def fetch_pr_commits(client: GitHubClient, full_name: str, pr_number: int, store) -> list:
    """Fetch the list of commits on a PR's feature branch (the commits that
    were squashed into the single merge commit). Cached per PR number so
    re-runs are free. Used to count real feature-branch commits for
    squash-merged PRs, restoring the commit metric's meaning."""
    url = f"{GITHUB_API_BASE}/repos/{full_name}/pulls/{pr_number}/commits"
    return _cached_get_paginated(client, store, f"prcommits_{pr_number}", url)


def fetch_check_runs(client: GitHubClient, full_name: str, sha: str, store) -> list:
    url = f"{GITHUB_API_BASE}/repos/{full_name}/commits/{sha}/check-runs"
    data = _cached_get_json(client, store, f"checkruns_{sha}", url)
    return data.get("check_runs", []) if isinstance(data, dict) else []


@dataclass(frozen=True)
class MonthlyGitHubMetrics:
    """One complete developer/month row of GitHub-sourced metrics -- every
    field always present (implementation-plan.md S3's "no missing keys,
    ever" rule), matching the GitHub-derived subset of `developers[].monthly[]`.

    `ci_pass_rate` is JSON `null` (`None`) when zero check runs were found
    for this developer/month -- never `0.0` and never a fabricated default
    (spec.md S6 / implementation-plan.md S3's explicit rule for
    `salescloser-helm-charts`-style repos with no CI at all).

    `pr_commits` is the count of feature-branch commits inside squash-merged
    PRs attributed to this developer this month. These replace the single
    squash commit that git_source.py now excludes from its commit count,
    so the total `commits` in the merged row reflects real development
    effort rather than "number of merged PRs."
    """

    month: str
    prs_merged: int = 0
    cycle_time_days: float = 0.0
    reviews_given: int = 0
    review_turnaround_hours: float = 0.0
    change_request_rate: float = 0.0
    ci_pass_rate: Optional[float] = None
    pr_commits: int = 0

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "prs_merged": self.prs_merged,
            "cycle_time_days": self.cycle_time_days,
            "reviews_given": self.reviews_given,
            "review_turnaround_hours": self.review_turnaround_hours,
            "change_request_rate": self.change_request_rate,
            "ci_pass_rate": self.ci_pass_rate,
            "pr_commits": self.pr_commits,
        }


@dataclass
class _MonthGithubAccumulator:
    """Mutable per-(developer, month) accumulator; finalized into an
    immutable MonthlyGitHubMetrics once the PR/review/check-run walk is
    done. `ci_checks_total == 0` is what makes ci_pass_rate finalize to
    `None` rather than a fallback `0.0` -- there is no other code path that
    produces a CI value, so a division-by-zero can never silently become 0."""

    prs_merged: int = 0
    cycle_time_total_days: float = 0.0
    reviews_given: int = 0
    review_turnaround_total_hours: float = 0.0
    change_request_reviews: int = 0
    total_reviews_on_authored_prs: int = 0
    ci_checks_total: int = 0
    ci_checks_passed: int = 0
    pr_commits: int = 0

    def finalize(self, month: str) -> MonthlyGitHubMetrics:
        cycle_time = (self.cycle_time_total_days / self.prs_merged) if self.prs_merged else 0.0
        turnaround = (
            self.review_turnaround_total_hours / self.reviews_given
        ) if self.reviews_given else 0.0
        cr_rate = (
            self.change_request_reviews / self.total_reviews_on_authored_prs
        ) if self.total_reviews_on_authored_prs else 0.0
        ci_rate = (
            self.ci_checks_passed / self.ci_checks_total
        ) if self.ci_checks_total else None
        return MonthlyGitHubMetrics(
            month=month,
            prs_merged=self.prs_merged,
            cycle_time_days=cycle_time,
            reviews_given=self.reviews_given,
            review_turnaround_hours=turnaround,
            change_request_rate=cr_rate,
            ci_pass_rate=ci_rate,
            pr_commits=self.pr_commits,
        )


def _populate_from_api(
    client: GitHubClient,
    full_name: str,
    branch: str,
    month_set: set,
    identity_map: IdentityMap,
    accumulators: dict,
    store,
) -> None:
    """Walk merged PRs -> reviews -> check-runs for one repo, accumulating
    into `accumulators` (already zero-filled by the caller). PRs merged to
    any branch other than `branch` (the repo's *detected* default branch)
    are excluded entirely (IDENT-GH-001) -- this filter is applied in code,
    not just via the `/pulls` query param, so it holds regardless of what a
    given fixture/mocked response contains.
    """
    prs = fetch_merged_prs(client, full_name, store)
    for pr in prs:
        merged_at = _parse_iso(pr.get("merged_at"))
        if merged_at is None:
            continue  # closed but never merged -- not a merge event
        base_ref = (pr.get("base") or {}).get("ref")
        if base_ref != branch:
            continue  # merged to a non-default branch -- excluded (IDENT-GH-001)
        month = f"{merged_at.year:04d}-{merged_at.month:02d}"
        if month not in month_set:
            continue

        author_login = (pr.get("user") or {}).get("login", "")
        author_result = resolve_author(identity_map, name=author_login, login=author_login)

        created_at = _parse_iso(pr.get("created_at"))
        cycle_days = (merged_at - created_at).total_seconds() / 86400 if created_at else 0.0

        number = pr.get("number")
        reviews = fetch_pr_reviews(client, full_name, number, store) if number is not None else []
        cr_count = sum(1 for r in reviews if r.get("state") == "CHANGES_REQUESTED")

        if author_result.category == "roster":
            acc = accumulators[author_result.person.handle][month]
            acc.prs_merged += 1
            acc.cycle_time_total_days += cycle_days
            acc.change_request_reviews += cr_count
            acc.total_reviews_on_authored_prs += len(reviews)

        for review in reviews:
            reviewer_login = (review.get("user") or {}).get("login", "")
            reviewer_result = resolve_author(identity_map, name=reviewer_login, login=reviewer_login)
            if reviewer_result.category != "roster":
                continue
            submitted_at = _parse_iso(review.get("submitted_at"))
            if submitted_at is None or created_at is None:
                continue
            review_month = f"{submitted_at.year:04d}-{submitted_at.month:02d}"
            if review_month not in month_set:
                continue
            turnaround_hours = (submitted_at - created_at).total_seconds() / 3600
            racc = accumulators[reviewer_result.person.handle][review_month]
            racc.reviews_given += 1
            racc.review_turnaround_total_hours += turnaround_hours

        sha = pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha")
        if sha and author_result.category == "roster":
            check_runs = fetch_check_runs(client, full_name, sha, store)
            acc = accumulators[author_result.person.handle][month]
            for run in check_runs:
                acc.ci_checks_total += 1
                if run.get("conclusion") == "success":
                    acc.ci_checks_passed += 1

        # Fetch the PR's feature-branch commits to count real development
        # effort for squash-merged PRs. Each commit is attributed to its own
        # author (by GitHub login or commit email), not to the PR opener --
        # so a PR opened by amedwishpond but with commits authored by both
        # amedwishpond and a coding tool correctly credits only the human.
        # This replaces the single squash commit that git_source.py now
        # excludes, restoring the commit metric's meaning.
        if number is not None:
            pr_commits = fetch_pr_commits(client, full_name, number, store)
            for c in pr_commits:
                commit_author_name = (c.get("commit", {}).get("author", {}) or {}).get("name", "")
                commit_author_email = (c.get("commit", {}).get("author", {}) or {}).get("email", "")
                github_login = (c.get("author") or {}).get("login", "")
                commit_result = resolve_author(
                    identity_map,
                    name=commit_author_name,
                    email=commit_author_email,
                    login=github_login,
                )
                if commit_result.category != "roster":
                    continue
                commit_date = _parse_iso(
                    (c.get("commit", {}).get("author", {}) or {}).get("date", "")
                )
                if commit_date is None:
                    continue
                commit_month = f"{commit_date.year:04d}-{commit_date.month:02d}"
                if commit_month not in month_set:
                    continue
                accumulators[commit_result.person.handle][commit_month].pr_commits += 1


def collect_repo_github_metrics(
    repo: dict,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
    default_branch: Optional[str] = None,
    token: Optional[str] = None,
    cache_root: Optional[str] = None,
    client: Optional[GitHubClient] = None,
) -> dict:
    """Compute this repo's per-developer-per-month GitHub metrics.

    Returns `{roster_handle: {month: MonthlyGitHubMetrics.to_dict()}}`,
    complete for every one of `identity_map.roster`'s 9 people and every
    month in `window_months` -- the full matrix is scaffolded *before* any
    API call, exactly like git_source.py's collect_repo_git_metrics, so a
    repo/month with zero GitHub activity is a real zero row, not a missing
    key.

    Per-repo fault isolation (implementation-plan.md S0.3/S5): a
    GitHubSourceError/GitHubAuthError (auth failure, or a rate limit that
    outlasts all retries) -- or any other unexpected exception -- is caught
    here, logged, and this repo's matrix is returned zero-filled. The run
    must continue for the other 7 repos, never abort.

    `cache_root=None` (the default) uses a fresh, in-memory-only cache
    scoped to this single call, rather than touching `data/raw/` on disk --
    a direct caller who didn't ask for a durable cache location shouldn't
    have this call silently collide on disk with an unrelated invocation
    that happens to share the same nominal repo name. Real production
    entrypoints (collect_all_github_metrics/__main__) always resolve and
    pass an explicit path, so the mandatory disk caching required by
    spec.md S6 is unaffected for actual runs.
    """
    months = window_months if window_months is not None else default_window_months()
    month_set = set(months)

    accumulators = {
        person.handle: {month: _MonthGithubAccumulator() for month in months}
        for person in identity_map.roster
    }

    repo_name = repo.get("name") or repo["full_name"].split("/")[-1]
    if cache_root is None:
        store = _MemoryCacheStore()
    else:
        store = _DiskCacheStore(Path(cache_root) / repo_name / "github")

    try:
        branch = default_branch or detect_default_branch(repo["local_path"])
        tok = token if token is not None else _resolve_token(repo.get("local_path"))
        gh = client or GitHubClient(token=tok)
        _populate_from_api(gh, repo["full_name"], branch, month_set, identity_map, accumulators, store)
    except GitHubSourceError:
        logger.exception("GitHub metrics collection failed for repo %s; zero-filling", repo_name)
    except Exception:  # noqa: BLE001 -- any other failure must not abort the run
        logger.exception("unexpected error collecting GitHub metrics for repo %s; zero-filling", repo_name)

    return {
        handle: {month: acc.finalize(month).to_dict() for month, acc in month_accs.items()}
        for handle, month_accs in accumulators.items()
    }


def collect_all_github_metrics(
    repos,
    identity_map: IdentityMap,
    window_months: Optional[list] = None,
    cache_root: str = DEFAULT_CACHE_ROOT,
) -> dict:
    """Batch wrapper over collect_repo_github_metrics for every repo entry
    from config/repos.json, mirroring git_source.py's
    collect_repo_git_metrics/collect_all_git_metrics convention. Each repo
    is fully fault-isolated -- one repo's failure never affects another's
    result (implementation-plan.md S0.3)."""
    results = {}
    for repo in repos:
        name = repo.get("name") or repo.get("full_name") or repo.get("local_path")
        results[name] = collect_repo_github_metrics(
            repo, identity_map, window_months=window_months, cache_root=cache_root
        )
    return results


if __name__ == "__main__":
    import sys

    from collector.identity import load_identity_map, validate_identity_map

    repos_config_path = sys.argv[1] if len(sys.argv) > 1 else "config/repos.json"
    identity_map_path = sys.argv[2] if len(sys.argv) > 2 else "config/identity-map.json"

    with open(repos_config_path, encoding="utf-8") as f:
        repos_list = json.load(f)["repos"]

    loaded_map = load_identity_map(identity_map_path)
    validate_identity_map(loaded_map)

    all_metrics = collect_all_github_metrics(repos_list, loaded_map)
    for repo_name, per_dev in all_metrics.items():
        total_prs = sum(row["prs_merged"] for months in per_dev.values() for row in months.values())
        print(f"{repo_name:28s} total_prs_merged(9-roster, window)={total_prs}")
