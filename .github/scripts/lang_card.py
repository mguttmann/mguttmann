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
# Output: THREE artefacts from the SAME aggregated data:
#   - dist/top-languages.svg          — horizontal bar card  (LANGCARD_OUTPUT)
#   - dist/top-languages-donut.svg    — standalone donut + legend  (LANGDONUT_OUTPUT)
#   - dist/donut-group.svg            — donut as a bare, self-contained <svg>
#                                       FRAGMENT (one <g> root) for COMPOSITION
#                                       into the 3D contribution graphic by the
#                                       token-free merge step (LANGDONUT_GROUP_OUTPUT)
# The standalone donut and bar card are written only when their output paths are
# explicitly requested (kept for continuity / debugging). The PRIMARY artefact
# consumed downstream is the donut GROUP fragment, which is merged into the 3D
# SVG so the calendar + language breakdown render as a SINGLE visual.
#
# VIBRANT, SEMANTICALLY-CORRECT COLOURS: each segment is coloured with the
# OFFICIAL GitHub Linguist colour for that language (e.g. TypeScript #3178c6,
# JavaScript #f1e05a). Unknown languages and the bucketed "Other" slice use a
# neutral grey. Verified against github-linguist/linguist lib/linguist/languages.yml.
#
# Deterministic: languages are sorted by bytes descending, then by name, so
# identical data yields byte-identical files (no pseudo-diff churn on the daily
# schedule); no timestamps or random ids are emitted.
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
# Donut GROUP fragment output — a bare, self-contained <svg> (single root) that
# the token-free merge step composes INTO the 3D contribution graphic. This is
# the PRIMARY artefact of the composite pipeline and is ALWAYS written.
GROUP_OUTPUT_PATH = os.environ.get(
    "LANGDONUT_GROUP_OUTPUT", "dist/donut-group.svg"
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

# ---- Official GitHub Linguist language colours -----------------------------
# Vibrant, semantically-correct per-language colours. Each value is the exact
# `color:` field from github-linguist/linguist lib/linguist/languages.yml
# (verified June 2026). Used by the DONUT renderers so each segment carries the
# language's official GitHub colour instead of a monochrome palette. Languages
# not present here (and the bucketed "Other" slice) fall back to OTHER_COLOR
# (neutral grey). Keys are matched case-insensitively against the language name
# GitHub reports.
GITHUB_LANG_COLORS: Dict[str, str] = {
    "typescript": "#3178c6",
    "javascript": "#f1e05a",
    "python": "#3572A5",
    "c#": "#178600",
    "html": "#e34c26",
    "powershell": "#012456",
    "swift": "#F05138",
    "css": "#663399",
    "shell": "#89e051",
    "dockerfile": "#384d54",
    "c++": "#f34b7d",
    "c": "#555555",
    "tsql": "#e38c00",
    "sql": "#e38c00",
    "plpgsql": "#336790",
    "bicep": "#519aba",
    "vue": "#41b883",
    "go": "#00ADD8",
    "java": "#b07219",
    "ruby": "#701516",
    "kotlin": "#A97BFF",
    "rust": "#dea584",
    "scss": "#c6538c",
    "less": "#1d365d",
    "objective-c": "#438eff",
    "ruby on rails": "#cc0000",
    "dart": "#00B4AB",
    "php": "#4F5D95",
    "makefile": "#427819",
    "batchfile": "#C1F12E",
    "json": "#292929",
    "yaml": "#cb171e",
    "markdown": "#083fa1",
}

# Languages whose official colour is very dark (low luminance). On the midnight
# card / 3D background these read as almost-black blobs, so the donut renderers
# draw a thin lighter stroke around such segments to keep them legible. Values
# are the lighter stroke colour to use for that language's segment edge.
DARK_SEGMENT_STROKE: Dict[str, str] = {
    "powershell": "#3b6ea5",   # #012456 is near-black -> lighter navy edge
    "less": "#5577aa",          # #1d365d is dark -> lighter blue edge
    "json": "#888888",
}


def lang_color(name: str) -> str:
    """Return the official GitHub Linguist colour for a language name.

    Case-insensitive lookup; the bucketed ``Other`` slice and any unknown
    language fall back to the neutral grey ``OTHER_COLOR``.
    """
    if name == "Other":
        return OTHER_COLOR
    return GITHUB_LANG_COLORS.get(name.strip().lower(), OTHER_COLOR)


def lang_edge_stroke(name: str) -> str:
    """Return a lighter edge-stroke colour for very dark segments, else "".

    Empty string means "no special edge needed" (the renderer then uses its
    normal thin separator only).
    """
    if name == "Other":
        return ""
    return DARK_SEGMENT_STROKE.get(name.strip().lower(), "")


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
    for _i, (name, _size, pct) in enumerate(rows):
        seg_len = circumference * pct / 100.0
        # VIBRANT: official GitHub language colour per segment (grey for Other).
        color = lang_color(name)
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
        color = lang_color(name)
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


def render_donut_group(rows: List[Tuple[str, int, float]]) -> str:
    """Render the donut + compact legend as a SELF-CONTAINED <svg> FRAGMENT.

    Unlike :func:`render_donut_svg` (a full card with its own background panel),
    this emits a transparent, single-root ``<svg>`` sized to its OWN local
    coordinate box, intended to be MERGED into the 3D contribution graphic by the
    token-free merge step. The merge wraps this fragment's children in a
    ``<g transform="translate(x y) scale(s)">`` and places it in the empty
    bottom-left region of the 3D SVG, so the donut and the calendar read as a
    single composed visual.

    Design choices for legibility ON the dark 3D background (#00000f / #0E0E13):
      * NO opaque panel — only a faint rounded backdrop at low opacity so the
        donut visually groups without hiding the 3D scene.
      * Each segment uses its OFFICIAL GitHub language colour (vibrant).
      * Very dark segments (e.g. PowerShell #012456) get a thin lighter edge
        stroke so they do not vanish into the background.
      * A heading, the dominant language in the ring centre, and a compact
        legend (swatch + language + %) sit to the right of the ring.

    The fragment's local coordinate box is ``LOCAL_W x LOCAL_H`` (declared on the
    root ``<svg>`` viewBox); the merge scales/translates that whole box. Fully
    deterministic for identical input (stable order, no timestamps/ids).
    """
    # Local coordinate box of the fragment. The merge scales this whole box into
    # the free bottom-left region of the 3D SVG.
    local_w = 360
    # Height grows with the number of legend rows so nothing clips.
    n = len(rows)
    legend_block_h = 30 + n * 22 + 8
    ring_block_h = 200
    local_h = max(ring_block_h, legend_block_h)

    # Ring geometry (left portion of the fragment).
    cx = 96.0
    cy = local_h / 2.0
    radius = 64.0
    ring_w = 24.0
    circumference = 2.0 * math.pi * radius

    parts: List[str] = []
    # Root: a fragment <svg> with an explicit viewBox. role/aria kept minimal;
    # the merged-into-3D context provides the outer title/desc.
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {local_w} {int(local_h)}" '
        f'width="{local_w}" height="{int(local_h)}" '
        f'font-family="Ubuntu, Segoe UI, Helvetica, Arial, sans-serif">'
    )
    # Faint rounded backdrop (low opacity) so the donut groups visually without
    # masking the 3D scene behind it.
    parts.append(
        f'<rect x="2" y="2" width="{local_w - 4}" height="{int(local_h) - 4}" '
        f'rx="14" fill="{BG}" fill-opacity="0.55" '
        f'stroke="{COPPER}" stroke-opacity="0.30" stroke-width="1"/>'
    )

    # Track ring (dark surface) drawn under the coloured segments so the small
    # inter-segment gaps reveal a crisp separator line.
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
        f'stroke="{SURFACE}" stroke-width="{ring_w}"/>'
    )

    seg_gap = 2.0
    parts.append(f'<g transform="rotate(-90 {cx} {cy})">')
    cumulative = 0.0
    for name, _size, pct in rows:
        seg_len = circumference * pct / 100.0
        color = lang_color(name)
        edge = lang_edge_stroke(name)
        visible = max(1.0, seg_len - seg_gap)
        dash = f"{round(visible, 3)} {round(circumference - visible, 3)}"
        offset = round(-cumulative, 3)
        # Optional lighter edge for very dark segments: a slightly WIDER stroke
        # of the lighter colour drawn first, then the true colour on top, so a
        # thin lighter rim remains visible around the dark segment.
        if edge:
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
                f'stroke="{edge}" stroke-width="{ring_w + 3}" '
                f'stroke-dasharray="{dash}" stroke-dashoffset="{offset}"/>'
            )
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
            f'stroke="{color}" stroke-width="{ring_w}" '
            f'stroke-dasharray="{dash}" stroke-dashoffset="{offset}"/>'
        )
        cumulative += seg_len
    parts.append("</g>")

    # Centre label: dominant language + its share.
    if rows:
        top_name, _ts, top_pct = rows[0]
        parts.append(
            f'<text x="{cx}" y="{cy - 3}" fill="{OFFWHITE}" font-size="19" '
            f'font-weight="700" text-anchor="middle">{_fmt_num(top_pct)}%</text>'
        )
        parts.append(
            f'<text x="{cx}" y="{cy + 15}" fill="{COPPER}" font-size="11" '
            f'font-weight="600" text-anchor="middle">{escape(top_name)}</text>'
        )

    # Heading + compact legend (right portion).
    legend_x = 196.0
    legend_top = 34.0
    legend_row_h = 22.0
    swatch = 11.0
    parts.append(
        f'<text x="{legend_x}" y="{legend_top - 12}" fill="{COPPER}" '
        f'font-size="13" font-weight="600">Languages</text>'
    )
    for i, (name, _size, pct) in enumerate(rows):
        row_y = legend_top + i * legend_row_h
        color = lang_color(name)
        edge = lang_edge_stroke(name)
        safe_name = escape(name)
        stroke_attr = (
            f' stroke="{edge}" stroke-width="1"' if edge else ""
        )
        parts.append(
            f'<rect x="{legend_x}" y="{row_y}" width="{swatch}" '
            f'height="{swatch}" rx="2" fill="{color}"{stroke_attr}/>'
        )
        parts.append(
            f'<text x="{legend_x + swatch + 8}" y="{row_y + swatch - 1}" '
            f'fill="{OFFWHITE}" font-size="12">{safe_name}</text>'
        )
        parts.append(
            f'<text x="{local_w - 14}" y="{row_y + swatch - 1}" fill="{OFFWHITE}" '
            f'fill-opacity="0.78" font-size="12" '
            f'text-anchor="end">{_fmt_num(pct)}%</text>'
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

    # Render artefacts from the SAME rows. The PRIMARY artefact consumed
    # downstream is the donut GROUP fragment (merged into the 3D graphic). The
    # bar card and the standalone donut card are written only when their output
    # paths are explicitly configured (kept for continuity / debugging) — by
    # default the composite pipeline asks for the group fragment alone.
    group_svg = render_donut_group(rows)

    outputs: List[Tuple[str, str]] = [(GROUP_OUTPUT_PATH, group_svg)]
    if os.environ.get("LANGCARD_OUTPUT"):
        outputs.append((OUTPUT_PATH, render_svg(rows)))
    if os.environ.get("LANGDONUT_OUTPUT"):
        outputs.append((DONUT_OUTPUT_PATH, render_donut_svg(rows)))

    for path, svg in outputs:
        out_dir = os.path.dirname(path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        print(f"wrote {path} ({len(svg)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
