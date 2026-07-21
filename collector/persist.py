"""Persistence layer: assembles `data/metrics.json` exactly per
implementation-plan.md S3's data contract, writes per-repo/per-month raw JSON
snapshots (spec.md S9), and writes human-readable Markdown summaries.

Orchestrates bucketing.py -> scoring.py -> dora.py in that order and merges
their outputs into one document; owns no git/GitHub collection of its own
(identity.py/git_source.py/github_source.py/branch.py are collect.py's job --
`repos` entries arriving here must already carry the detected `org`,
`default_branch`, `merge_strategy` fields, per branch.py's own docstring:
`detect_all()`/`detect_repo()` exist "for collect.py/persist.py to build
metrics.json's repos[] array"). Keeping persist.py itself free of git
subprocess calls (dora.py is the one exception -- see below) is what keeps
METRIC-PERSIST-001..008 testable from pure hand-written fixtures, per
test-plan-metrics.md GAP-3.

Note: dora.build_team_dora_metrics *does* re-detect each repo's default
branch and re-fetch merged PRs (via github_source.py's cache) -- that is
dora.py's own documented design (it needs raw PR titles bucketing.py's
already-collapsed output doesn't preserve), not something persist.py can
avoid. A repo whose `local_path` doesn't resolve degrades to zero DORA
signal for that repo rather than crashing (dora.py's own per-repo fault
isolation), so persist.py's own tests can use lightweight repo fixtures
without a real git checkout.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from collector import bucketing, dora, scoring
from collector.git_source import default_window_months
from collector.identity import IdentityMap

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = "data"


class PersistError(RuntimeError):
    """Raised when persist.py is given structurally invalid input."""


def _initials(name: str) -> str:
    """First letter of the first and last whitespace-separated tokens,
    uppercased (`"Amir Pourjabbari"` -> `"AP"`). A single-token name falls
    back to its first two letters so the field is never empty for a
    plausible human name."""
    parts = name.split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "unknown"


def _weighted_average(pairs: Iterable[tuple]) -> float:
    """Same convention as bucketing.py's own helper of the same name (not
    imported from there -- that one is module-private): weight-weighted
    average, 0.0 if every weight is zero."""
    total_weight = 0.0
    total = 0.0
    for value, weight in pairs:
        total += value * weight
        total_weight += weight
    return (total / total_weight) if total_weight else 0.0


def _average_skip_none(pairs: Iterable[tuple]) -> Optional[float]:
    """Same convention as bucketing.py's own helper: weighted average over
    only non-None entries, None (never 0.0) if every entry is None --
    preserves the ci_pass_rate null-vs-zero distinction through this
    module's own re-aggregations (team_breakdown/repo_breakdown)."""
    present = [(value, weight) for value, weight in pairs if value is not None]
    if not present:
        return None
    total_weight = sum(weight for _, weight in present)
    if not total_weight:
        return sum(value for value, _ in present) / len(present)
    return sum(value * weight for value, weight in present) / total_weight


def build_roster_section(identity_map: IdentityMap) -> list:
    """roster[] -- always exactly the 9 canonical people, in
    `identity_map.roster`'s order (already validated against EXPECTED_ROSTER
    upstream by identity.validate_identity_map), matching METRIC-PERSIST-001."""
    return [
        {"name": person.name, "handle": person.handle, "initials": _initials(person.name)}
        for person in identity_map.roster
    ]


def build_repos_section(repos: Iterable[dict]) -> list:
    """repos[] -- `{name, org, default_branch, merge_strategy}` per repo, in
    the same order `repos` was given. Requires `default_branch`/
    `merge_strategy` already present on each entry (collect.py runs
    branch.detect_all() once and merges it in before calling persist.py)."""
    section = []
    for repo in repos:
        try:
            section.append({
                "name": repo["name"],
                "org": repo.get("org", ""),
                "default_branch": repo["default_branch"],
                "merge_strategy": repo["merge_strategy"],
            })
        except KeyError as exc:
            raise PersistError(
                f"repo entry {repo.get('name', '?')!r} is missing {exc}; "
                "did the caller forget to merge in branch.detect_all()'s output?"
            ) from exc
    return section


