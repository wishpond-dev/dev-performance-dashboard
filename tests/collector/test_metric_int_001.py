"""METRIC-INT-001 -- cross-module integration test. See test-plan-metrics.md
for the exact acceptance-case wording this implements.

Feeds a small hand-written 2-repo/2-developer/2-month fixture (matching the
raw-source shape git_source.py/github_source.py already produce, per
test-plan-metrics.md GAP-3) through persist.collect_and_assemble -- which
itself orchestrates bucketing.build_consolidated_metrics ->
scoring.score_all -> dora.build_team_dora_metrics in that order -- and
asserts the resulting metrics.json document validates end-to-end against
the documented schema in implementation-plan.md S3.

Window note: scoring.py's half_year_months() hard-requires exactly 12
months (H1/H2 only make sense for the full annual window -- it raises
ScoringError otherwise), so collect_and_assemble cannot be run against a
literal 2-month window_months. "2-month fixture" is therefore implemented
as: the full default 12-month window (as a real run always uses), with
fixture git/GitHub activity confined to 2 of those 12 months
(_ACTIVE_MONTHS) for 2 repos and 2 developers -- the other 10 months are
left to zero-fill, which doubles as end-to-end zero-fill coverage on top
of the schema-validity check.

Repos use nonexistent local_path values on purpose: dora.py's own
documented design degrades a repo whose local_path doesn't resolve to zero
DORA signal for that repo rather than crashing (see persist.py's module
docstring), so this test never needs a real git checkout or live GitHub
call -- consistent with test_persist.py's own fixture convention.

Schema validation is hand-rolled (type/required/properties/items only,
JSON-Schema-shaped for readability) rather than via the `jsonschema`
package, which is not part of this project's requirements.txt and this
test has no business adding as a new dependency.
"""

from __future__ import annotations

import json

import pytest

from collector import persist
from collector.git_source import default_window_months
from collector.identity import IdentityMap, Person

WEIGHTS = {"commits": 0.20, "prs": 0.30, "reviews": 0.25, "tests": 0.15, "ci": 0.10}

MONTHS = default_window_months()  # full 12-month window -- required by scoring.py
_ACTIVE_MONTHS = ["2025-07", "2025-08"]  # the fixture's actual "2-month" activity window

DEV1 = Person(name="QA Int Dev One", handle="qa-int-dev1")
DEV2 = Person(name="QA Int Dev Two", handle="qa-int-dev2")
ROSTER = [DEV1, DEV2]

REPO_A = {
    "name": "repo-a", "org": "test-org", "full_name": "test-org/repo-a",
    "local_path": "/nonexistent/repo-a", "default_branch": "main", "merge_strategy": "merge",
}
REPO_B = {
    "name": "repo-b", "org": "test-org", "full_name": "test-org/repo-b",
    "local_path": "/nonexistent/repo-b", "default_branch": "main", "merge_strategy": "squash",
}
REPOS = [REPO_A, REPO_B]

# Every field the documented MergedMonthlyMetrics row (implementation-plan.md
# S3 / METRIC-BUCKET-006) must carry.
_MONTHLY_ROW_FIELDS = {
    "month", "commits", "pr_commits", "lines_added", "lines_removed", "net", "active_days",
    "test_touch_rate", "prs_merged", "cycle_time_days", "reviews_given",
    "review_turnaround_hours", "change_request_rate", "ci_pass_rate", "composite",
}


def _validate_schema(instance, schema, path="$"):
    """Minimal JSON-Schema-shaped validator: type/required/properties/items
    only -- exactly the subset this test needs to check
    implementation-plan.md S3's documented `metrics.json` shape without
    pulling in the `jsonschema` package as a new dependency."""
    expected_type = schema.get("type")
    if expected_type == "object":
        assert isinstance(instance, dict), f"{path}: expected object, got {type(instance).__name__}"
        for key in schema.get("required", []):
            assert key in instance, f"{path}: missing required key {key!r}"
        for key, subschema in schema.get("properties", {}).items():
            if key in instance:
                _validate_schema(instance[key], subschema, f"{path}.{key}")
    elif expected_type == "array":
        assert isinstance(instance, list), f"{path}: expected array, got {type(instance).__name__}"
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                _validate_schema(item, item_schema, f"{path}[{i}]")
    elif expected_type == "string":
        assert isinstance(instance, str), f"{path}: expected string, got {type(instance).__name__}"


