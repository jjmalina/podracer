"""Podcast artwork: generated placeholder tiles.

Local caching of real cover art lives in `download.py` (`download_artwork`) and the
worker; this module holds the pure, dependency-free placeholder generator used when a
podcast has no cached cover (or never had a usable `artwork_url`). Kept separate so it
can be rendered and tested without the web stack.
"""

from markupsafe import escape

# Muted, instrument-panel tints — chosen to sit on the near-black UI. Indexed by
# `podcast.id % len`, so sequential ids spread across the palette and a list of
# missing covers reads as distinct tiles. Greens kept to muted olive per the design
# language (saturated green was rejected for the accent).
PLACEHOLDER_TINTS = [
    "#8a5a2b",  # amber-brown
    "#9c6b2e",  # ochre
    "#7d6a2f",  # brass
    "#566b3c",  # olive
    "#3c6b62",  # teal
    "#3c5a7d",  # slate-blue
    "#5b4a7d",  # muted violet
    "#7d3c5a",  # mauve
    "#8a4433",  # rust
    "#6b5440",  # clay
]


def placeholder_initial(title: str | None) -> str:
    """First character of `title`, uppercased; `?` when empty."""
    if title:
        ch = title.strip()[:1]
        if ch:
            return ch.upper()
    return "?"


def placeholder_svg(seed: int, letter: str) -> str:
    """A square SVG tile: a deterministic tint (by `seed`) with `letter` centered."""
    tint = PLACEHOLDER_TINTS[seed % len(PLACEHOLDER_TINTS)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" fill="{tint}"/>'
        '<text x="50" y="50" text-anchor="middle" dominant-baseline="central" '
        'font-family="Space Grotesk, sans-serif" font-size="46" font-weight="600" '
        f'fill="#e7e5dd" opacity="0.9">{escape(letter)}</text>'
        '</svg>'
    )
