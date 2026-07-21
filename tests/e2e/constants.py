"""Static test data shared across the tests/e2e/ suite (TASK-004-016).

Values here mirror the real, canonical data (spec.md S3, config/identity-map.json)
rather than being re-derived from the built dist/index.html, so a test that
reads the wrong thing out of the page fails loudly instead of silently
agreeing with itself.
"""
from __future__ import annotations

ROSTER_NAMES = [
    "Alejandro Medina",
    "Amir Pourjabbari",
    "Gabriel Laporte",
    "Igor Negrizoli",
    "Paulo Mellin",
    "Umer Boostani",
    "David Moradi",
    "Marcelo Negrini",
]

ROSTER_HANDLES = [
    "amedwishpond",
    "mc4future",
    "gabriellaporte-wp",
    "igorFNegrizoli",
    "pmellingimenes",
    "umerbhattiboostani",
    "davidmoradi",
    "marcelon-salescloser",
]

# Bots/non-roster markers excluded at collection time per spec.md S3 --
# must never appear in any rendered panel text.
BOT_AND_NON_ROSTER_MARKERS = [
    "cursor[bot]",
    "github-actions[bot]",
    "wishpond[bot]",
]

# The 7 scroll-spy sections (spec.md S11 items 2-8); the header band
# (S11 item 1) is checked separately since it has no <section id>.
SECTION_IDS = ["overview", "activity", "scorecards", "reviews", "dora", "repos", "h1h2"]

SIDEBAR_COLLAPSE_BREAKPOINT_PX = 900
