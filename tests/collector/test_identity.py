"""Unit tests for collector/identity.py -- see test-plan-identity.md
IDENT-MAP-001..009 and IDENT-EDGE-002."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from collector.identity import (
    IdentityMap,
    IdentityMapError,
    Person,
    RawAuthor,
    load_identity_map,
    resolve_author,
    resolve_authors,
    validate_identity_map,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

ROSTER = [
    Person("Alejandro Medina", "amedwishpond"),
    Person("Amir Pourjabbari", "mc4future"),
    Person("Gabriel Laporte", "gabriellaporte-wp"),
    Person("Igor Negrizoli", "igorFNegrizoli"),
    Person("Paulo Mellin", "pmellingimenes"),
    Person("Umer Boostani", "umerbhattiboostani"),
    Person("David Moradi", "davidmoradi"),
    Person("Marcelo Negrini", "marcelon-salescloser"),
]


def _make_identity_map(**overrides) -> IdentityMap:
    roster = overrides.pop("roster", ROSTER)
    defaults = dict(
        roster=roster,
        aliases={
            "amedina.dev@gmail.com": "amedwishpond",
            "alejandro.m@salescloser.ai": "amedwishpond",
            "gabriel.laporte@wishpond.com": "gabriellaporte-wp",
        },
        github_logins={
            "mc4future": "mc4future",
            "alejandromedina": "amedwishpond",
        },
        bots=frozenset(["cursor[bot]", "github-actions[bot]", "wishpond"]),
        departed_emails={"carlos.sanchez@wishpond.com": "Carlos Sanchez"},
        departed_count=1,
        people_by_handle={p.handle: p for p in roster},
        unmapped_reasons={"kobe@salescloser1": "no name/roster match"},
    )
    defaults.update(overrides)
    return IdentityMap(**defaults)


# ---------------------------------------------------------------------------
# IDENT-MAP-001: known alias email -> correct roster member
# ---------------------------------------------------------------------------
def test_ident_map_001_known_alias_resolves_to_roster_member():
    im = _make_identity_map()
    result = resolve_author(im, name="Alejandro Medina", email="amedina.dev@gmail.com")
    assert result.category == "roster"
    assert result.person is not None
    assert result.person.handle == "amedwishpond"
    assert result.person.name == "Alejandro Medina"


# ---------------------------------------------------------------------------
# IDENT-MAP-002: two different emails for the same person both resolve to
# the same Person
# ---------------------------------------------------------------------------
def test_ident_map_002_multiple_aliases_same_person():
    im = _make_identity_map()
    r1 = resolve_author(im, name="Alejandro Medina", email="amedina.dev@gmail.com")
    r2 = resolve_author(im, name="Alejandro M", email="alejandro.m@salescloser.ai")
    assert r1.category == "roster"
    assert r2.category == "roster"
    assert r1.person == r2.person
    assert r1.person.handle == "amedwishpond"


# ---------------------------------------------------------------------------
# IDENT-MAP-003: github login resolves correctly even with unrelated/unknown
# email
# ---------------------------------------------------------------------------
def test_ident_map_003_github_login_resolves_with_unrelated_email():
    im = _make_identity_map()
    result = resolve_author(
        im, name="Amir P", email="totally-unrelated@example.com", login="mc4future"
    )
    assert result.category == "roster"
    assert result.person.handle == "mc4future"


# ---------------------------------------------------------------------------
# IDENT-MAP-004: bots filtered by name and by login
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bot_name", ["cursor[bot]", "github-actions[bot]", "wishpond"])
def test_ident_map_004_bot_names_filtered(bot_name):
    im = _make_identity_map()
    result = resolve_author(im, name=bot_name, email="bot@example.com")
    assert result.category == "bot"
    assert result.person is None


def test_ident_map_004_bot_matched_via_login():
    im = _make_identity_map()
    result = resolve_author(im, name="Some Bot Display Name", login="cursor[bot]")
    assert result.category == "bot"
    assert result.person is None


# ---------------------------------------------------------------------------
# IDENT-MAP-005: departed dev email -> category "departed", person None
# ---------------------------------------------------------------------------
def test_ident_map_005_departed_dev_email():
    im = _make_identity_map()
    result = resolve_author(im, name="Carlos Sanchez", email="carlos.sanchez@wishpond.com")
    assert result.category == "departed"
    assert result.person is None
    assert "Carlos Sanchez" in result.reason


# ---------------------------------------------------------------------------
# IDENT-MAP-006: no match anywhere -> unmapped, logged, reason present
# ---------------------------------------------------------------------------
def test_ident_map_006_unmapped_author_logged_with_reason(caplog):
    im = _make_identity_map()
    with caplog.at_level(logging.WARNING):
        result = resolve_author(im, name="Random Person", email="random@nowhere.example")
    assert result.category == "unmapped"
    assert result.person is None
    assert result.reason
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    formatted = warnings[0].getMessage()
    assert "unmapped author" in formatted
    assert "random@nowhere.example" in formatted


def test_ident_map_006_unmapped_uses_known_unmapped_reason():
    im = _make_identity_map()
    result = resolve_author(im, name="Kobe", email="kobe@salescloser1")
    assert result.category == "unmapped"
    assert result.reason == "no name/roster match"


# ---------------------------------------------------------------------------
# IDENT-MAP-007: validate_identity_map positive + negative cases
# ---------------------------------------------------------------------------
def test_ident_map_007_valid_map_passes_validation():
    im = _make_identity_map()
    validate_identity_map(im)  # should not raise


def test_ident_map_007_wrong_roster_order_raises():
    reordered = list(reversed(ROSTER))
    im = _make_identity_map(roster=reordered, people_by_handle={p.handle: p for p in reordered})
    with pytest.raises(IdentityMapError):
        validate_identity_map(im)


def test_ident_map_007_extra_roster_person_raises():
    extra_roster = ROSTER + [Person("Extra Person", "extrahandle")]
    im = _make_identity_map(
        roster=extra_roster, people_by_handle={p.handle: p for p in extra_roster}
    )
    with pytest.raises(IdentityMapError):
        validate_identity_map(im)


def test_ident_map_007_alias_to_nonexistent_handle_raises():
    im = _make_identity_map(aliases={"someone@example.com": "not-a-real-handle"})
    with pytest.raises(IdentityMapError):
        validate_identity_map(im)


def test_ident_map_007_github_login_to_nonexistent_handle_raises():
    im = _make_identity_map(github_logins={"someone": "not-a-real-handle"})
    with pytest.raises(IdentityMapError):
        validate_identity_map(im)


def test_ident_map_007_alias_and_departed_conflict_raises():
    im = _make_identity_map(
        aliases={"conflict@example.com": "amedwishpond"},
        departed_emails={"conflict@example.com": "Someone Departed"},
    )
    with pytest.raises(IdentityMapError):
        validate_identity_map(im)


def test_ident_map_007_load_identity_map_detects_conflicting_alias_duplicates(tmp_path):
    data = {
        "roster": [{"name": p.name, "handle": p.handle} for p in ROSTER],
        "aliases": {
            "Foo@Example.com": "amedwishpond",
            "foo@example.com ": "mc4future",
        },
        "github_logins": {},
        "bots": [],
        "departed": [],
        "unmapped": [],
    }
    config_path = tmp_path / "identity-map.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(IdentityMapError):
        load_identity_map(str(config_path))


# ---------------------------------------------------------------------------
# IDENT-MAP-008: case/whitespace normalization
# ---------------------------------------------------------------------------
def test_ident_map_008_email_case_whitespace_normalized():
    im = _make_identity_map()
    canonical = resolve_author(im, name="Alejandro Medina", email="amedina.dev@gmail.com")
    variant = resolve_author(im, name="Alejandro Medina", email="  AMEDINA.DEV@GMAIL.COM  ")
    assert canonical.category == "roster"
    assert variant.category == "roster"
    assert canonical.person == variant.person


def test_ident_map_008_login_case_whitespace_normalized():
    im = _make_identity_map()
    canonical = resolve_author(im, name="Amir", login="mc4future")
    variant = resolve_author(im, name="Amir", login="  MC4Future  ")
    assert canonical.category == "roster"
    assert variant.category == "roster"
    assert canonical.person == variant.person


# ---------------------------------------------------------------------------
# IDENT-MAP-009: mixed batch with no cross-contamination
# ---------------------------------------------------------------------------
def test_ident_map_009_mixed_batch_resolves_each_category_correctly():
    im = _make_identity_map()
    authors = [
        RawAuthor(name="Alejandro Medina", email="amedina.dev@gmail.com"),
        RawAuthor(name="cursor[bot]", email="bot@example.com"),
        RawAuthor(name="Carlos Sanchez", email="carlos.sanchez@wishpond.com"),
        RawAuthor(name="Random Person", email="random@nowhere.example"),
    ]
    results = resolve_authors(im, authors)
    assert [r.category for r in results] == ["roster", "bot", "departed", "unmapped"]
    assert results[0].person.handle == "amedwishpond"
    assert results[1].person is None
    assert results[2].person is None
    assert results[3].person is None


# ---------------------------------------------------------------------------
# IDENT-EDGE-002: repeated unmapped author in one batch logs WARNING once
# ---------------------------------------------------------------------------
def test_ident_edge_002_repeated_unmapped_author_logged_once(caplog):
    im = _make_identity_map()
    authors = [
        RawAuthor(name="Random Person", email="random@nowhere.example"),
        RawAuthor(name="Random Person", email="random@nowhere.example"),
        RawAuthor(name="Random Person", email="random@nowhere.example"),
    ]
    with caplog.at_level(logging.WARNING):
        results = resolve_authors(im, authors)
    assert len(results) == 3
    assert all(r.category == "unmapped" for r in results)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_ident_edge_002_different_unmapped_authors_each_logged():
    im = _make_identity_map()
    authors = [
        RawAuthor(name="Random Person", email="random@nowhere.example"),
        RawAuthor(name="Another Person", email="another@nowhere.example"),
    ]
    logger = logging.getLogger("collector.identity")
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        resolve_authors(im, authors)
    finally:
        logger.removeHandler(handler)
    assert len(records) == 2


def test_ident_edge_002_same_unmapped_author_different_repo_month_each_logged():
    """The same unmapped (name, email) appearing in two different
    repo/month combinations must each log a WARNING -- dedup is scoped to
    (repo, month, name, email, login), not the whole batch/run."""
    im = _make_identity_map()
    authors = [
        RawAuthor(name="Random Person", email="random@nowhere.example", repo="repo-a", month="2026-07"),
        RawAuthor(name="Random Person", email="random@nowhere.example", repo="repo-b", month="2026-08"),
    ]
    logger = logging.getLogger("collector.identity")
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        results = resolve_authors(im, authors)
    finally:
        logger.removeHandler(handler)
    assert len(results) == 2
    assert all(r.category == "unmapped" for r in results)
    assert len(records) == 2


def test_ident_edge_002_same_unmapped_author_same_repo_month_logged_once():
    """Sanity check for the other half of the contract: repeats within the
    *same* repo/month still collapse to a single WARNING."""
    im = _make_identity_map()
    authors = [
        RawAuthor(name="Random Person", email="random@nowhere.example", repo="repo-a", month="2026-07"),
        RawAuthor(name="Random Person", email="random@nowhere.example", repo="repo-a", month="2026-07"),
    ]
    logger = logging.getLogger("collector.identity")
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        resolve_authors(im, authors)
    finally:
        logger.removeHandler(handler)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Integration test: real config/identity-map.json loads and validates
# ---------------------------------------------------------------------------
def test_integration_real_identity_map_loads_and_validates():
    config_path = REPO_ROOT / "config" / "identity-map.json"
    im = load_identity_map(str(config_path))
    validate_identity_map(im)  # should not raise

    assert len(im.roster) == 8
    assert len(im.aliases) == 17
    assert len(im.github_logins) == 10
    assert len(im.bots) == 4
    assert im.departed_count == 16
    assert len(im.departed_emails) == 25
    assert len(im.unmapped_reasons) == 10