METRICS_JSON_SCHEMA = {
    "type": "object",
    "required": [
        "generated_at", "window", "roster", "repos", "score_weights",
        "team", "developers", "repo_breakdown",
    ],
    "properties": {
        "generated_at": {"type": "string"},
        "window": {
            "type": "object",
            "required": ["start", "end", "months"],
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "months": {"type": "array", "items": {"type": "string"}},
            },
        },
        "roster": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "handle", "initials"],
                "properties": {
                    "name": {"type": "string"},
                    "handle": {"type": "string"},
                    "initials": {"type": "string"},
                },
            },
        },
        "repos": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "org", "default_branch", "merge_strategy"],
            },
        },
        "score_weights": {
            "type": "object",
            "required": ["commits", "prs", "reviews", "tests", "ci"],
        },
        "team": {
            "type": "object",
            "required": ["monthly", "h1", "h2", "dora"],
            "properties": {
                "monthly": {"type": "array"},
                "dora": {
                    "type": "object",
                    "required": [
                        "deploy_frequency_per_month", "lead_time_days",
                        "change_failure_rate", "mttr",
                    ],
                },
            },
        },
        "developers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "name", "handle", "initials", "composite", "review_share",
                    "monthly", "h1", "h2",
                ],
                "properties": {
                    "composite": {
                        "type": "object",
                        "required": ["score", "signals"],
                        "properties": {
                            "signals": {
                                "type": "object",
                                "required": ["commits", "prs", "reviews", "tests", "ci"],
                            },
                        },
                    },
                    "monthly": {"type": "array"},
                },
            },
        },
        "repo_breakdown": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["repo", "commits", "prs", "reviews", "ci_pass_rate", "active_devs"],
            },
        },
    },
}


def _git_row(month, **overrides):
    row = {
        "month": month, "commits": 0, "lines_added": 0, "lines_removed": 0,
        "net": 0, "active_days": 0, "test_touch_rate": 0.0,
    }
    row.update(overrides)
    return row


def _github_row(month, **overrides):
    row = {
        "month": month, "prs_merged": 0, "cycle_time_days": 0.0, "reviews_given": 0,
        "review_turnaround_hours": 0.0, "change_request_rate": 0.0, "ci_pass_rate": None,
    }
    row.update(overrides)
    return row


def _identity_map() -> IdentityMap:
    return IdentityMap(
        roster=ROSTER,
        aliases={f"{p.handle}@example.com": p.handle for p in ROSTER},
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER},
        unmapped_reasons={},
    )


def _fixture_git_metrics_by_repo():
    # Only _ACTIVE_MONTHS (2025-07, 2025-08) carry data -- every other one
    # of the 12 window months is left absent here on purpose, so
    # merge_repo_metrics's own zero-fill fallback (bucketing.py) fills them,
    # exercising that path as part of this same end-to-end test.
    return {
        "repo-a": {
            "qa-int-dev1": {
                "2025-07": _git_row("2025-07", commits=10, lines_added=400, lines_removed=80,
                                     net=320, active_days=6, test_touch_rate=0.4),
                "2025-08": _git_row("2025-08", commits=6, lines_added=150, lines_removed=20,
                                     net=130, active_days=4, test_touch_rate=0.3),
            },
            "qa-int-dev2": {
                "2025-07": _git_row("2025-07", commits=3, lines_added=90, lines_removed=10,
                                     net=80, active_days=2, test_touch_rate=0.2),
            },
        },
        "repo-b": {
            "qa-int-dev1": {
                "2025-08": _git_row("2025-08", commits=2, lines_added=40, lines_removed=5,
                                     net=35, active_days=1, test_touch_rate=0.5),
            },
            "qa-int-dev2": {
                "2025-07": _git_row("2025-07", commits=8, lines_added=300, lines_removed=60,
                                     net=240, active_days=5, test_touch_rate=0.25),
                "2025-08": _git_row("2025-08", commits=4, lines_added=100, lines_removed=15,
                                     net=85, active_days=3, test_touch_rate=0.35),
            },
        },
    }


def _fixture_github_metrics_by_repo():
    return {
        "repo-a": {
            "qa-int-dev1": {
                "2025-07": _github_row("2025-07", prs_merged=2, cycle_time_days=1.5,
                                        reviews_given=3, review_turnaround_hours=4.0,
                                        change_request_rate=0.1, ci_pass_rate=0.95),
                "2025-08": _github_row("2025-08", prs_merged=1, cycle_time_days=2.0,
                                        reviews_given=1, review_turnaround_hours=6.0,
                                        change_request_rate=0.0, ci_pass_rate=1.0),
            },
            "qa-int-dev2": {
                "2025-07": _github_row("2025-07", prs_merged=1, cycle_time_days=3.0,
                                        reviews_given=0, review_turnaround_hours=0.0,
                                        change_request_rate=0.2, ci_pass_rate=0.8),
            },
        },
        "repo-b": {
            "qa-int-dev1": {
                "2025-08": _github_row("2025-08", prs_merged=1, cycle_time_days=0.9,
                                        reviews_given=2, review_turnaround_hours=2.5,
                                        change_request_rate=0.0, ci_pass_rate=1.0),
            },
            "qa-int-dev2": {
                "2025-07": _github_row("2025-07", prs_merged=2, cycle_time_days=1.1,
                                        reviews_given=4, review_turnaround_hours=3.2,
                                        change_request_rate=0.05, ci_pass_rate=0.9),
                "2025-08": _github_row("2025-08", prs_merged=2, cycle_time_days=1.3,
                                        reviews_given=2, review_turnaround_hours=5.5,
                                        change_request_rate=0.1, ci_pass_rate=0.85),
            },
        },
    }