def build_window_section(window_months: list) -> dict:
    return {"start": window_months[0], "end": window_months[-1], "months": list(window_months)}


def _developer_composite_aggregate(monthly_breakdown: dict, window_months: list) -> dict:
    """The full-window `developers[].composite: {score, signals}` object
    scoring.py's own handoff explicitly leaves for persist.py to derive: the
    average of the 12 months' already-computed `score`s and `signals`
    (scoring.py's judgment call #6 -- this is a plain average of an already
    per-month-normalized value, not a second renormalization)."""
    scores = [monthly_breakdown[month]["score"] for month in window_months]
    signals = {
        key: sum(monthly_breakdown[month]["signals"][key] for month in window_months) / len(window_months)
        for key in scoring.SIGNAL_KEYS
    }
    return {"score": sum(scores) / len(scores), "signals": signals}


def _review_shares(by_developer: dict, handles: list, window_months: list) -> dict:
    """Each roster developer's share of the team's total reviews given over
    the whole window (spec.md S11's "review share among the 9"). 0.0 for
    everyone if the team gave zero reviews all window, rather than a
    divide-by-zero crash."""
    totals = {
        handle: sum(by_developer[handle][month]["reviews_given"] for month in window_months)
        for handle in handles
    }
    team_total = sum(totals.values())
    if not team_total:
        return {handle: 0.0 for handle in handles}
    return {handle: totals[handle] / team_total for handle in handles}


def _developer_half(by_developer: dict, handle: str, half_months: tuple, half_composite: float) -> dict:
    commits = sum(by_developer[handle][month]["commits"] for month in half_months)
    return {"commits": commits, "composite": half_composite}


def _developer_active_repos(by_repo: dict, handle: str, repo_order: list, window_months: list) -> list:
    """The repos (in `repo_order`) this developer has any commit, merged
    PR, or review in, across the whole window -- `dashboard.js`'s
    `findDeveloperActiveRepos` (TASK-004-014) reads this field to decide
    which Per-Repo table rows to select/dim for the cross-panel developer
    filter, but no collector module ever emitted it (TASK-004-016's handoff
    finding #2). Mirrors `build_repo_breakdown_section`'s own `active_devs`
    derivation rule (row values, not row presence, since every developer
    always has a zero-filled row for every repo)."""
    active = []
    for repo_name in repo_order:
        dev_rows = by_repo.get(repo_name, {}).get(handle, {})
        has_activity = any(
            dev_rows.get(month, {}).get("commits")
            or dev_rows.get(month, {}).get("prs_merged")
            or dev_rows.get(month, {}).get("reviews_given")
            for month in window_months
        )
        if has_activity:
            active.append(repo_name)
    return active


def build_developers_section(
    consolidated: dict,
    breakdowns: dict,
    half_year: dict,
    identity_map: IdentityMap,
    window_months: list,
    repo_order: list,
) -> list:
    """developers[] -- one entry per roster person, in roster order
    (METRIC-PERSIST-001). `monthly[]` is sourced from `by_developer` (rolled
    up across all repos), per bucketing.py's own handoff note that this --
    not `by_repo` -- is what implementation-plan.md S3's example payload
    shows."""
    by_developer = consolidated["by_developer"]
    by_repo = consolidated["by_repo"]
    handles = [person.handle for person in identity_map.roster]
    h1_months, h2_months = scoring.half_year_months(window_months)
    shares = _review_shares(by_developer, handles, window_months)

    developers = []
    for person in identity_map.roster:
        handle = person.handle
        monthly_breakdown = breakdowns[handle]
        monthly_rows = [dict(by_developer[handle][month]) for month in window_months]
        developers.append({
            "name": person.name,
            "handle": handle,
            "initials": _initials(person.name),
            "composite": _developer_composite_aggregate(monthly_breakdown, window_months),
            "review_share": shares[handle],
            "active_repos": _developer_active_repos(by_repo, handle, repo_order, window_months),
            "monthly": monthly_rows,
            "h1": _developer_half(by_developer, handle, h1_months, half_year[handle]["h1"]),
            "h2": _developer_half(by_developer, handle, h2_months, half_year[handle]["h2"]),
        })
    return developers


