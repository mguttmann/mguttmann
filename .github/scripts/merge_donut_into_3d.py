#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# merge_donut_into_3d.py  ·  Compose the language donut INTO the 3D graphic
# ---------------------------------------------------------------------------
# Pure, TOKEN-FREE SVG manipulation. Takes the 3D contribution SVG produced by
# yoshi389111/github-profile-3d-contrib (after the language-panel strip) and the
# language-donut GROUP fragment produced by lang_card.py (render_donut_group),
# and injects the donut into the EMPTY bottom-left region of the 3D SVG so the
# calendar + language breakdown render as ONE composed visual.
#
# WHY token-free: this script never touches the GitHub API and never reads any
# secret. It runs in the compose job (Job B) which deliberately does NOT declare
# `environment: stats`, so STATS_TOKEN is not even in scope here. The donut data
# arrives ONLY as a finished SVG fragment (an Actions artifact), never as a
# token — so no credential ever reaches the third-party 3D Action or this merge.
#
# GEOMETRY (verified against the live 3D SVG, viewBox 0 0 1280 850):
#   * The isometric contribution diamond occupies the upper band and slopes down
#     to the FRONT/RIGHT (front-row cells at x 920..1180, y up to ~789).
#   * The radar chart sits on the RIGHT (abs bbox ~x[1132..1259] y[658..788];
#     visually the pentagon is centre-right, y ~100..430).
#   * The contribution counter ("N contributions ☆ ⑂") is bottom-CENTRE at y~830.
#   * The bottom-LEFT region (x < ~350, y > ~500) is EMPTY — exactly where the
#     stripped language panel used to be. The donut is placed there.
# The placement transform is therefore fixed (it does not depend on the daily
# data, which only changes the diamond's interior, never the empty bottom-left).
#
# DETERMINISM: identical inputs -> byte-identical output (no timestamps, no ids,
# fixed transform, fixed insertion point).
#
# FAIL-LOUD: aborts non-zero if either input is missing/empty, if the 3D SVG has
# no closing </svg>, if the fragment is not a single <svg> root, or if the merged
# result is not well-formed XML — so the workflow never pushes a broken compose.
#
# Usage:
#   merge_donut_into_3d.py THREE_D.svg DONUT_GROUP.svg OUTPUT.svg
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import sys
from xml.parsers import expat

# Placement of the donut fragment inside the 3D SVG's coordinate space
# (viewBox 0 0 1280 850). The fragment's own local box is 360 x ~200; scaling by
# ~0.86 yields ~310 x ~172, which fits the empty bottom-left void with margin and
# stays clear of the centre contribution counter (x >= ~315) and the right-side
# radar. These constants are deliberately fixed (the empty region is stable
# across daily regenerations).
PLACE_X = 34.0
PLACE_Y = 588.0
PLACE_SCALE = 0.86

# The 3D SVG's expected viewBox dimensions (sanity check only; we do NOT depend
# on an exact match, but we verify the canvas is at least this large so our
# fixed bottom-left placement is inside the canvas).
EXPECT_W = 1280
EXPECT_H = 850

_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_SVG_CLOSE_RE = re.compile(r"</svg\s*>", re.IGNORECASE)
_VIEWBOX_RE = re.compile(
    r'viewBox\s*=\s*"\s*([-0-9.]+)\s+([-0-9.]+)\s+([-0-9.]+)\s+([-0-9.]+)\s*"',
    re.IGNORECASE,
)


def _is_wellformed_no_dtd(xml_text: str) -> bool:
    """Return True iff ``xml_text`` is well-formed AND contains no DTD/entities.

    Uses expat directly with external-entity resolution disabled and a handler
    that rejects any DOCTYPE/entity declaration. This makes the well-formedness
    check safe against XXE and entity-expansion ("billion laughs") attacks using
    only the standard library (no defusedxml needed on the CI runner). Prints a
    diagnostic and returns False on any parse error or DTD/entity declaration.
    """

    def _reject_dtd(*_args: object) -> None:
        raise ValueError("DOCTYPE/DTD is not permitted in the composed SVG")

    def _reject_entity(*_args: object) -> None:
        raise ValueError("entity declarations are not permitted in the composed SVG")

    parser = expat.ParserCreate()
    # Do not resolve or load any external entity.
    parser.DefaultHandler = lambda _data: None
    parser.StartDoctypeDeclHandler = _reject_dtd  # type: ignore[assignment]
    parser.EntityDeclHandler = _reject_entity  # type: ignore[assignment]
    try:
        parser.Parse(xml_text.encode("utf-8"), True)
    except (expat.ExpatError, ValueError) as exc:
        sys.stderr.write(f"ERROR: merged SVG rejected by parser: {exc}\n")
        return False
    return True


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        raise ValueError(f"input file is empty: {path}")
    return text


