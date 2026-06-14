#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# lang_card.py  ·  Top-languages card for the profile repo mguttmann/mguttmann
# ---------------------------------------------------------------------------
# Renders an HONEST "most used languages" SVG by aggregating the language BYTES
# reported by GitHub across the owner's OWN repositories — public AND private,
# with FORKS EXCLUDED. This is deliberately a self-written renderer (Python
# standard library only, no third-party package, no external Action) so that the
# access token is used by THIS script and nothing else.
#
# Why a self-rendered, committed card (and not github-readme-stats /api/top-langs):
#   - The public github-readme-stats Vercel instance is rate-limited / flaky and
#     cannot see private repos without handing it a PAT.
#   - This script reads private repos through the env-isolated STATS_TOKEN, sums
#     the bytes itself, and writes a static SVG that the workflow commits to the
#     `output` branch — high uptime, no render-time third-party host.
#
# TOKEN HANDLING (security-critical):
#   - The token is read ONLY from the environment variable STATS_TOKEN.
#   - It is NEVER printed, logged, or written into the SVG / any file. The
#     functions below never echo the token; only repo names and byte counts are
#     ever emitted, and the SVG contains aggregate language stats only.
#   - The workflow gates the step with `if: ${{ secrets.STATS_TOKEN != '' }}`,
#     so this script only runs when the secret is present (clean no-op skip
#     otherwise). If the script is nevertheless invoked without a token it
#     fails loud (exit 1) rather than emitting a wrong/empty card.
#
# PRIVACY: the SVGs and the README never expose private repository NAMES or URLs.
# Only the AGGREGATE language byte distribution is rendered.
#
# Output: TWO SVGs from the SAME aggregated data, written into dist/ so the
# workflow's existing push step commits both:
#   - dist/top-languages.svg        — horizontal bar card  (LANGCARD_OUTPUT)
#   - dist/top-languages-donut.svg  — donut ring + legend  (LANGDONUT_OUTPUT)
# The README embeds only the donut (in the Activity section); the bar card is
# kept generated for continuity but is no longer embedded. Deterministic:
# languages are sorted by bytes descending, then by name, so identical data
# yields byte-identical files (no pseudo-diff churn on the daily schedule).
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from html import escape
from typing import Dict, List, Tuple

API_ROOT = "https://api.github.com"
OWNER = os.environ.get("LANGCARD_OWNER", "mguttmann")
# How many languages to show explicitly before bucketing the rest into "Other".
TOP_N = int(os.environ.get("LANGCARD_TOP_N", "8"))
OUTPUT_PATH = os.environ.get("LANGCARD_OUTPUT", "dist/top-languages.svg")
# Donut card output (rendered from the SAME aggregated data as the bar card).
DONUT_OUTPUT_PATH = os.environ.get(
    "LANGDONUT_OUTPUT", "dist/top-languages-donut.svg"
)
# Cap pagination defensively (100 repos/page * 10 pages = 1000 repos).
MAX_PAGES = 10

# ---- Executive Dark Luxury theme ------------------------------------------
BG = "#0E0E13"        # midnight (card background)
SURFACE = "#14141B"   # surface (track behind bars)
COPPER = "#C8A06A"    # primary accent (title, dominant bar)
CHAMPAGNE = "#D9B583"  # secondary accent (bars)
OFFWHITE = "#ECE7DF"  # text
MUTED = "#8A8578"     # subtle text (percent / "Other")

# A small, theme-consistent palette for the bars: copper/champagne tones plus a
# few muted neutrals so distinct languages remain distinguishable WITHOUT
# importing brand colours that would clash with the dark/copper look.
BAR_COLORS = [
    "#C8A06A",  # copper
    "#D9B583",  # champagne
    "#B98E55",  # darker copper
    "#E3C79A",  # light champagne
    "#9E8B6E",  # warm taupe
    "#C2B59B",  # sand
    "#7E7866",  # muted olive-grey
    "#A89274",  # bronze
]
OTHER_COLOR = MUTED


def _request(url: str, token: str) -> Tuple[object, Dict[str, str]]:
    """Perform an authenticated GET. Returns (parsed_json, headers).

    Raises on any non-2xx status (fail-loud). The token is sent in the
    Authorization header and is never logged.
    """
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "mguttmann-lang-card")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed host)
        data = json.loads(resp.read().decode("utf-8"))
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return data, headers


