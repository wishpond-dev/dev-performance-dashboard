"""Shared fixtures for the site/build.py test suite (TASK-004-015).

A synthetic, minimal-but-valid metrics.json fixture is used for build.py's
own logic tests (determinism, self-containment, escaping) so those tests
never depend on data/metrics.json existing or the collector having been
run -- build.py itself makes no network calls, so this keeps the suite
CI-safe, matching tests/collector/conftest.py's git_repo fixture, which
follows the same "don't depend on live/real state" reasoning for the
collector suite. Tests that specifically want to exercise the real,
collector-produced data/metrics.json use the `real_metrics_path` fixture
and skip themselves when that file is absent.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_DIR = REPO_ROOT / "site"
BUILD_SCRIPT_PATH = SITE_DIR / "build.py"
REAL_METRICS_PATH = REPO_ROOT / "data" / "metrics.json"

MINIMAL_METRICS = {
    "generated_at": "2026-07-19T00:00:00Z",
    "window": {"start": "2025-07", "end": "2026-06", "months": ["2025-07"]},
    "roster": [{"name": "Test Dev", "handle": "testdev"}],
    "repos": [{"name": "repo-a", "org": "test-org"}],
    "score_weights": {"commits": 0.2, "prs": 0.3, "reviews": 0.25, "tests": 0.15, "ci": 0.1},
    "team": {"monthly": [], "h1": {}, "h2": {}, "dora": {"mttr": None}},
    "developers": [],
    "repo_breakdown": [],
}


def _load_build_module():
    spec = importlib.util.spec_from_file_location("site_build", BUILD_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MetricsFixture:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data


@pytest.fixture(scope="session")
def build_module():
    return _load_build_module()


@pytest.fixture
def site_dir() -> Path:
    return SITE_DIR


@pytest.fixture
def metrics_fixture(tmp_path) -> MetricsFixture:
    data = copy.deepcopy(MINIMAL_METRICS)
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return MetricsFixture(path=path, data=data)


@pytest.fixture
def real_metrics_path() -> Path:
    if not REAL_METRICS_PATH.is_file():
        pytest.skip("data/metrics.json not present -- run `python3 -m collector.collect` first")
    return REAL_METRICS_PATH
