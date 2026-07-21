"""IDENT-EDGE-001 -- see test-plan-identity.md's Cross-cutting section.

AC: Running identity.py + branch.py + git_source.py + github_source.py
twice against unchanged fixtures produces byte-identical output for this
area's slice of data (idempotency, scoped to this area's modules).

Deliberately narrower than test_idempotency.py's METRIC-IDEMP-001, which
exercises collect.py's full pipeline (identity+branch+git+github+
bucketing+scoring+dora+persist) and asserts byte-identical metrics.json.
Here only this area's four modules are run and serialized -- no
bucketing.py, scoring.py, dora.py, or persist.py involved -- so a
nondeterminism introduced by a sibling-area module could pass
METRIC-IDEMP-001 today and still be masked; this test isolates the claim
to exactly the four modules this area owns.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from collector import branch, git_source, github_source
from collector.identity import EXPECTED_ROSTER, IdentityMap, Person


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeSession:
    """Fixed, recorded-style GitHub API fixture: one merged PR with one
    review and one passing check-run, authored/reviewed by two different
    roster members, on the repo's default branch. Every GET is answered
    from this fixed table -- no live network calls."""

    def __init__(self):
        self.calls: list = []
        self.headers: dict = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/pulls"):
            return FakeResponse(json_data=[
                {
                    "number": 42,
                    "user": {"login": "amedwishpond"},
                    "merged_at": "2025-08-10T12:00:00Z",
                    "created_at": "2025-08-08T09:00:00Z",
                    "base": {"ref": "main"},
                    "merge_commit_sha": "deadbeef",
                    "head": {"sha": "deadbeef"},
                },
            ])
        if "/reviews" in url:
            return FakeResponse(json_data=[
                {
                    "user": {"login": "mc4future"},
                    "state": "APPROVED",
                    "submitted_at": "2025-08-09T09:00:00Z",
                },
            ])
        if "/check-runs" in url:
            return FakeResponse(json_data={
                "check_runs": [{"conclusion": "success"}],
            })
        return FakeResponse(json_data=[])


def _build_identity_map() -> IdentityMap:
    roster = [Person(name=name, handle=handle) for name, handle in EXPECTED_ROSTER]
    return IdentityMap(
        roster=roster,
        aliases={"alejandro@example.com": "amedwishpond"},
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in roster},
        unmapped_reasons={},
    )


def _run_area_pipeline(repo_path: str) -> bytes:
    """Runs exactly this area's four modules (identity.py resolution happens
    inside git_source.py/github_source.py's calls to resolve_authors/
    resolve_author) against fixed fixtures, and returns a canonical
    serialization of their combined output."""
    identity_map = _build_identity_map()

    branch_info = branch.detect_repo(repo_path, name="repo-a")

    git_metrics = git_source.collect_repo_git_metrics(
        repo_path, identity_map, default_branch=branch_info.default_branch,
    )

    repo_dict = {"name": "repo-a", "full_name": "test-org/repo-a", "local_path": repo_path}
    github_client = github_source.GitHubClient(token="fake-token", session=FakeSession(), sleep_fn=lambda s: None)
    github_metrics = github_source.collect_repo_github_metrics(
        repo_dict, identity_map, default_branch=branch_info.default_branch, client=github_client,
    )

    combined = {
        "branch": asdict(branch_info),
        "git": git_metrics,
        "github": github_metrics,
    }
    return json.dumps(combined, sort_keys=True, indent=2).encode("utf-8")


def test_ident_edge_001_area_modules_produce_byte_identical_output_on_rerun(git_repo):
    git_repo.commit(
        message="c1", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-08-05T10:00:00+00:00", files={"a.py": "x\n"},
    )
    git_repo.checkout_new_branch("feature/x")
    git_repo.commit(
        message="c2", author_name="Amir Pourjabbari", author_email="unmapped-amir@example.com",
        date="2025-08-06T10:00:00+00:00", files={"tests/test_a.py": "y\n"},
    )
    git_repo.checkout("main")
    git_repo.merge(
        branch="feature/x", message="Merge pull request #42 from test-org/feature/x",
        author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-08-10T12:00:00+00:00",
    )

    first_bytes = _run_area_pipeline(str(git_repo.path))
    second_bytes = _run_area_pipeline(str(git_repo.path))

    assert first_bytes == second_bytes
    assert len(first_bytes) > 0