def list_own_nonfork_repos(token: str) -> List[str]:
    """Return full_names of the owner's OWN repos (public+private), forks excluded.

    Uses /user/repos?affiliation=owner&visibility=all so PRIVATE repos owned by
    the authenticated user are included; entries with fork==True are dropped.
    """
    full_names: List[str] = []
    page = 1
    while page <= MAX_PAGES:
        url = (
            f"{API_ROOT}/user/repos?affiliation=owner&visibility=all"
            f"&per_page=100&page={page}&sort=full_name"
        )
        repos, _ = _request(url, token)
        if not isinstance(repos, list) or not repos:
            break
        for repo in repos:
            # Skip forks — only the owner's OWN code should count.
            if repo.get("fork"):
                continue
            name = repo.get("full_name")
            if name:
                full_names.append(name)
        if len(repos) < 100:
            break
        page += 1
    return full_names


def aggregate_language_bytes(token: str, repos: List[str]) -> Dict[str, int]:
    """Sum language bytes across the given repos via /repos/{full}/languages."""
    totals: Dict[str, int] = {}
    skipped = 0
    for full in repos:
        url = f"{API_ROOT}/repos/{full}/languages"
        try:
            langs, _ = _request(url, token)
        except urllib.error.HTTPError as exc:
            # A single inaccessible repo (e.g. just deleted) must not poison the
            # whole run, but we count it so the run stays observable.
            if exc.code in (403, 404):
                skipped += 1
                continue
            raise
        if not isinstance(langs, dict):
            continue
        for lang, size in langs.items():
            totals[lang] = totals.get(lang, 0) + int(size)
    if skipped:
        # Repo names are NOT logged here — only an aggregate count.
        print(f"note: {skipped} repo(s) returned 403/404 and were skipped",
              file=sys.stderr)
    return totals


def build_rows(totals: Dict[str, int]) -> Tuple[List[Tuple[str, int, float]], int]:
    """Return (rows, total_bytes). rows = [(name, bytes, percent)] Top-N + Other.

    Deterministic ordering: bytes descending, then name ascending.
    """
    total = sum(totals.values())
    if total <= 0:
        return [], 0
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ordered[:TOP_N]
    rest = ordered[TOP_N:]
    rows: List[Tuple[str, int, float]] = [
        (name, size, size * 100.0 / total) for name, size in top
    ]
    rest_bytes = sum(size for _, size in rest)
    if rest_bytes > 0:
        rows.append(("Other", rest_bytes, rest_bytes * 100.0 / total))
    return rows, total


