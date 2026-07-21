"""CLI entrypoint: runs the full collector pipeline end to end -- identity
resolution -> git_source -> github_source -> (persist.py's own
bucketing -> scoring -> dora -> write) -- producing `data/metrics.json` +
per-repo/month raw JSON + Markdown summaries, per implementation-plan.md S4
step 9 / spec.md S6.

This module owns config loading (repos.json/identity-map.json/
score-weights.json), identity validation, branch/merge-strategy detection
(via branch.detect_all -- persist.py never shells out to git itself, see its
own module docstring), and the two raw-collection calls (git_source.py,
github_source.py). Everything downstream of that is persist.persist_all's
job.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from collector import branch, persist
from collector.git_source import collect_all_git_metrics, default_window_months
from collector.github_source import collect_all_github_metrics
from collector.identity import load_identity_map, validate_identity_map
from collector.shortcut_source import collect_shortcut_metrics

logger = logging.getLogger(__name__)

DEFAULT_REPOS_PATH = "config/repos.json"
DEFAULT_IDENTITY_MAP_PATH = "config/identity-map.json"
DEFAULT_WEIGHTS_PATH = "config/score-weights.json"
DEFAULT_DATA_DIR = "data"


def _load_repos(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["repos"]


def _load_score_weights(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _repos_with_branch_info(repos: list) -> list:
    """Merges branch.detect_all()'s detected `default_branch`/
    `merge_strategy` into each repos.json entry -- repos.json itself stays
    `default_branch: null` on disk (branch.py's own convention: detection
    always happens at runtime, in memory), so this is the one place that
    resolves it for the rest of the run."""
    info_by_name = {info.name: info for info in branch.detect_all(repos)}
    merged = []
    for repo in repos:
        name = repo.get("name") or repo["local_path"]
        info = info_by_name[name]
        merged.append({
            **repo,
            "default_branch": info.default_branch,
            "merge_strategy": info.merge_strategy,
        })
    return merged


def run_collect(
    repos_path: str = DEFAULT_REPOS_PATH,
    identity_map_path: str = DEFAULT_IDENTITY_MAP_PATH,
    weights_path: str = DEFAULT_WEIGHTS_PATH,
    data_dir: str = DEFAULT_DATA_DIR,
    window_months: Optional[list] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """Runs the full pipeline and writes every persisted artifact under
    `data_dir`. Returns the assembled metrics document (mainly useful for
    the CLI smoke-print and for tests -- callers that only need the on-disk
    side effects can ignore the return value).
    """
    months = window_months if window_months is not None else default_window_months()

    repos = _load_repos(repos_path)
    score_weights = _load_score_weights(weights_path)

    identity_map = load_identity_map(identity_map_path)
    validate_identity_map(identity_map)

    repos_with_branch = _repos_with_branch_info(repos)

    cache_root = f"{data_dir}/raw"
    git_metrics_by_repo = collect_all_git_metrics(repos_with_branch, identity_map, window_months=months)
    github_metrics_by_repo = collect_all_github_metrics(
        repos_with_branch, identity_map, window_months=months, cache_root=cache_root
    )
    shortcut_metrics = collect_shortcut_metrics(
        window_months=months, cache_root=cache_root
    )

    return persist.persist_all(
        repos_with_branch,
        identity_map,
        git_metrics_by_repo,
        github_metrics_by_repo,
        score_weights,
        data_dir=data_dir,
        window_months=months,
        generated_at=generated_at,
        dora_cache_root=cache_root,
        shortcut_metrics=shortcut_metrics,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the dev-performance-dashboard collector end-to-end."
    )
    parser.add_argument("--repos", default=DEFAULT_REPOS_PATH, help="path to config/repos.json")
    parser.add_argument(
        "--identity-map", default=DEFAULT_IDENTITY_MAP_PATH, help="path to config/identity-map.json"
    )
    parser.add_argument("--weights", default=DEFAULT_WEIGHTS_PATH, help="path to config/score-weights.json")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="output directory (default: data/)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    document = run_collect(
        repos_path=args.repos,
        identity_map_path=args.identity_map,
        weights_path=args.weights,
        data_dir=args.data_dir,
    )
    latest_month = document["team"]["monthly"][-1]
    print(
        f"wrote {args.data_dir}/metrics.json  "
        f"roster={len(document['roster'])}  repos={len(document['repos'])}  "
        f"latest_month={latest_month['month']} commits={latest_month['commits']} "
        f"prs_merged={latest_month['prs_merged']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
