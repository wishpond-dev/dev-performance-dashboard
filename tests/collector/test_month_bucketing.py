"""IDENT-GIT-007 -- author-date on a UTC month boundary lands in the
correct month bucket. See test-plan-identity.md. (This covers only
git_source.py's own author-date -> month assignment, not bucketing.py's
cross-source git+GitHub merge -- see test-plan-identity.md GAP-2.)
"""

from __future__ import annotations

from collector.git_source import collect_repo_git_metrics
from collector.identity import IdentityMap, Person

WINDOW = ["2025-07", "2025-08"]

ROSTER = [Person("Alejandro Medina", "amedwishpond")]


def _identity_map() -> IdentityMap:
    return IdentityMap(
        roster=ROSTER,
        aliases={"alejandro@example.com": "amedwishpond"},
        github_logins={},
        bots=frozenset(),
        departed_emails={},
        departed_count=0,
        people_by_handle={p.handle: p for p in ROSTER},
        unmapped_reasons={},
    )


def test_ident_git_007_utc_boundary_lands_in_correct_month_bucket(git_repo):
    # 23:59:59 UTC on the last second of July -> still July.
    git_repo.commit(
        message="end of july utc", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-07-31T23:59:59+00:00", files={"a.txt": "x\n"},
    )
    # 00:00:00 UTC on the first second of August -> already August.
    git_repo.commit(
        message="start of august utc", author_name="Alejandro Medina", author_email="alejandro@example.com",
        date="2025-08-01T00:00:00+00:00", files={"b.txt": "x\n"},
    )
    # Non-UTC offset that rolls over the boundary once converted to UTC:
    # 20:00 on July 31 at UTC-07:00 == 03:00 UTC on August 1.
    git_repo.commit(
        message="non-utc offset rolls into august", author_name="Alejandro Medina",
        author_email="alejandro@example.com", date="2025-07-31T20:00:00-07:00", files={"c.txt": "x\n"},
    )

    result = collect_repo_git_metrics(str(git_repo.path), _identity_map(), window_months=WINDOW)

    assert result["amedwishpond"]["2025-07"]["commits"] == 1
    assert result["amedwishpond"]["2025-08"]["commits"] == 2
