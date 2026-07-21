"""Shortcut API metrics: story points delivered per developer per month,
via the Shortcut REST API v3 (`/stories/search`).

Fetches completed stories in the dashboard's window, attributes story
points (the `estimate` field) to roster developers via the
`owner_ids` → roster-handle map in `config/shortcut-members.json`, and
buckets by `completed_at` month. Results are cached to
`data/raw/shortcut/` so re-runs are cheap.

Credential: the Shortcut API token is read from
`/home/openclaw/.openclaw/workspace/shortcut_token.txt` (the same token
used by all other Shortcut integrations). The token file is not
committed to the repo.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from collector.git_source import default_window_months

logger = logging.getLogger(__name__)

SHORTCUT_API_BASE = "https://api.app.shortcut.com/api/v3"
DEFAULT_CACHE_ROOT = "data/raw"
DEFAULT_TOKEN_PATH = "/home/openclaw/.openclaw/workspace/shortcut_token.txt"
DEFAULT_MEMBERS_PATH = "config/shortcut-members.json"


class ShortcutSourceError(RuntimeError):
    """Raised for unrecoverable Shortcut API failures."""


@dataclass
class MonthlyShortcutMetrics:
    """Per-developer, per-month story-point metrics from Shortcut."""
    story_points: int = 0
    stories_completed: int = 0


def _load_token(token_path: str = DEFAULT_TOKEN_PATH) -> str:
    """Read the Shortcut API token from the standard file."""
    path = Path(token_path)
    if not path.is_file():
        raise ShortcutSourceError(
            f"Shortcut token file not found: {token_path}"
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ShortcutSourceError(f"Shortcut token file is empty: {token_path}")
    return token


def _load_members_map(members_path: str = DEFAULT_MEMBERS_PATH) -> dict:
    """Load the Shortcut member UUID → roster handle map."""
    with open(members_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("members", {})


def _search_completed_stories(
    token: str,
    completed_at_start: str,
    completed_at_end: str,
) -> list:
    """Search Shortcut for all stories completed in the given date range.

    Returns a list of story dicts. The API returns all matching stories
    in a single response (no pagination needed for our volume).
    """
    url = f"{SHORTCUT_API_BASE}/stories/search"
    headers = {
        "Shortcut-Token": token,
        "Content-Type": "application/json",
    }
    body = {
        "completed_at_start": completed_at_start,
        "completed_at_end": completed_at_end,
    }
    resp = requests.post(url, json=body, headers=headers, timeout=60)
    if resp.status_code == 429:
        raise ShortcutSourceError("Shortcut API rate limit exceeded (429)")
    if resp.status_code not in (200, 201):
        raise ShortcutSourceError(
            f"Shortcut API returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    if not isinstance(data, list):
        raise ShortcutSourceError(
            f"Shortcut API returned unexpected type {type(data).__name__}"
        )
    return data


def _cache_path(cache_root: str) -> Path:
    """Return the cache directory for Shortcut API responses."""
    return Path(cache_root) / "shortcut"


def _month_from_completed(completed_at: Optional[str]) -> Optional[str]:
    """Extract YYYY-MM from a completed_at timestamp, or None."""
    if not completed_at:
        return None
    return completed_at[:7]


def collect_shortcut_metrics(
    window_months: Optional[list] = None,
    cache_root: str = DEFAULT_CACHE_ROOT,
    token_path: str = DEFAULT_TOKEN_PATH,
    members_path: str = DEFAULT_MEMBERS_PATH,
) -> dict:
    """Fetch completed Shortcut stories and bucket story points by
    developer and month.

    Returns a dict keyed by roster handle, each value a dict keyed by
    month (YYYY-MM) with a ``MonthlyShortcutMetrics`` dataclass::

        {
            "amedwishpond": {
                "2025-07": MonthlyShortcutMetrics(story_points=15, stories_completed=8),
                ...
            },
            ...
        }

    Stories with no estimate are counted in ``stories_completed`` but
    contribute 0 to ``story_points``. Stories with no matching roster
    owner are silently excluded (non-roster members, bots, etc.).

    Caches the raw API response to ``<cache_root>/shortcut/stories.json``
    so re-runs without ``--refresh`` skip the API call entirely.
    """
    months = window_months if window_months is not None else default_window_months()
    cache_dir = _cache_path(cache_root)
    cache_file = cache_dir / "stories.json"

    # Try cache first
    if cache_file.is_file():
        logger.info("Shortcut: using cached stories from %s", cache_file)
        with open(cache_file, encoding="utf-8") as f:
            stories = json.load(f)
    else:
        logger.info("Shortcut: fetching completed stories from API")
        token = _load_token(token_path)
        # Search from the first month's start to the last month's end
        completed_at_start = f"{months[0]}-01T00:00:00Z"
        # Calculate the end of the last month
        year, mon = int(months[-1][:4]), int(months[-1][5:7])
        if mon == 12:
            end_year, end_mon = year + 1, 1
        else:
            end_year, end_mon = year, mon + 1
        completed_at_end = f"{end_year:04d}-{end_mon:02d}-01T00:00:00Z"

        stories = _search_completed_stories(token, completed_at_start, completed_at_end)

        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(stories, f, indent=2)

    # Load member UUID → handle map
    members_map = _load_members_map(members_path)
    uuid_set = set(members_map.keys())

    # Bucket stories by developer and month
    result: dict[str, dict[str, MonthlyShortcutMetrics]] = defaultdict(
        lambda: defaultdict(lambda: MonthlyShortcutMetrics())
    )

    for story in stories:
        month = _month_from_completed(story.get("completed_at"))
        if month is None or month not in set(months):
            continue

        estimate = story.get("estimate") or 0
        owner_ids = story.get("owner_ids", [])

        for owner_id in owner_ids:
            if owner_id in uuid_set:
                handle = members_map[owner_id]
                m = result[handle][month]
                m.story_points += estimate
                m.stories_completed += 1

    return dict(result)
