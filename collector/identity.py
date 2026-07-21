"""Identity resolution: raw git-author and GitHub-login records -> the
9-person roster.

spec.md §7 makes `config/identity-map.json` the single source of truth for
"who is this commit/PR/review by." This module is the code that reads that
data and applies it -- new aliases are added to the JSON, never here (see
implementation-plan.md §3). Three things happen here, all driven by that
JSON file:

1. Any known alias (git author email) or GitHub login, case/whitespace
   normalized, is resolved to one of the 9 canonical roster people.
2. Bots (`cursor[bot]`, `github-actions[bot]`, `wishpond`,
   `salescloser-chart-bumper[bot]`) and departed (non-roster) developers are
   filtered out entirely -- they are never force-mapped to a roster person
   even if a name or email superficially resembles one.
3. Anything left over is logged as unmapped, with a reason, so gaps in
   identity-map.json are visible and fixable rather than silently miscounted
   (spec.md §7's "logs any unmapped author it encounters").

`validate_identity_map()` additionally checks identity-map.json's own data
against the exact 9-person roster (spec.md §3): the roster must be exactly
those 9 people in the canonical order, and every alias/login must resolve
to one of them.

`config_loader.py` is still an empty stub (implementation-plan.md §0), so --
like branch.py -- this module reads its own JSON directly rather than
depending on it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_IDENTITY_MAP_PATH = "config/identity-map.json"

# Hardcoded from spec.md §3 (implementation-plan.md §3: the roster is always
# in this same order everywhere). This is the single source of truth
# validate_identity_map() checks identity-map.json's `roster` array against
# -- it is never read from the JSON file itself, so a corrupted or
# hand-edited identity-map.json (wrong person, wrong handle, wrong order,
# missing/extra person) is caught rather than silently trusted.
EXPECTED_ROSTER: list[tuple[str, str]] = [
    ("Alejandro Medina", "amedwishpond"),
    ("Amir Pourjabbari", "mc4future"),
    ("Gabriel Laporte", "gabriellaporte-wp"),
    ("Igor Negrizoli", "igorFNegrizoli"),
    ("Jose Almada", "PepeAlmada"),
    ("Paulo Mellin", "pmellingimenes"),
    ("Umer Boostani", "umerbhattiboostani"),
    ("David Moradi", "davidmoradi"),
    ("Marcelo Negrini", "marcelon-salescloser"),
]


class IdentityMapError(RuntimeError):
    """Raised when identity-map.json fails structural validation, or (in
    load_identity_map) when it contains an internal conflict such as two
    differently-cased/whitespaced keys that normalize to the same lookup
    key but point at different handles."""


def _normalize(value: Optional[str]) -> str:
    """Case/whitespace normalization applied uniformly to every alias key,
    login key, bot name, and departed-dev email -- and to every lookup
    input -- so `"Foo@Bar.com "` and `"foo@bar.com"` are the same identity
    (task requirement: "case/whitespace-normalized" matching)."""
    return value.strip().lower() if value else ""


@dataclass(frozen=True)
class Person:
    name: str
    handle: str


@dataclass(frozen=True)
class RawAuthor:
    """One raw identity record as emitted by git log or the GitHub API,
    before resolution. `login` is only ever populated for GitHub-sourced
    records (PR/review authors); git commit authors have name+email only.

    `repo` and `month` are optional batch-dedup context (IDENT-EDGE-002):
    when a caller populates them, resolve_authors() dedupes its
    unmapped-author WARNING per (repo, month, name, email, login) instead
    of per (name, email, login) alone, so the same unmapped author showing
    up in a different repo or a different month still gets its own log
    line -- callers that don't care about that distinction (or resolve one
    repo/month at a time already) can leave these at their default \"\" and
    get the old whole-call dedup behavior."""

    name: str = ""
    email: Optional[str] = None
    login: Optional[str] = None
    repo: str = ""
    month: str = ""


@dataclass(frozen=True)
class ResolvedAuthor:
    category: str  # "roster" | "bot" | "departed" | "unmapped"
    raw: RawAuthor
    person: Optional[Person] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class IdentityMap:
    """In-memory, normalized view of identity-map.json, built once by
    load_identity_map() and then reused for every resolve_author() call."""

    roster: list[Person]
    aliases: dict[str, str]  # normalized email -> roster handle
    github_logins: dict[str, str]  # normalized login -> roster handle
    bots: frozenset[str]  # normalized bot names/logins
    departed_emails: dict[str, str]  # normalized email -> departed person's name
    departed_count: int  # number of departed *people* (not emails)
    people_by_handle: dict[str, Person]
    unmapped_reasons: dict[str, str] = field(default_factory=dict)  # normalized email -> reason


def _build_normalized_map(raw: dict[str, str], label: str) -> dict[str, str]:
    """Normalize every key in a raw email/login -> handle mapping, raising
    IdentityMapError if two distinct raw keys collapse to the same
    normalized key but disagree on the target handle -- a genuine
    conflicting/duplicate mapping (task requirement, IDENT-MAP-007)."""
    result: dict[str, str] = {}
    for key, value in raw.items():
        norm_key = _normalize(key)
        if norm_key in result and result[norm_key] != value:
            raise IdentityMapError(
                f"conflicting {label} mapping for {key!r}: "
                f"{result[norm_key]!r} vs {value!r}"
            )
        result[norm_key] = value
    return result


def load_identity_map(path: str = DEFAULT_IDENTITY_MAP_PATH) -> IdentityMap:
    """Read identity-map.json and build the normalized lookup structure
    resolve_author()/resolve_authors() use. Does NOT itself validate against
    the 9-person roster -- call validate_identity_map() on the result for
    that (kept separate so callers/tests can validate independently of
    loading, and so a load-time structural conflict raises a distinct,
    specific error from a roster-shape mismatch)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    roster = [Person(name=p["name"], handle=p["handle"]) for p in data.get("roster", [])]
    people_by_handle = {p.handle: p for p in roster}

    aliases = _build_normalized_map(data.get("aliases", {}), "alias")
    github_logins = _build_normalized_map(data.get("github_logins", {}), "github_logins")
    bots = frozenset(_normalize(name) for name in data.get("bots", []))

    departed_emails: dict[str, str] = {}
    departed_list = data.get("departed", [])
    for person_entry in departed_list:
        for email in person_entry.get("emails", []):
            departed_emails[_normalize(email)] = person_entry["name"]

    unmapped_reasons: dict[str, str] = {}
    for entry in data.get("unmapped", []):
        email = entry.get("email")
        if email:
            unmapped_reasons[_normalize(email)] = entry.get("reason", "")

    return IdentityMap(
        roster=roster,
        aliases=aliases,
        github_logins=github_logins,
        bots=bots,
        departed_emails=departed_emails,
        departed_count=len(departed_list),
        people_by_handle=people_by_handle,
        unmapped_reasons=unmapped_reasons,
    )