def render_svg(rows: List[Tuple[str, int, float]]) -> str:
    """Render the Executive Dark Luxury top-languages SVG (horizontal bars)."""
    # Layout constants.
    width = 480
    pad_x = 24
    title_y = 40
    first_bar_y = 74
    row_h = 34          # vertical space per language row
    label_w = 118       # space reserved for the language name on the left
    pct_w = 56          # space reserved for the percent on the right
    bar_x = pad_x + label_w
    bar_max = width - bar_x - pct_w - pad_x
    bar_h = 12
    radius = 6
    height = first_bar_y + len(rows) * row_h + 6

    parts: List[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="title desc" '
        f'font-family="Segoe UI, Helvetica, Arial, sans-serif">'
    )
    parts.append(
        '<title id="title">Most used languages across public and private '
        "repositories (forks excluded)</title>"
    )
    parts.append(
        '<desc id="desc">Aggregate share of programming languages by bytes '
        "across Manuel Guttmann's own GitHub repositories, forks excluded. "
        "No private repository names are shown.</desc>"
    )
    # Card background with subtle rounded corners + 1px copper border.
    parts.append(
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="10" fill="{BG}" stroke="{COPPER}" stroke-opacity="0.35" '
        f'stroke-width="1"/>'
    )
    # Title.
    parts.append(
        f'<text x="{pad_x}" y="{title_y}" fill="{COPPER}" font-size="18" '
        f'font-weight="600">Most used languages</text>'
    )
    parts.append(
        f'<text x="{pad_x}" y="{title_y + 16}" fill="{MUTED}" font-size="10.5">'
        f'across public &amp; private repos · forks excluded</text>'
    )

    for i, (name, _size, pct) in enumerate(rows):
        row_y = first_bar_y + i * row_h
        text_baseline = row_y + bar_h
        color = OTHER_COLOR if name == "Other" else BAR_COLORS[i % len(BAR_COLORS)]
        safe_name = escape(name)
        pct_str = f"{pct:.1f}%"
        # Language label (left).
        parts.append(
            f'<text x="{pad_x}" y="{text_baseline}" fill="{OFFWHITE}" '
            f'font-size="13">{safe_name}</text>'
        )
        # Track (full-width muted bar).
        parts.append(
            f'<rect x="{bar_x}" y="{row_y}" width="{bar_max}" height="{bar_h}" '
            f'rx="{radius}" fill="{SURFACE}"/>'
        )
        # Value bar (clamped to a visible minimum so tiny shares still register).
        fill_w = max(2.0, round(bar_max * pct / 100.0, 2))
        parts.append(
            f'<rect x="{bar_x}" y="{row_y}" width="{fill_w}" height="{bar_h}" '
            f'rx="{radius}" fill="{color}"/>'
        )
        # Percent (right-aligned).
        parts.append(
            f'<text x="{width - pad_x}" y="{text_baseline}" fill="{MUTED}" '
            f'font-size="12" text-anchor="end">{pct_str}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _fmt_num(value: float) -> str:
    """Compact, deterministic number formatting for the percent labels.

    One decimal place, trailing ``.0`` kept for visual alignment in the legend.
    """
    return f"{value:.1f}"


def render_donut_svg(rows: List[Tuple[str, int, float]]) -> str:
    """Render the Executive Dark Luxury top-languages SVG as a DONUT + legend.

    Same aggregated data as :func:`render_svg`; only the visual form differs.

    Geometry: each language is one arc of a ring drawn with the stroke-dasharray
    technique on a full ``<circle>`` (circumference C). A segment of share ``p``
    gets ``stroke-dasharray="p*C  C-p*C"`` and a ``stroke-dashoffset`` equal to
    the cumulative length already consumed, so segments are laid end-to-end. The
    ring is rotated -90 degrees (via the group transform) so it starts at 12
    o'clock and runs clockwise. This is fully deterministic for identical input.
    """
    # Layout: ring on the left, legend on the right.
    width = 480
    height = 232
    pad = 24

    # Ring geometry (left side).
    cx = 128
    cy = height / 2
    radius = 70          # radius of the stroked circle (centre line of the ring)
    ring_w = 26          # ring thickness (stroke width)
    circumference = 2.0 * math.pi * radius

    parts: List[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="dtitle ddesc" '
        f'font-family="Segoe UI, Helvetica, Arial, sans-serif">'
    )
    parts.append(
        '<title id="dtitle">Most used languages across public and private '
        "repositories (forks excluded)</title>"
    )
    parts.append(
        '<desc id="ddesc">Donut chart of the aggregate share of programming '
        "languages by bytes across Manuel Guttmann's own GitHub repositories, "
        "forks excluded. No private repository names are shown.</desc>"
    )
    # Card background with subtle rounded corners + 1px copper border.
    parts.append(
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="10" fill="{BG}" stroke="{COPPER}" stroke-opacity="0.35" '
        f'stroke-width="1"/>'
    )

    # Track ring (full circle in the surface colour). It is drawn UNDER the
    # coloured segments so the small gap left between adjacent segments reveals
    # this darker surface as a crisp separator line — this keeps neighbouring
    # segments visually distinct even when their copper/champagne hues are close
    # in lightness (the legend additionally labels every segment).
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
        f'stroke="{SURFACE}" stroke-width="{ring_w}"/>'
    )

    # A small fixed gap (in px along the circumference) is removed from the END of
    # every visible arc so that a thin slice of the dark track shows through
    # between segments. The cumulative position still advances by the FULL segment
    # length, so percentages stay exact and the layout is deterministic.
    seg_gap = 2.0
    # Coloured segments, laid end-to-end starting at 12 o'clock (clockwise).
    parts.append(f'<g transform="rotate(-90 {cx} {cy})">')
    cumulative = 0.0
    for i, (name, _size, pct) in enumerate(rows):
        seg_len = circumference * pct / 100.0
        color = OTHER_COLOR if name == "Other" else BAR_COLORS[i % len(BAR_COLORS)]
        # Visible arc = segment minus the separator gap, but never below a small
        # minimum so a tiny share still registers as a sliver.
        visible = max(1.0, seg_len - seg_gap)
        # dasharray: visible arc, then the rest of the circle as a gap.
        dash = f"{round(visible, 3)} {round(circumference - visible, 3)}"
        # offset shifts the dash pattern back by the already-consumed length so
        # this segment begins exactly where the previous one ended.
        offset = round(-cumulative, 3)
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
            f'stroke="{color}" stroke-width="{ring_w}" '
            f'stroke-dasharray="{dash}" stroke-dashoffset="{offset}"/>'
        )
        cumulative += seg_len
    parts.append("</g>")

    # Centre label: dominant language + its share (no token, no repo names).
    if rows:
        top_name, _ts, top_pct = rows[0]
        parts.append(
            f'<text x="{cx}" y="{cy - 4}" fill="{OFFWHITE}" font-size="20" '
            f'font-weight="700" text-anchor="middle">{_fmt_num(top_pct)}%</text>'
        )
        parts.append(
            f'<text x="{cx}" y="{cy + 16}" fill="{COPPER}" font-size="12" '
            f'font-weight="600" text-anchor="middle">{escape(top_name)}</text>'
        )

    # Legend (right side): colour swatch + language name + percent, one per row.
    legend_x = 248
    legend_top = 40
    legend_row_h = 21
    swatch = 11
    parts.append(
        f'<text x="{legend_x}" y="{legend_top - 12}" fill="{COPPER}" '
        f'font-size="13" font-weight="600">Most used languages</text>'
    )
    for i, (name, _size, pct) in enumerate(rows):
        row_y = legend_top + i * legend_row_h
        color = OTHER_COLOR if name == "Other" else BAR_COLORS[i % len(BAR_COLORS)]
        safe_name = escape(name)
        # Swatch.
        parts.append(
            f'<rect x="{legend_x}" y="{row_y}" width="{swatch}" '
            f'height="{swatch}" rx="2" fill="{color}"/>'
        )
        # Language name.
        parts.append(
            f'<text x="{legend_x + swatch + 8}" y="{row_y + swatch - 1}" '
            f'fill="{OFFWHITE}" font-size="12">{safe_name}</text>'
        )
        # Percent (right-aligned within the card).
        parts.append(
            f'<text x="{width - pad}" y="{row_y + swatch - 1}" fill="{MUTED}" '
            f'font-size="12" text-anchor="end">{_fmt_num(pct)}%</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    token = os.environ.get("STATS_TOKEN", "").strip()
    if not token:
        # The workflow's `if: secrets.STATS_TOKEN != ''` gate normally prevents
        # this step from running at all when the secret is absent. If we DO get
        # here without a token, fail loud rather than emit an empty/wrong card.
        print(
            "error: STATS_TOKEN is not set. This script must be gated with "
            "`if: ${{ secrets.STATS_TOKEN != '' }}` so it is skipped (no-op) "
            "when the secret is absent; it never emits a card without data.",
            file=sys.stderr,
        )
        return 1

    repos = list_own_nonfork_repos(token)
    print(f"own non-fork repos: {len(repos)}", file=sys.stderr)
    if not repos:
        print("error: no own non-fork repositories returned by the API",
              file=sys.stderr)
        return 1

    totals = aggregate_language_bytes(token, repos)
    rows, total = build_rows(totals)
    if not rows or total <= 0:
        print("error: aggregated language total is zero — refusing to write an "
              "empty card", file=sys.stderr)
        return 1

    # Observability WITHOUT leaking anything sensitive: log only language names
    # and percentages (no token, no repo names).
    print(f"total language bytes: {total}", file=sys.stderr)
    for name, _size, pct in rows:
        print(f"  {name:<14} {pct:5.1f}%", file=sys.stderr)

    # Render BOTH cards from the SAME rows: the bar card (kept for continuity)
    # and the donut card (embedded in the README's Activity section).
    bar_svg = render_svg(rows)
    donut_svg = render_donut_svg(rows)

    for path, svg in ((OUTPUT_PATH, bar_svg), (DONUT_OUTPUT_PATH, donut_svg)):
        out_dir = os.path.dirname(path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        print(f"wrote {path} ({len(svg)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