def _team_half(team_monthly: dict, half_months: tuple) -> dict:
    """`reviews_per_mo`/`active_devs` are additive fields dashboard.js's KPI
    row already reads (h2.active_devs for the Active Devs tile, h1/h2's
    reviews_per_mo for the Reviews tile's delta) but the collector never
    emitted -- see TASK-004-016's handoff finding #1. Computed the same way
    as commits_per_mo/prs_per_mo: an average over the half's 6 months,
    reusing team.monthly[].reviews/.active_devs bucketing.py already
    derives (active_devs rounded to the nearest whole developer -- it is a
    headcount, not a rate)."""
    rows = [team_monthly[month] for month in half_months]
    return {
        "commits_per_mo": sum(r["commits"] for r in rows) / len(rows),
        "prs_per_mo": sum(r["prs_merged"] for r in rows) / len(rows),
        "reviews_per_mo": sum(r["reviews"] for r in rows) / len(rows),
        "active_devs": round(sum(r["active_devs"] for r in rows) / len(rows)),
        "cycle_time_days": _weighted_average((r["cycle_time_days"], r["prs_merged"]) for r in rows),
        "ci_pass_rate": _average_skip_none((r["ci_pass_rate"], r["prs_merged"]) for r in rows),
    }


def build_team_section(consolidated: dict, team_dora: dict, window_months: list) -> dict:
    """team{} -- monthly[] verbatim from bucketing's team roll-up, h1/h2
    rate rollups, and `dora` (full-window only -- implementation-plan.md S3's
    example shows no per-half team.h1.dora/team.h2.dora sub-object, so none
    is added here; see this task's handoff for the reasoning)."""
    team_monthly = consolidated["team"]
    h1_months, h2_months = scoring.half_year_months(window_months)
    return {
        "monthly": [team_monthly[month] for month in window_months],
        "h1": _team_half(team_monthly, h1_months),
        "h2": _team_half(team_monthly, h2_months),
        "dora": team_dora,
    }


def build_repo_breakdown_section(
    by_repo: dict, identity_map: IdentityMap, repo_order: list, window_months: list
) -> list:
    """repo_breakdown[] -- one row per repo (in `repo_order`), each a
    full-window collapse across all 9 developers and all 12 months. Not a
    monthly series (bucketing.py's handoff explicitly notes no such
    function exists upstream) -- this is the one aggregate only persist.py
    produces."""
    handles = [person.handle for person in identity_map.roster]
    breakdown = []
    for repo_name in repo_order:
        repo_matrix = by_repo.get(repo_name, {})
        commits = prs = reviews = active_devs = 0
        ci_pairs = []
        for handle in handles:
            dev_rows = repo_matrix.get(handle, {})
            dev_commits = sum(dev_rows.get(month, {}).get("commits", 0) for month in window_months)
            dev_prs = sum(dev_rows.get(month, {}).get("prs_merged", 0) for month in window_months)
            dev_reviews = sum(dev_rows.get(month, {}).get("reviews_given", 0) for month in window_months)
            commits += dev_commits
            prs += dev_prs
            reviews += dev_reviews
            if dev_commits or dev_prs or dev_reviews:
                active_devs += 1
            ci_pairs.extend(
                (dev_rows.get(month, {}).get("ci_pass_rate"), dev_rows.get(month, {}).get("prs_merged", 0))
                for month in window_months
            )
        breakdown.append({
            "repo": repo_name,
            "commits": commits,
            "prs": prs,
            "reviews": reviews,
            "ci_pass_rate": _average_skip_none(ci_pairs),
            "active_devs": active_devs,
        })
    return breakdown


