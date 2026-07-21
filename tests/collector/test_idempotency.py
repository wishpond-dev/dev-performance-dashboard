"""METRIC-IDEMP-001..003 -- idempotency/determinism tests for collect.py's
full pipeline. Exercises a real (tiny, on-disk) git fixture repo via the
shared `git_repo` fixture (conftest.py) so git_source.py's own commit walk
runs for real, but stands in a FakeSession for all GitHub HTTP traffic (no
live network calls, per spec.md S14's "no live calls in CI" requirement)
-- the same technique test_github_source.py already uses, just wired
through both github_source.GitHubClient and dora.GitHubClient (dora.py
imports GitHubClient directly, so both module-level names need patching).
"""

from __future__ import annotations

import json

from collector import collect
from collector import dora as dora_module
from collector import github_source
from collector.github_source import GitHubClient
from collector.identity import EXPECTED_ROSTER

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}
GENERATED_AT = "2026-07-19T00:00:00Z"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeSession:
    """Every GET returns an empty closed-PR list -- enough to exercise the
    real fetch/cache path with zero PRs to walk, and `calls` makes "zero
    additional calls" directly assertable (mirrors test_github_source.py's
    own FakeSession)."""

    def __init__(self):
        self.calls: list = []
        self.headers: dict = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(status_code=200, json_data=[])


def _patch_github_client(monkeypatch, session: FakeSession) -> None:
    def factory(token=None, **kwargs):
        return GitHubClient(token=token, session=session, sleep_fn=lambda seconds: None)

    monkeypatch.setattr(github_source, "GitHubClient", factory)
    monkeypatch.setattr(dora_module, "GitHubClient", factory)


def _write_config(tmp_path, repo_path, weights=None):
    repos = [{
        "name": "repo-a", "org": "test-org", "full_name": "test-org/repo-a",
        "local_path": str(repo_path), "default_branch": None,
    }]
    repos_path = tmp_path / "repos.json"
    repos_path.write_text(json.dumps({"repos": repos}), encoding="utf-8")

    identity = {
        "roster": [{"name": name, "handle": handle} for name, handle in EXPECTED_ROSTER],
        "aliases": {"alejandro@example.com": "amedwishpond"},
        "github_logins": {},
        "bots": [],
        "departed": [],
        "unmapped": [],
    }
    identity_path = tmp_path / "identity-map.json"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    weights_path = tmp_path / "score-weights.json"
    weights_path.write_text(json.dumps(weights or WEIGHTS), encoding="utf-8")

    return repos_path, identity_path, weights_path


def test_metric_idemp_001_repeated_run_produces_byte_identical_metrics_json(tmp_path, git_repo, monkeypatch):
    git_repo.commit(
        message="c1", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00", files={"a.py": "x\n"},
    )
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    _patch_github_client(monkeypatch, FakeSession())
    repos_path, identity_path, weights_path = _write_config(tmp_path, git_repo.path)
    data_dir = tmp_path / "out"

    collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(data_dir), generated_at=GENERATED_AT,
    )
    first_bytes = (data_dir / "metrics.json").read_bytes()

    collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(data_dir), generated_at=GENERATED_AT,
    )
    second_bytes = (data_dir / "metrics.json").read_bytes()

    assert first_bytes == second_bytes
    assert len(first_bytes) > 0


def test_metric_idemp_002_second_run_makes_zero_additional_github_calls(tmp_path, git_repo, monkeypatch):
    git_repo.commit(
        message="c1", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00", files={"a.py": "x\n"},
    )
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    session = FakeSession()
    _patch_github_client(monkeypatch, session)
    repos_path, identity_path, weights_path = _write_config(tmp_path, git_repo.path)
    data_dir = tmp_path / "out"

    collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(data_dir), generated_at=GENERATED_AT,
    )
    calls_after_first_run = len(session.calls)
    assert calls_after_first_run > 0  # sanity: the first run actually hit the network once

    collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(data_dir), generated_at=GENERATED_AT,
    )
    assert len(session.calls) == calls_after_first_run


def test_metric_idemp_003_weight_change_changes_composite_not_raw_counts(tmp_path, git_repo, monkeypatch):
    git_repo.commit(
        message="c1", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-05T10:00:00+00:00", files={"a.py": "x\n"},
    )
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    _patch_github_client(monkeypatch, FakeSession())
    repos_path, identity_path, weights_path = _write_config(tmp_path, git_repo.path)

    doc1 = collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(tmp_path / "out1"), generated_at=GENERATED_AT,
    )

    reweighted = dict(WEIGHTS, commits=0.05, prs=0.45)
    weights_path.write_text(json.dumps(reweighted), encoding="utf-8")
    doc2 = collect.run_collect(
        repos_path=str(repos_path), identity_map_path=str(identity_path),
        weights_path=str(weights_path), data_dir=str(tmp_path / "out2"), generated_at=GENERATED_AT,
    )

    assert doc1["score_weights"] != doc2["score_weights"]

    for dev1, dev2 in zip(doc1["developers"], doc2["developers"]):
        assert dev1["handle"] == dev2["handle"]
        for row1, row2 in zip(dev1["monthly"], dev2["monthly"]):
            assert row1["commits"] == row2["commits"]
            assert row1["prs_merged"] == row2["prs_merged"]
            assert row1["reviews_given"] == row2["reviews_given"]
            assert row1["lines_added"] == row2["lines_added"]

    composites1 = [dev["composite"]["score"] for dev in doc1["developers"]]
    composites2 = [dev["composite"]["score"] for dev in doc2["developers"]]
    assert composites1 != composites2