def fragment_inner(fragment_svg: str) -> str:
    """Return the inner markup of a single-root <svg> fragment (no <svg> wrapper).

    The donut group fragment from lang_card.render_donut_group is exactly one
    <svg>…</svg>. We strip that outer element and keep its children, which are
    then wrapped in a placement <g> by the caller.
    """
    open_m = _SVG_OPEN_RE.search(fragment_svg)
    if not open_m:
        raise ValueError("donut fragment has no opening <svg> tag")
    # Find the LAST closing </svg> (the fragment is a single root, so this is the
    # root's close).
    close_iter = list(_SVG_CLOSE_RE.finditer(fragment_svg))
    if not close_iter:
        raise ValueError("donut fragment has no closing </svg> tag")
    close_m = close_iter[-1]
    # Guard: there must be exactly ONE <svg> root in the fragment.
    if len(_SVG_OPEN_RE.findall(fragment_svg)) != 1:
        raise ValueError("donut fragment is not a single <svg> root")
    inner = fragment_svg[open_m.end():close_m.start()]
    if not inner.strip():
        raise ValueError("donut fragment is empty inside <svg>")
    return inner.strip()


def merge(three_d_svg: str, donut_fragment: str) -> str:
    """Inject the donut fragment into the 3D SVG's bottom-left region."""
    # Verify the 3D canvas is large enough for our fixed bottom-left placement.
    vb = _VIEWBOX_RE.search(three_d_svg)
    if vb:
        _minx, _miny, vw, vh = (float(vb.group(i)) for i in range(1, 5))
        if vw < EXPECT_W * 0.5 or vh < EXPECT_H * 0.5:
            raise ValueError(
                f"3D SVG viewBox {vw}x{vh} smaller than expected "
                f"~{EXPECT_W}x{EXPECT_H}; refusing to place donut blindly"
            )
        # Our placement must sit inside the canvas.
        if PLACE_X < 0 or PLACE_Y < 0 or PLACE_Y > vh:
            raise ValueError("donut placement falls outside the 3D canvas")

    inner = fragment_inner(donut_fragment)

    # The composed donut block: a titled <g> with the placement transform. A
    # <title> on the wrapper aids accessibility/searchability without exposing
    # any private data (aggregate language breakdown only).
    block = (
        f'<g transform="translate({PLACE_X} {PLACE_Y}) scale({PLACE_SCALE})" '
        f'data-role="language-donut">'
        f"<title>Most used languages (own repositories, forks excluded)</title>"
        f"{inner}"
        f"</g>"
    )

    # Insert immediately BEFORE the 3D SVG's final </svg> so the donut paints on
    # top of the (empty) bottom-left background — nothing else is there to occlude.
    close_iter = list(_SVG_CLOSE_RE.finditer(three_d_svg))
    if not close_iter:
        raise ValueError("3D SVG has no closing </svg> tag")
    close_m = close_iter[-1]
    merged = three_d_svg[: close_m.start()] + block + three_d_svg[close_m.start():]
    return merged


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.stderr.write(
            "usage: merge_donut_into_3d.py THREE_D.svg DONUT_GROUP.svg OUTPUT.svg\n"
        )
        return 2
    three_d_path, donut_path, out_path = argv[1], argv[2], argv[3]

    three_d = _read(three_d_path)
    donut = _read(donut_path)

    merged = merge(three_d, donut)

    # Well-formedness gate: parse the merged result with a HARDENED expat parser.
    # We reject any DTD / external entity / parameter entity so the parser is not
    # exposed to XXE or billion-laughs amplification — even though the input is
    # our own freshly generated SVG (stdlib only; no defusedxml dependency on the
    # runner, matching the token-free strip step's stdlib-only policy). We fail
    # loud so a broken (or DTD-bearing) compose is never written/pushed.
    if not _is_wellformed_no_dtd(merged):
        return 1

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(merged)

    sys.stderr.write(
        "OK: composed donut into 3D SVG "
        f"(3D {len(three_d)} + donut {len(donut)} -> {len(merged)} bytes); "
        f"placed at translate({PLACE_X} {PLACE_Y}) scale({PLACE_SCALE}); "
        f"wrote {out_path}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