def collect_and_assemble(
    repos: Iterable[dict],
    identity_map: IdentityMap,
    git_metrics_by_repo: dict,
    github_metrics_by_repo: dict,
    score_weights: dict,
    window_months: Optional[list] = None,
    generated_at: Optional[str] = None,
    dora_cache_root: Optional[str] = None,
) -> tuple:
    """Top-level assembly: runs bucketing.build_consolidated_metrics ->
    scoring.score_all -> dora.build_team_dora_metrics, in that order (the
    order scoring.py's own handoff prescribes), and returns
    `(metrics_document, consolidated)`. `consolidated["by_repo"]` (already
    scored -- `composite` filled in in place by scoring.score_all) is what
    write_raw_json persists per repo/month; returning it alongside the
    document avoids bucketing the same inputs twice.
    """
    months = window_months if window_months is not None else default_window_months()
    repos = list(repos)

    consolidated = bucketing.build_consolidated_metrics(
        repos, identity_map, git_metrics_by_repo, github_metrics_by_repo, window_months=months
    )
    scored = scoring.score_all(consolidated, score_weights, identity_map, window_months=months)
    team_dora = dora.build_team_dora_metrics(
        repos,
        window_months=months,
        cache_root=dora_cache_root if dora_cache_root is not None else dora.DEFAULT_CACHE_ROOT,
    )

    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = {
        "generated_at": generated_at,
        "window": build_window_section(months),
        "roster": build_roster_section(identity_map),
        "repos": build_repos_section(repos),
        "score_weights": dict(score_weights),
        "team": build_team_section(consolidated, team_dora, months),
        "developers": build_developers_section(
            consolidated, scored["monthly"], scored["half_year"], identity_map, months,
            [repo["name"] for repo in repos],
        ),
        "repo_breakdown": build_repo_breakdown_section(
            consolidated["by_repo"], identity_map, [repo["name"] for repo in repos], months
        ),
    }
    return document, consolidated