def validate_identity_map(identity_map: IdentityMap) -> None:
    """Structural validation of identity-map.json's own data (IDENT-MAP-007):

    - `roster` must be exactly the 9 canonical people, in the canonical
      order and spelling (EXPECTED_ROSTER) -- no typos, no missing/extra
      person, no reordering.
    - every alias email and every github_logins value must resolve to one
      of those 9 roster handles (catches a typo'd handle in either map).
    - no email is simultaneously an active alias and a departed-dev email
      (a genuine conflicting mapping -- the same person can't be both a
      current roster member and a departed non-roster human).

    Raises IdentityMapError on any violation.
    """
    actual_roster = [(p.name, p.handle) for p in identity_map.roster]
    if actual_roster != EXPECTED_ROSTER:
        raise IdentityMapError(
            f"roster does not match the expected 9-person roster.\n"
            f"  expected: {EXPECTED_ROSTER!r}\n"
            f"  actual:   {actual_roster!r}"
        )

    valid_handles = {handle for _, handle in EXPECTED_ROSTER}

    for email, handle in identity_map.aliases.items():
        if handle not in valid_handles:
            raise IdentityMapError(f"alias email {email!r} maps to unknown handle {handle!r}")

    for login, handle in identity_map.github_logins.items():
        if handle not in valid_handles:
            raise IdentityMapError(f"github login {login!r} maps to unknown handle {handle!r}")

    conflicting = set(identity_map.aliases) & set(identity_map.departed_emails)
    if conflicting:
        raise IdentityMapError(
            f"email(s) appear as both an active roster alias and a departed-dev email: {sorted(conflicting)!r}"
        )