@pytest.fixture(scope="module")
def pipeline_document():
    document, consolidated = persist.collect_and_assemble(
        REPOS,
        _identity_map(),
        _fixture_git_metrics_by_repo(),
        _fixture_github_metrics_by_repo(),
        WEIGHTS,
        window_months=MONTHS,
        generated_at="2026-07-20T00:00:00Z",
    )
    return document, consolidated


def test_metric_int_001_pipeline_produces_schema_valid_document(pipeline_document):
    document, _consolidated = pipeline_document

    # 1. Full document round-trips through JSON (this is what actually
    # lands in data/metrics.json on disk) before any structural check.
    reloaded = json.loads(json.dumps(document))

    # 2. Validates against the documented schema (implementation-plan.md S3)
    _validate_schema(reloaded, METRICS_JSON_SCHEMA)

    # 3. No required top-level key missing, no field silently dropped.
    assert set(reloaded.keys()) == {
        "generated_at", "window", "roster", "repos", "score_weights",
        "team", "developers", "repo_breakdown",
    }


def test_metric_int_001_window_is_full_year_with_fixture_confined_to_2_months(pipeline_document):
    document, _ = pipeline_document
    assert document["window"] == {"start": "2025-07", "end": "2026-06", "months": MONTHS}
    assert len(document["window"]["months"]) == 12
    assert set(_ACTIVE_MONTHS).issubset(set(document["window"]["months"]))


def test_metric_int_001_roster_is_the_2_developer_fixture_in_order(pipeline_document):
    document, _ = pipeline_document
    assert [d["handle"] for d in document["roster"]] == ["qa-int-dev1", "qa-int-dev2"]
    assert [d["handle"] for d in document["developers"]] == ["qa-int-dev1", "qa-int-dev2"]


def test_metric_int_001_repos_present_in_order_with_branch_info(pipeline_document):
    document, _ = pipeline_document
    assert [r["name"] for r in document["repos"]] == ["repo-a", "repo-b"]
    assert document["repos"][0] == {
        "name": "repo-a", "org": "test-org", "default_branch": "main", "merge_strategy": "merge",
    }
    assert document["repos"][1]["merge_strategy"] == "squash"


def test_metric_int_001_every_developer_monthly_row_has_every_documented_field(pipeline_document):
    document, _ = pipeline_document
    for dev in document["developers"]:
        assert [row["month"] for row in dev["monthly"]] == MONTHS
        for row in dev["monthly"]:
            assert set(row.keys()) == _MONTHLY_ROW_FIELDS
            # composite is always a real number by this stage (scoring.py
            # has already run) -- never left as bucketing.py's None sentinel.
            assert row["composite"] is not None


def test_metric_int_001_non_active_months_are_zero_filled_not_missing(pipeline_document):
    document, _ = pipeline_document
    by_handle = {d["handle"]: d for d in document["developers"]}
    for dev in by_handle.values():
        for row in dev["monthly"]:
            if row["month"] not in _ACTIVE_MONTHS:
                assert row["commits"] == 0
                assert row["prs_merged"] == 0
                assert row["ci_pass_rate"] is None


def test_metric_int_001_non_zero_activity_flows_through_to_developers(pipeline_document):
    document, _ = pipeline_document
    by_handle = {d["handle"]: d for d in document["developers"]}
    dev1_rows = {row["month"]: row for row in by_handle["qa-int-dev1"]["monthly"]}
    assert dev1_rows["2025-07"]["commits"] == 10  # repo-a only; repo-b dev1 is absent in 2025-07
    dev2_rows = {row["month"]: row for row in by_handle["qa-int-dev2"]["monthly"]}
    assert dev2_rows["2025-07"]["commits"] == 3 + 8  # repo-a + repo-b


def test_metric_int_001_repo_breakdown_covers_both_repos(pipeline_document):
    document, _ = pipeline_document
    assert [r["repo"] for r in document["repo_breakdown"]] == ["repo-a", "repo-b"]
    for row in document["repo_breakdown"]:
        assert row["active_devs"] == 2  # both fixture developers touch both repos overall


def test_metric_int_001_dora_present_and_mttr_is_json_null(pipeline_document):
    document, _ = pipeline_document
    dora = document["team"]["dora"]
    assert set(dora.keys()) == {
        "deploy_frequency_per_month", "lead_time_days", "change_failure_rate", "mttr",
    }
    # Fixture repos use nonexistent local_path values on purpose -- dora.py
    # degrades to zero signal per repo rather than crashing (see module
    # docstring), which is itself part of what this integration test proves.
    assert dora["mttr"] is None


def test_metric_int_001_score_weights_match_input_exactly(pipeline_document):
    document, _ = pipeline_document
    assert document["score_weights"] == WEIGHTS