def write_metrics_json(document: dict, path) -> None:
    """Writes `document` as pretty-printed, deterministically-ordered JSON
    (insertion order, not sorted -- every builder above constructs its dict
    the same way given the same inputs, which is what makes two runs
    byte-identical, METRIC-IDEMP-001)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
        f.write("\n")


def write_raw_json(by_repo: dict, raw_root, window_months: list) -> None:
    """Writes `<raw_root>/<repo>/<YYYY-MM>.json` for every repo x window
    month (METRIC-PERSIST-006) -- the per-repo/month granular facts (spec.md
    S9), one merged git+GitHub+composite row per roster developer. Distinct
    from github_source.py's own raw-API-response cache, which lives under
    `<raw_root>/<repo>/github/` -- no path collision.
    """
    raw_root = Path(raw_root)
    for repo_name, repo_matrix in by_repo.items():
        repo_dir = raw_root / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)
        for month in window_months:
            developers = {
                handle: month_rows[month]
                for handle, month_rows in repo_matrix.items()
                if month in month_rows
            }
            payload = {"repo": repo_name, "month": month, "developers": developers}
            with open(repo_dir / f"{month}.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")


def _format_team_markdown(document: dict) -> str:
    team = document["team"]
    window = document["window"]
    total_commits = sum(row["commits"] for row in team["monthly"])
    total_prs = sum(row["prs_merged"] for row in team["monthly"])
    total_reviews = sum(row["reviews"] for row in team["monthly"])
    lines = [
        f"# Team Summary ({window['start']} to {window['end']})",
        "",
        f"Generated at: {document['generated_at']}",
        "",
        "## Headline totals",
        "",
        f"- Total commits: {total_commits}",
        f"- Total PRs merged: {total_prs}",
        f"- Total reviews given: {total_reviews}",
        f"- Roster size: {len(document['roster'])}",
        f"- Repos tracked: {len(document['repos'])}",
        "",
        "## H1 vs H2",
        "",
        f"- Commits/mo: H1 {team['h1']['commits_per_mo']:.1f} -> H2 {team['h2']['commits_per_mo']:.1f}",
        f"- PRs/mo: H1 {team['h1']['prs_per_mo']:.1f} -> H2 {team['h2']['prs_per_mo']:.1f}",
        "",
        "## DORA (full window, approximated -- see spec.md S5/GAP-1/GAP-2)",
        "",
        f"- Deploy frequency: {team['dora']['deploy_frequency_per_month']:.2f}/mo",
        f"- Lead time: {team['dora']['lead_time_days']:.2f} days",
        f"- Change failure rate: {team['dora']['change_failure_rate']:.1%}",
        "- MTTR: N/A (no incident source -- honestly labeled, not faked)",
        "",
    ]
    return "\n".join(lines) + "\n"


def _format_developer_markdown(dev: dict) -> str:
    lines = [
        f"# {dev['name']} ({dev['handle']})",
        "",
        f"Composite score (window average): {dev['composite']['score']:.1f}",
        "",
        "## Signal breakdown (normalized 0-1, weights in config/score-weights.json)",
        "",
    ]
    for key, value in dev["composite"]["signals"].items():
        lines.append(f"- {key}: {value:.2f}")
    lines += [
        "",
        f"Review share among the 9: {dev['review_share']:.1%}",
        "",
        "## H1 vs H2",
        "",
        f"- H1: {dev['h1']['commits']} commits, composite {dev['h1']['composite']:.1f}",
        f"- H2: {dev['h2']['commits']} commits, composite {dev['h2']['composite']:.1f}",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_markdown_summaries(document: dict, summaries_root) -> None:
    """One `team.md` plus one `<developer-slug>.md` per roster member
    (METRIC-PERSIST-007), each containing that person's/the team's headline
    numbers."""
    summaries_root = Path(summaries_root)
    summaries_root.mkdir(parents=True, exist_ok=True)
    with open(summaries_root / "team.md", "w", encoding="utf-8") as f:
        f.write(_format_team_markdown(document))
    for dev in document["developers"]:
        slug = _slugify(dev["name"])
        with open(summaries_root / f"{slug}.md", "w", encoding="utf-8") as f:
            f.write(_format_developer_markdown(dev))


def persist_all(
    repos: Iterable[dict],
    identity_map: IdentityMap,
    git_metrics_by_repo: dict,
    github_metrics_by_repo: dict,
    score_weights: dict,
    data_dir: str = DEFAULT_DATA_DIR,
    window_months: Optional[list] = None,
    generated_at: Optional[str] = None,
    dora_cache_root: Optional[str] = None,
) -> dict:
    """The single entrypoint collect.py calls: assembles the metrics
    document (collect_and_assemble) and writes every persisted artifact --
    `<data_dir>/metrics.json`, `<data_dir>/raw/<repo>/<month>.json`, and
    `<data_dir>/summaries/*.md` -- returning the assembled document.
    """
    months = window_months if window_months is not None else default_window_months()
    document, consolidated = collect_and_assemble(
        repos,
        identity_map,
        git_metrics_by_repo,
        github_metrics_by_repo,
        score_weights,
        window_months=months,
        generated_at=generated_at,
        dora_cache_root=dora_cache_root if dora_cache_root is not None else f"{data_dir}/raw",
    )
    data_root = Path(data_dir)
    write_metrics_json(document, data_root / "metrics.json")
    write_raw_json(consolidated["by_repo"], data_root / "raw", months)
    write_markdown_summaries(document, data_root / "summaries")
    return document