def _classify_author(identity_map: IdentityMap, raw: RawAuthor) -> ResolvedAuthor:
    """Core resolution logic, without logging (resolve_author() and
    resolve_authors() each layer their own logging policy on top of this).

    Precedence (spec.md §7): bots are checked first, by BOTH normalized name
    and normalized login, so a bot can never fall through and be
    coincidentally matched against a roster alias or departed-dev email --
    it is filtered before any alias/login/departed lookup runs. Then GitHub
    login against `github_logins`. Then git author email against
    `aliases`. Then email against `departed_emails`. Anything left is
    "unmapped"."""
    norm_name = _normalize(raw.name)
    norm_login = _normalize(raw.login)
    norm_email = _normalize(raw.email)

    if norm_name in identity_map.bots or (norm_login and norm_login in identity_map.bots):
        return ResolvedAuthor(
            category="bot", raw=raw, reason=f"bot author ({raw.name or raw.login})"
        )

    if norm_login and norm_login in identity_map.github_logins:
        handle = identity_map.github_logins[norm_login]
        return ResolvedAuthor(category="roster", raw=raw, person=identity_map.people_by_handle[handle])

    if norm_email and norm_email in identity_map.aliases:
        handle = identity_map.aliases[norm_email]
        return ResolvedAuthor(category="roster", raw=raw, person=identity_map.people_by_handle[handle])

    if norm_email and norm_email in identity_map.departed_emails:
        departed_name = identity_map.departed_emails[norm_email]
        return ResolvedAuthor(
            category="departed", raw=raw, reason=f"departed developer: {departed_name}"
        )

    reason = identity_map.unmapped_reasons.get(norm_email, "")
    if not reason:
        reason = "no matching alias, github login, bot, or departed-dev entry"
    return ResolvedAuthor(category="unmapped", raw=raw, reason=reason)


def resolve_author(
    identity_map: IdentityMap,
    name: str = "",
    email: Optional[str] = None,
    login: Optional[str] = None,
) -> ResolvedAuthor:
    """Resolve one raw (name, email, login) tuple. Unmapped authors are
    logged at WARNING level with enough structured detail to be actionable
    (spec.md §7). For resolving many authors at once, prefer
    resolve_authors() -- it dedupes repeated unmapped-author log lines
    within the batch (IDENT-EDGE-002); this single-record function always
    logs, since it has no batch context to dedupe against."""
    raw = RawAuthor(name=name, email=email, login=login)
    result = _classify_author(identity_map, raw)
    if result.category == "unmapped":
        logger.warning(
            "unmapped author: name=%r email=%r login=%r reason=%s",
            name, email, login, result.reason,
        )
    return result


def resolve_authors(identity_map: IdentityMap, authors: Iterable[RawAuthor]) -> list[ResolvedAuthor]:
    """Resolve a batch of raw authors in one pass. Every author is
    classified independently -- no cross-contamination between categories --
    but a given unmapped (repo, month, name, email, login) identity is
    logged at WARNING at most once per call, so re-resolving many commits
    by the same unmapped author in the same repo/month does not spam the
    log. Callers that populate RawAuthor.repo/.month get that per-repo/month
    granularity; the same unmapped author in a different repo or month is a
    distinct key and still gets its own WARNING line, so gaps stay visible
    everywhere they actually occur rather than only on first sight
    (IDENT-EDGE-002)."""
    resolved: list[ResolvedAuthor] = []
    already_logged: set[tuple[str, str, str, str, str]] = set()
    for author in authors:
        result = _classify_author(identity_map, author)
        if result.category == "unmapped":
            key = (
                author.repo,
                author.month,
                _normalize(author.name),
                _normalize(author.email),
                _normalize(author.login),
            )
            if key not in already_logged:
                logger.warning(
                    "unmapped author: repo=%r month=%r name=%r email=%r login=%r reason=%s",
                    author.repo, author.month, author.name, author.email, author.login, result.reason,
                )
                already_logged.add(key)
        resolved.append(result)
    return resolved


if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IDENTITY_MAP_PATH
    loaded_map = load_identity_map(config_path)
    validate_identity_map(loaded_map)

    print(f"roster ({len(loaded_map.roster)} people):")
    for loaded_person in loaded_map.roster:
        print(f"  {loaded_person.name:20s} {loaded_person.handle}")
    print(f"aliases:          {len(loaded_map.aliases)}")
    print(f"github_logins:    {len(loaded_map.github_logins)}")
    print(f"bots:             {len(loaded_map.bots)}")
    print(f"departed (people):{loaded_map.departed_count:3d}  (emails: {len(loaded_map.departed_emails)})")
    print(f"unmapped:         {len(loaded_map.unmapped_reasons)}")
