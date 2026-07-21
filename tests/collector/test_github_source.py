"""Unit/integration tests for collector/github_source.py -- see
test-plan-identity.md IDENT-GH-001 through IDENT-GH-011. No live GitHub API
calls: a FakeSession/FakeResponse pair (below) stands in for `requests`, and
`GitHubClient` is constructed with `session=` / `sleep_fn=` injected so
rate-limit backoff never actually sleeps in the test run.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from collector.github_source import (
    GitHubAuthError,
    GitHubClient,
    _resolve_token,
    collect_all_github_metrics,
    collect_repo_github_metrics,
)
from collector.identity import IdentityMap, Person, resolve_author

WINDOW = ["2025-07", "2025-08"]

ROSTER = [Person("Alice Roster", "alicehub"), Person("Bob Roster", "bobhub")]


def _identity_map(**overrides) -> IdentityMap:
    roster = overrides.pop("roster", ROSTER)
    defaults = dict(
        roster=roster,
        aliases={},  # deliberately empty -- attribution here is login-based only
        github_logins={"alicehub": "alicehub", "bobhub": "bobhub"},
        bots=frozenset(["ci-bot[bot]"]),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in roster},
        unmapped_reasons={},
    )
    defaults.update(overrides)
    return IdentityMap(**defaults)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    """Routes GET calls by exact URL; each call pops the next queued
    response for that URL. Raises if a URL is called with no response
    left queued -- makes "made zero additional calls" assertable directly
    from `len(session.calls)`."""

    def __init__(self):
        self.calls: list = []
        self.headers: dict = {}
        self._routes: dict = {}

    def queue(self, url: str, *responses: FakeResponse) -> None:
        self._routes.setdefault(url, []).extend(responses)

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        queue = self._routes.get(url)
        if not queue:
            raise AssertionError(f"FakeSession: no queued response left for {url}")
        return queue.pop(0)


PULLS_URL = "https://api.github.com/repos/org/repo/pulls"


def _pr(number, login, base_ref, created_at, merged_at, sha="sha" + "0"):
    return {
        "number": number,
        "user": {"login": login},
        "base": {"ref": base_ref},
        "created_at": created_at,
        "merged_at": merged_at,
        "merge_commit_sha": sha,
    }


def _repo_config(name="repo", full_name="org/repo", local_path="/nonexistent"):
    return {"name": name, "full_name": full_name, "local_path": local_path}


def _client(session):
    return GitHubClient(token="dummy", session=session, sleep_fn=lambda seconds: None)


# ---------------------------------------------------------------------------
# IDENT-GH-001: PRs merged to the default branch are counted; a PR merged to
# a non-default branch is excluded.
# ---------------------------------------------------------------------------
def test_ident_gh_001_only_default_branch_prs_counted(tmp_path):
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(
            200,
            [
                _pr(1, "alicehub", "main", "2025-07-01T10:00:00Z", "2025-07-03T10:00:00Z"),
                _pr(2, "alicehub", "some-other-branch", "2025-07-01T10:00:00Z", "2025-07-03T10:00:00Z"),
            ],
        ),
    )
    session.queue(f"{PULLS_URL}/1/reviews", FakeResponse(200, []))
    session.queue("https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []}))

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW,
        default_branch="main", client=_client(session), cache_root=str(tmp_path / "raw"),
    )

    assert result["alicehub"]["2025-07"]["prs_merged"] == 1  # only PR #1 (main), not PR #2


# ---------------------------------------------------------------------------
# IDENT-GH-002: PR cycle time (merged_at - created_at) computed correctly.
# ---------------------------------------------------------------------------
def test_ident_gh_002_cycle_time_computed_correctly(tmp_path):
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(5, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-03T12:00:00Z")]),
    )
    session.queue(f"{PULLS_URL}/5/reviews", FakeResponse(200, []))
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW,
        default_branch="main", client=_client(session), cache_root=str(tmp_path / "raw"),
    )

    # 2025-07-01T00:00 -> 2025-07-03T12:00 == 2.5 days
    assert result["alicehub"]["2025-07"]["cycle_time_days"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# IDENT-GH-003: reviews-per-PR and review turnaround computed correctly.
# ---------------------------------------------------------------------------
def test_ident_gh_003_reviews_and_turnaround_computed_correctly(tmp_path):
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(7, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-05T00:00:00Z")]),
    )
    session.queue(
        f"{PULLS_URL}/7/reviews",
        FakeResponse(
            200,
            [
                {"user": {"login": "bobhub"}, "state": "APPROVED", "submitted_at": "2025-07-02T00:00:00Z"},
                {"user": {"login": "bobhub"}, "state": "COMMENTED", "submitted_at": "2025-07-03T00:00:00Z"},
            ],
        ),
    )
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW,
        default_branch="main", client=_client(session), cache_root=str(tmp_path / "raw"),
    )

    bob_july = result["bobhub"]["2025-07"]
    assert bob_july["reviews_given"] == 2
    # turnaround: (1 day + 2 days) / 2 reviews = 1.5 days = 36 hours
    assert bob_july["review_turnaround_hours"] == pytest.approx(36.0)


# ---------------------------------------------------------------------------
# IDENT-GH-004: change-request rate = CHANGES_REQUESTED review count / total
# reviews, for a PR with a mixed review history.
# ---------------------------------------------------------------------------
def test_ident_gh_004_change_request_rate_mixed_reviews():
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(9, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-05T00:00:00Z")]),
    )
    session.queue(
        f"{PULLS_URL}/9/reviews",
        FakeResponse(
            200,
            [
                {"user": {"login": "bobhub"}, "state": "CHANGES_REQUESTED", "submitted_at": "2025-07-02T00:00:00Z"},
                {"user": {"login": "bobhub"}, "state": "APPROVED", "submitted_at": "2025-07-03T00:00:00Z"},
                {"user": {"login": "bobhub"}, "state": "COMMENTED", "submitted_at": "2025-07-03T00:00:00Z"},
                {"user": {"login": "bobhub"}, "state": "CHANGES_REQUESTED", "submitted_at": "2025-07-04T00:00:00Z"},
            ],
        ),
    )
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW,
        default_branch="main", client=_client(session),
    )

    # 2 CHANGES_REQUESTED out of 4 total reviews on alicehub's one merged PR
    assert result["alicehub"]["2025-07"]["change_request_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# IDENT-GH-005: CI pass rate from check-runs; zero check runs anywhere in a
# repo/month yields None (JSON null), never 0.
# ---------------------------------------------------------------------------
def test_ident_gh_005_ci_pass_rate_and_null_when_zero_check_runs():
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(
            200,
            [
                _pr(11, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z", sha="shaA"),
                _pr(12, "bobhub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z", sha="shaB"),
            ],
        ),
    )
    session.queue(f"{PULLS_URL}/11/reviews", FakeResponse(200, []))
    session.queue(f"{PULLS_URL}/12/reviews", FakeResponse(200, []))
    session.queue(
        "https://api.github.com/repos/org/repo/commits/shaA/check-runs",
        FakeResponse(200, {"check_runs": [{"conclusion": "success"}, {"conclusion": "failure"}]}),
    )
    session.queue(
        "https://api.github.com/repos/org/repo/commits/shaB/check-runs",
        FakeResponse(200, {"check_runs": []}),
    )

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW,
        default_branch="main", client=_client(session),
    )

    assert result["alicehub"]["2025-07"]["ci_pass_rate"] == pytest.approx(0.5)
    assert result["bobhub"]["2025-07"]["ci_pass_rate"] is None
    # never a fabricated 0 -- explicitly None, and every other month/dev with
    # no PRs/check-runs at all is also None, not 0
    assert result["alicehub"]["2025-08"]["ci_pass_rate"] is None


# ---------------------------------------------------------------------------
# IDENT-GH-006: raw API responses are cached to data/raw/; a second run
# against the same repo/month reads from cache and makes no live call.
# ---------------------------------------------------------------------------
def test_ident_gh_006_second_run_is_fully_cached(tmp_path):
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(21, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z")]),
    )
    session.queue(f"{PULLS_URL}/21/reviews", FakeResponse(200, []))
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    cache_root = str(tmp_path / "raw")
    first = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW, default_branch="main",
        client=_client(session), cache_root=cache_root,
    )
    calls_after_first_run = len(session.calls)
    assert calls_after_first_run == 3  # pulls + reviews + check-runs, exactly once each

    # Second run: nothing left queued in `session`, so any live call would
    # raise inside FakeSession.get -- a cache hit must avoid touching it.
    second = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW, default_branch="main",
        client=_client(session), cache_root=cache_root,
    )

    assert len(session.calls) == calls_after_first_run  # zero additional calls
    assert second == first


# ---------------------------------------------------------------------------
# IDENT-GH-007: on a simulated rate-limit response, the client backs off and
# retries rather than crashing or losing that repo/month's data.
# ---------------------------------------------------------------------------
def test_ident_gh_007_rate_limit_backs_off_and_retries():
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(403, json_data={"message": "rate limit"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
        FakeResponse(200, [_pr(31, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z")]),
    )
    session.queue(f"{PULLS_URL}/31/reviews", FakeResponse(200, []))
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    sleep_calls = []
    client = GitHubClient(token="dummy", session=session, sleep_fn=sleep_calls.append)

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW, default_branch="main", client=client,
    )

    assert sleep_calls, "expected at least one backoff sleep"
    assert result["alicehub"]["2025-07"]["prs_merged"] == 1  # data not lost after retry


# ---------------------------------------------------------------------------
# IDENT-GH-008: a per-repo auth failure is caught, logged, and that repo's
# metrics are zero-filled -- the run continues for the other repos.
# ---------------------------------------------------------------------------
def test_ident_gh_008_auth_failure_isolated_per_repo(caplog):
    failing_session = FakeSession()
    failing_session.queue(
        "https://api.github.com/repos/org/broken-repo/pulls",
        FakeResponse(403, json_data={"message": "Bad credentials"}, headers={}),
    )
    ok_session = FakeSession()
    ok_session.queue(
        "https://api.github.com/repos/org/good-repo/pulls",
        FakeResponse(200, [_pr(41, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z")]),
    )
    ok_session.queue(
        "https://api.github.com/repos/org/good-repo/pulls/41/reviews", FakeResponse(200, [])
    )
    ok_session.queue(
        "https://api.github.com/repos/org/good-repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    with caplog.at_level(logging.ERROR):
        broken_result = collect_repo_github_metrics(
            _repo_config(name="broken-repo", full_name="org/broken-repo"),
            _identity_map(), window_months=WINDOW, default_branch="main", client=_client(failing_session),
        )
        good_result = collect_repo_github_metrics(
            _repo_config(name="good-repo", full_name="org/good-repo"),
            _identity_map(), window_months=WINDOW, default_branch="main", client=_client(ok_session),
        )

    # broken repo: fully zero-filled, no crash
    total_broken = sum(row["prs_merged"] for months in broken_result.values() for row in months.values())
    assert total_broken == 0
    assert all(row["ci_pass_rate"] is None for months in broken_result.values() for row in months.values())

    # good repo: unaffected, real data present
    assert good_result["alicehub"]["2025-07"]["prs_merged"] == 1

    assert any("broken-repo" in r.getMessage() for r in caplog.records)


def test_ident_gh_008b_auth_error_raised_directly_by_client():
    session = FakeSession()
    session.queue(PULLS_URL, FakeResponse(403, json_data={"message": "Bad credentials"}, headers={}))
    client = _client(session)
    with pytest.raises(GitHubAuthError):
        client.get_paginated(PULLS_URL, params={"state": "closed"})


# ---------------------------------------------------------------------------
# IDENT-GH-009: a roster member has a git-commit email alias AND a GitHub
# login -- two different strings -- both mapped to the same canonical handle
# in identity-map.json. Email-based resolution (the path git_source.py uses
# for commit authors) and login-based resolution (the path github_source.py
# uses for PR/review authors) must converge on that one person: no split
# identity between the two sources.
# ---------------------------------------------------------------------------
def test_ident_gh_009_attribution_converges_email_and_login():
    identity_map = _identity_map(
        aliases={"alice.personal@example.com": "alicehub"},
        github_logins={"alicehub": "alicehub", "bobhub": "bobhub"},
    )

    # Email-based resolution: a commit authored under Alice's personal email
    # alias resolves to her roster handle.
    email_result = resolve_author(identity_map, name="Alice Personal", email="alice.personal@example.com")
    assert email_result.category == "roster"
    assert email_result.person.handle == "alicehub"

    # Login-based resolution: a PR/review authored under her GitHub login --
    # a different string than the email alias above -- must resolve to the
    # SAME roster handle, not a distinct/split identity.
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(51, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z")]),
    )
    session.queue(
        f"{PULLS_URL}/51/reviews",
        FakeResponse(200, [{"user": {"login": "bobhub"}, "state": "APPROVED", "submitted_at": "2025-07-01T12:00:00Z"}]),
    )
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    result = collect_repo_github_metrics(
        _repo_config(), identity_map, window_months=WINDOW, default_branch="main", client=_client(session),
    )

    # Both directions converge on alicehub -- no duplicate/split identity row
    # keyed by the email alias or any other handle.
    assert result["alicehub"]["2025-07"]["prs_merged"] == 1
    assert result["bobhub"]["2025-07"]["reviews_given"] == 1
    assert set(result.keys()) == {"alicehub", "bobhub"}


# ---------------------------------------------------------------------------
# IDENT-GH-010 (integration): a full fetch cycle (PRs -> reviews ->
# check-runs) against one recorded repo/month fixture produces a
# fully-populated, no-missing-keys per-developer record.
# ---------------------------------------------------------------------------
def test_ident_gh_010_full_fetch_cycle_no_missing_keys():
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(61, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-04T00:00:00Z")]),
    )
    session.queue(
        f"{PULLS_URL}/61/reviews",
        FakeResponse(
            200,
            [{"user": {"login": "bobhub"}, "state": "CHANGES_REQUESTED", "submitted_at": "2025-07-02T00:00:00Z"}],
        ),
    )
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs",
        FakeResponse(200, {"check_runs": [{"conclusion": "success"}]}),
    )

    result = collect_repo_github_metrics(
        _repo_config(), _identity_map(), window_months=WINDOW, default_branch="main", client=_client(session),
    )

    expected_keys = {
        "month", "prs_merged", "cycle_time_days", "reviews_given",
        "review_turnaround_hours", "change_request_rate", "ci_pass_rate",
    }
    for handle in ("alicehub", "bobhub"):
        for month in WINDOW:
            assert set(result[handle][month].keys()) == expected_keys

    alice_july = result["alicehub"]["2025-07"]
    assert alice_july["prs_merged"] == 1
    assert alice_july["change_request_rate"] == pytest.approx(1.0)
    assert alice_july["ci_pass_rate"] == pytest.approx(1.0)
    assert result["bobhub"]["2025-07"]["reviews_given"] == 1


def test_collect_all_github_metrics_batches_multiple_repos():
    session = FakeSession()
    session.queue(
        PULLS_URL,
        FakeResponse(200, [_pr(71, "alicehub", "main", "2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z")]),
    )
    session.queue(f"{PULLS_URL}/71/reviews", FakeResponse(200, []))
    session.queue(
        "https://api.github.com/repos/org/repo/commits/sha0/check-runs", FakeResponse(200, {"check_runs": []})
    )

    all_results = collect_all_github_metrics(
        [_repo_config()], _identity_map(), window_months=WINDOW,
    )
    # exercises the real GitHubClient()-construction path (no client override);
    # with no GH_TOKEN and no real git remote this should fail fast and
    # zero-fill rather than raise out of collect_all_github_metrics.
    assert "repo" in all_results
    assert set(all_results["repo"].keys()) == {"alicehub", "bobhub"}


# ---------------------------------------------------------------------------
# IDENT-GH-011: credential hygiene. GH_TOKEN env var is primary auth; the
# fallback token embedded in the remote URL is extracted via
# `git config --get remote.origin.url` (never `git remote -v`), and the
# token substring never appears in any captured log/stdout output.
# ---------------------------------------------------------------------------
FAKE_TOKEN = "ghp_ThisIsAFakeTokenForTestingOnly123456"  # noqa: S105 -- test fixture, not a real credential


def test_ident_gh_011_resolve_token_falls_back_to_remote_url(git_repo, monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://{FAKE_TOKEN}@github.com/org/repo.git"],
        cwd=git_repo.path, check=True, capture_output=True, text=True,
    )

    seen_commands: list = []
    real_run = subprocess.run

    def _spy_run(args, **kwargs):
        seen_commands.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    token = _resolve_token(str(git_repo.path))

    assert token == FAKE_TOKEN
    assert not any(a == ["git", "remote", "-v"] for a in seen_commands)  # never invoked
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err


def test_ident_gh_011_env_token_takes_priority(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "env-token-value")
    assert _resolve_token("/some/path") == "env-token-value"


def test_ident_gh_011_token_never_appears_in_logs_or_stdout(caplog, capsys, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    session = FakeSession()
    session.queue(PULLS_URL, FakeResponse(403, json_data={"message": "Bad credentials"}, headers={}))

    with caplog.at_level(logging.DEBUG):
        result = collect_repo_github_metrics(
            _repo_config(), _identity_map(), window_months=WINDOW, default_branch="main", client=_client(session),
        )

    captured = capsys.readouterr()
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_TOKEN not in log_text
    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err
    # auth failure still zero-fills cleanly, per IDENT-GH-008
    assert result["alicehub"]["2025-07"]["prs_merged"] == 0
