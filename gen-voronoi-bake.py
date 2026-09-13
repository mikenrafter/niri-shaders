#!/usr/bin/env python3
"""
Precompute Animation B Voronoi cell centres, ranks, and Murmur outputs
for niri-shaders.nix.

Mirrors the GPU vhash() and edge metric from close_anim_b:
  edge = 0.5 * (sq2 - sq1) / |c1 - c2|   (pixel-space when scaled by size_geo)
  gap  = sq2 - sq1 in normalised geo²

Gap/edge and owner lookup are computed at runtime in the shader (16-loop over
baked centres).

Usage:
  python3 home/gen-voronoi-bake.py > home/niri-voronoi-bake.nix

Regenerate after changing vhash, cell count, or variant count.
"""

from __future__ import annotations

import math
import struct
import sys
from typing import Iterable

NUM_VARIANTS = 6
NUM_CELLS = 16


def f32(x: float) -> float:
    """Round-trip through float32 (matches GPU precision)."""
    return struct.unpack("f", struct.pack("f", float(x)))[0]


def float_bits_to_int(x: float) -> int:
    """GLSL floatBitsToInt — must use float32 round-trip."""
    return struct.unpack("I", struct.pack("f", f32(x)))[0]


def murmur_cell_ik(vs_raw: float, cell: int) -> tuple[int, int]:
    """Murmur3-style chain from close_anim_b physics loop (_ik / _ik2)."""
    vs_raw = f32(vs_raw)
    ik = (cell * 1597334677) ^ (float_bits_to_int(vs_raw) >> 7)
    ik = ik & 0xFFFFFFFF
    ik = (ik ^ (ik >> 16)) * 0x45D9F3B
    ik = ik & 0xFFFFFFFF
    ik ^= ik >> 16
    ik2 = ik ^ ((ik << 13) & 0xFFFFFFFF)
    ik2 = (ik2 * 0x51D3B) & 0xFFFFFFFF
    ik2 ^= ik2 >> 15
    return ik & 0xFFFFFFFF, ik2 & 0xFFFFFFFF


def murmur_cell_randoms(vs_raw: float, cell: int) -> tuple[float, float, float, float]:
    """rv_speed, rv_rot, rnd_x, rnd_y in [0,1] / [-1,1] — matches GPU."""
    ik, ik2 = murmur_cell_ik(vs_raw, cell)
    rv_speed = f32(float(ik & 0x7FFF) * (1.0 / 32767.0))
    rv_rot = f32(float((ik >> 16) & 0x7FFF) * (1.0 / 32767.0))
    rnd_x = f32(float(ik2 & 0x7FFF) * (2.0 / 32767.0) - 1.0)
    rnd_y = f32(float((ik2 >> 15) & 0x7FFF) * (2.0 / 32767.0) - 1.0)
    return rv_speed, rv_rot, rnd_x, rnd_y


def vhash(i: float, s: float) -> tuple[float, float]:
    """Dave Hoskins-style hash — must match niri-shaders.nix vhash()."""
    i = f32(i)
    s = f32(s)
    p3x = f32(f32(i * 0.1031) + f32(s * 0.137))
    p3y = f32(f32(i * 0.1030) + f32(s * 0.137))
    p3z = f32(f32(i * 0.0973) + f32(s * 0.137))
    p3x = f32(p3x - math.floor(p3x))
    p3y = f32(p3y - math.floor(p3y))
    p3z = f32(p3z - math.floor(p3z))
    dot_p = f32(p3x * f32(p3y + 33.33) + p3y * f32(p3z + 33.33) + p3z * f32(p3x + 33.33))
    p3x = f32(p3x + dot_p)
    p3y = f32(p3y + dot_p)
    p3z = f32(p3z + dot_p)
    ox = f32(p3x + p3x)
    oy = f32(p3y + p3z)
    rx = f32(ox - math.floor(ox))
    ry = f32(oy - math.floor(oy))
    return rx, ry


def cell_centers_norm(vs: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i in range(NUM_CELLS):
        ox = f32(float(i & 3) * 0.25)
        oy = f32(float(i >> 2) * 0.25)
        hx, hy = vhash(float(i), vs)
        out.append((f32(ox + f32(hx * 0.25)), f32(oy + f32(hy * 0.25))))
    return out


def nearest_owner_gap(
    p: tuple[float, float], centers: list[tuple[float, float]]
) -> tuple[int, float]:
    """Return (owner cell id, sq2-sq1) in normalised geo units."""
    px, py = p
    sq1 = 1e18
    sq2 = 1e18
    owner = 0
    for i, (cx, cy) in enumerate(centers):
        dx = f32(px - cx)
        dy = f32(py - cy)
        dsq = f32(dx * dx + dy * dy)
        if dsq < sq1:
            sq2 = sq1
            sq1 = dsq
            owner = i
        elif dsq < sq2:
            sq2 = dsq
    return owner, f32(sq2 - sq1)


def edge_metric(p: tuple[float, float], centers: list[tuple[float, float]]) -> float:
    """Signed bisector distance in normalised geo units."""
    px, py = p
    sq1 = 1e18
    sq2 = 1e18
    c1 = (0.0, 0.0)
    c2 = (0.0, 0.0)
    for cx, cy in centers:
        dx = f32(px - cx)
        dy = f32(py - cy)
        dsq = f32(dx * dx + dy * dy)
        if dsq < sq1:
            sq2, c2 = sq1, c1
            sq1, c1 = dsq, (cx, cy)
        elif dsq < sq2:
            sq2, c2 = dsq, (cx, cy)
    c12x = f32(c1[0] - c2[0])
    c12y = f32(c1[1] - c2[1])
    denom = math.sqrt(max(f32(c12x * c12x + c12y * c12y), 1e-12))
    return f32(0.5 * f32(sq2 - sq1) / f32(denom))


def corner_dist2_norm(
    centers: list[tuple[float, float]], gl: bool, cell: int
) -> float:
    """Squared distance to far-bottom corner in unit-aspect normalised geo."""
    cx, cy = centers[cell]
    side = 1.0 if gl else 0.0
    dx = f32(cx - side)
    dy = f32(cy - 1.0)
    return f32(dx * dx + dy * dy)


def top_dist2_norm(centers: list[tuple[float, float]], gl: bool, cell: int) -> float:
    """Squared distance to far-top corner in unit-aspect normalised geo."""
    cx, cy = centers[cell]
    side = 1.0 if gl else 0.0
    dx = f32(cx - side)
    dy = f32(cy)
    return f32(dx * dx + dy * dy)


def crumble_rank(centers: list[tuple[float, float]], gl: bool, cell: int) -> int:
    """Release rank from corner distance — matches GPU dk <= di tie-break."""
    di = corner_dist2_norm(centers, gl, cell)
    rank = 0
    for k in range(NUM_CELLS):
        if k != cell and corner_dist2_norm(centers, gl, k) <= di:
            rank += 1
    return rank


def center_rank(centers: list[tuple[float, float]], cell: int) -> int:
    """Release rank from window-centre distance — open Animation B."""
    cx, cy = centers[cell]
    di = f32(f32(cx - 0.5) * f32(cx - 0.5) + f32(cy - 0.5) * f32(cy - 0.5))
    rank = 0
    for k in range(NUM_CELLS):
        if k != cell:
            kx, ky = centers[k]
            dk = f32(f32(kx - 0.5) * f32(kx - 0.5) + f32(ky - 0.5) * f32(ky - 0.5))
            if dk <= di:
                rank += 1
    return rank


def explode_rank(centers: list[tuple[float, float]], gl: bool, cell: int) -> int:
    """Release rank from min(corner, top) distance — matches explode_mode GPU."""
    di = min(
        corner_dist2_norm(centers, gl, cell),
        top_dist2_norm(centers, gl, cell),
    )
    rank = 0
    for k in range(NUM_CELLS):
        if k == cell:
            continue
        dk = min(
            corner_dist2_norm(centers, gl, k),
            top_dist2_norm(centers, gl, k),
        )
        if dk <= di:
            rank += 1
    return rank


def _norm2(dx: float, dy: float) -> tuple[float, float]:
    """Normalise (dx, dy) in f32 arithmetic; fallback to (0,-1) at origin."""
    dsq = f32(dx * dx + dy * dy)
    if dsq < 1e-12:
        return 0.0, f32(-1.0)
    inv = f32(1.0 / math.sqrt(dsq))
    return f32(dx * inv), f32(dy * inv)


def crumble_dir_norm(
    centers: list[tuple[float, float]], gl: bool, cell: int
) -> tuple[float, float]:
    """Pre-normalised crumble/tail fall direction toward the release corner.

    Unit-square approximation: ignores the 1-pixel y offset in dCp (negligible
    for any real window ≥ ~30 px high). The baked x-component replaces the
    runtime InverseSqrt in the physics loop.
    """
    cx, cy = centers[cell]
    corner_x = 1.0 if gl else 0.0
    return _norm2(f32(cx - corner_x), f32(cy - 1.0))


def explode_main_dir_norm(
    centers: list[tuple[float, float]], gl: bool, cell: int, vs: float
) -> tuple[float, float]:
    """Pre-normalised blast direction for main explode shards (rank < 12).

    Mirrors the GPU formula verbatim so the baked value is bit-exact at
    unit-square scale (rnd*0.18 is negligible vs pixel-space dC for real windows,
    but we include it for correctness at any scale).
    """
    rv_speed, rv_rot, rnd_x, rnd_y = murmur_cell_randoms(vs, cell)
    use_top = use_top_from_centers(centers, cell) > 0.5
    far_x = f32(1.0 if gl else -1.0)
    if use_top:
        bx = f32(far_x + f32(rnd_x * 0.18))
        by = f32(-1.35 + f32(rnd_y * 0.28))
    else:
        bx = f32(far_x + f32(rnd_x * 0.14))
        by = f32(-0.22 + f32(rnd_y * 0.10))
    return _norm2(bx, by)


def open_crumble_dir_norm(
    centers: list[tuple[float, float]], cell: int
) -> tuple[float, float]:
    """Pre-normalised crumble/tail direction for open_voronoi_shatter.

    Direction = cc - window_centre + (0, -0.15) in normalised geo.
    The (0, -0.15) is a constant downward bias in pixel space; for unit-square
    it equals (0, -0.15) normalised, which is included for accuracy.
    """
    cx, cy = centers[cell]
    return _norm2(f32(cx - 0.5), f32(cy - 0.5 - 0.15))


def open_explode_dir_norm(
    centers: list[tuple[float, float]], cell: int, vs: float
) -> tuple[float, float]:
    """Pre-normalised blast direction for open_voronoi_shatter explode shards.

    dir = (cc - window_centre) + rnd * 0.18  (unit-square, f32 arithmetic).
    For real windows rnd*0.18 is < 0.03% of dC, so bake-time is accurate.
    """
    rv_speed, rv_rot, rnd_x, rnd_y = murmur_cell_randoms(vs, cell)
    cx, cy = centers[cell]
    dx = f32(f32(cx - 0.5) + f32(rnd_x * 0.18))
    dy = f32(f32(cy - 0.5) + f32(rnd_y * 0.18))
    return _norm2(dx, dy)


PAGE_SIZE = 16


def glsl_float_lut(name: str, values: list[float], max_line: int = 120) -> list[str]:
    """Encode a float LUT as a const float array with ES 3.00 constructor syntax.

    GLSL ES 3.00 (#version 300 es) rejects brace array initializers
    (`const float a[N] = { ... }` needs GL_ARB_shading_language_420pack).
    Per §4.1.9 use `const float a[N] = float[N](v0, v1, ...)` instead.
    Runtime lookup is direct `NAME[i]` indexing — no mat4 packing or dispatch.
    """
    n = len(values)
    lines: list[str] = []

    def _flt(v: float) -> str:
        s = f"{v:.8g}"
        if "." not in s and "e" not in s and "E" not in s:
            s += ".0"
        return s

    lines.append(f"      const float {name}[{n}] = float[{n}](")
    parts = [_flt(v) for v in values]
    current = "          "
    for i, arg in enumerate(parts):
        sep = ", " if i > 0 else ""
        candidate = current + sep + arg
        if len(candidate) > max_line and i > 0:
            lines.append(current.rstrip() + ",")
            current = "          " + arg
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    lines.append("      );")
    lines.append("")
    return lines


def glsl_float_lut_paged(
    name: str, values: list[float], page_size: int = PAGE_SIZE, max_line: int = 120
) -> list[str]:
    """Paged float LUT for GLSL ES 3.00 on Mesa iGPU.

    Direct dynamic indexing into large `const float[N]` arrays (N>16) is unreliable
    on Intel iGPU drivers — lookups return 0 / garbage and Voronoi bake breaks.
    Use float[16] pages + page dispatch (same granularity as the old mat4 path).
    """
    n = len(values)
    if n <= page_size:
        lines = glsl_float_lut(name, values, max_line)
        lines.append(f"      float {name}_raw(int i) {{ return {name}[i]; }}")
        lines.append("")
        return lines

    pages = (n + page_size - 1) // page_size
    lines: list[str] = []
    for p in range(pages):
        start = p * page_size
        chunk = list(values[start : start + page_size])
        if len(chunk) < page_size:
            chunk.extend([0.0] * (page_size - len(chunk)))
        lines.extend(glsl_float_lut(f"{name}_P{p}", chunk, max_line))

    lines.append(f"      float {name}_raw(int i) {{")
    lines.append(f"          int page = i / {page_size};")
    lines.append(f"          int idx  = i - page * {page_size};")
    for p in range(pages):
        kw = "if" if p == 0 else "else if"
        lines.append(f"          {kw} (page == {p}) return {name}_P{p}[idx];")
    lines.append("          return 0.0;")
    lines.append("      }")
    lines.append("")
    return lines


def use_top_from_centers(centers: list[tuple[float, float]], cell: int) -> float:
    """top_d2 < corner_d2 in unit-aspect norm — equals cy < 0.5."""
    _, cy = centers[cell]
    return 1.0 if f32(cy) < 0.5 else 0.0


def emit_nix() -> str:
    all_cc: list[tuple[float, float]] = []
    all_crumble_rank: list[float] = []
    all_explode_rank: list[float] = []
    all_corner_d2: list[float] = []
    all_top_d2: list[float] = []
    all_use_top: list[float] = []
    all_rv_speed: list[float] = []
    all_rv_rot: list[float] = []
    all_rnd_x: list[float] = []
    all_rnd_y: list[float] = []
    all_center_rank: list[float] = []
    # Pre-normalised physics directions (eliminates InverseSqrt from GPU loops)
    all_crumble_dir_x: list[float] = []   # close: indexed by (variant, gl, cell)
    all_crumble_dir_y: list[float] = []
    all_explode_dir_x: list[float] = []   # close: indexed by (variant, gl, cell)
    all_explode_dir_y: list[float] = []
    all_open_crumble_dir_x: list[float] = []  # open: indexed by (variant, cell)
    all_open_crumble_dir_y: list[float] = []
    all_open_explode_dir_x: list[float] = []  # open: indexed by (variant, cell)
    all_open_explode_dir_y: list[float] = []
    variant_vs: list[float] = []

    for k in range(NUM_VARIANTS):
        vs = f32((float(k) + 0.5) / float(NUM_VARIANTS))
        variant_vs.append(vs)
        centers = cell_centers_norm(vs)
        all_cc.extend(centers)
        for cell in range(NUM_CELLS):
            rv_speed, rv_rot, rnd_x, rnd_y = murmur_cell_randoms(vs, cell)
            all_rv_speed.append(rv_speed)
            all_rv_rot.append(rv_rot)
            all_rnd_x.append(rnd_x)
            all_rnd_y.append(rnd_y)
            all_center_rank.append(float(center_rank(centers, cell)))
            ox, oy = open_crumble_dir_norm(centers, cell)
            all_open_crumble_dir_x.append(ox)
            all_open_crumble_dir_y.append(oy)
            ex, ey = open_explode_dir_norm(centers, cell, vs)
            all_open_explode_dir_x.append(ex)
            all_open_explode_dir_y.append(ey)
        for gl in (False, True):
            for cell in range(NUM_CELLS):
                all_crumble_rank.append(float(crumble_rank(centers, gl, cell)))
                all_explode_rank.append(float(explode_rank(centers, gl, cell)))
                all_corner_d2.append(corner_dist2_norm(centers, gl, cell))
                all_top_d2.append(top_dist2_norm(centers, gl, cell))
                all_use_top.append(use_top_from_centers(centers, cell))
                dx, dy = crumble_dir_norm(centers, gl, cell)
                all_crumble_dir_x.append(dx)
                all_crumble_dir_y.append(dy)
                ex, ey = explode_main_dir_norm(centers, gl, cell, vs)
                all_explode_dir_x.append(ex)
                all_explode_dir_y.append(ey)

    cc_flat: list[float] = []
    for x, y in all_cc:
        cc_flat.extend([x, y])

    lines = [
        "# Generated by home/gen-voronoi-bake.py — do not edit by hand.",
        "{",
        f"  numVariants = {NUM_VARIANTS};",
        f"  numCells = {NUM_CELLS};",
        "  variantVs = [ "
        + " ".join(f"{v:.8g}" for v in variant_vs)
        + " ];",
        "  glslConstants = ''",
        f"      // ── Baked Voronoi ({NUM_VARIANTS} seed variants, {NUM_CELLS} cells) ──",
        "      // Regenerate: python3 home/gen-voronoi-bake.py > home/niri-voronoi-bake.nix",
        "      //",
        "      // Target: GLSL ES 3.00 (#version 300 es) via patches/niri-glsl-es300.patch.",
        "      // Baked LUTs use float[16] pages + NAME_raw(i) dispatch.",
        "      // Direct float[N>16] dynamic indexing breaks on Intel Mesa ES 3.00.",
        f"      const int BAKED_VORONOI_VARIANTS = {NUM_VARIANTS};",
        f"      const int BAKED_VORONOI_CELLS    = {NUM_CELLS};",
        "",
        "      // Flat normalised cell centres: [(variant*16+cell)*2+{0,1}]",
        "      // Owner/gap/edge queries are runtime 16-loops (see niri-shaders.nix).",
        *glsl_float_lut_paged("BAKED_CC_NXY", cc_flat),
        "      // Release ranks per (variant, gl, cell); unit-aspect corner metric",
        *glsl_float_lut_paged("BAKED_CRUMBLE_RANK", all_crumble_rank),
        *glsl_float_lut_paged("BAKED_EXPLODE_RANK", all_explode_rank),
        "      // Unit-aspect squared distances to far-bottom / far-top corners",
        *glsl_float_lut_paged("BAKED_CORNER_D2_NORM", all_corner_d2),
        *glsl_float_lut_paged("BAKED_TOP_D2_NORM", all_top_d2),
        "      // Explode blast direction hint: top_d2 < corner_d2",
        *glsl_float_lut_paged("BAKED_USE_TOP", all_use_top),
        "      // Per-(variant,cell) randoms and open-B centre release rank",
        *glsl_float_lut_paged("BAKED_RV_SPEED", all_rv_speed),
        *glsl_float_lut_paged("BAKED_RV_ROT", all_rv_rot),
        *glsl_float_lut_paged("BAKED_RND_X", all_rnd_x),
        *glsl_float_lut_paged("BAKED_RND_Y", all_rnd_y),
        *glsl_float_lut_paged("BAKED_CENTER_RANK", all_center_rank),
        "      // Pre-normalised physics directions — eliminates InverseSqrt from GPU loops.",
        "      // close: indexed by baked_rank_index(variant, gl, cell)  [variant*2+gl_int)*16+cell]",
        *glsl_float_lut_paged("BAKED_CRUMBLE_DIR_X", all_crumble_dir_x),
        *glsl_float_lut_paged("BAKED_CRUMBLE_DIR_Y", all_crumble_dir_y),
        *glsl_float_lut_paged("BAKED_EXPLODE_DIR_X", all_explode_dir_x),
        *glsl_float_lut_paged("BAKED_EXPLODE_DIR_Y", all_explode_dir_y),
        "      // open: indexed by baked_cell_index(variant, cell)  [variant*16+cell]",
        *glsl_float_lut_paged("BAKED_OPEN_CRUMBLE_DIR_X", all_open_crumble_dir_x),
        *glsl_float_lut_paged("BAKED_OPEN_CRUMBLE_DIR_Y", all_open_crumble_dir_y),
        *glsl_float_lut_paged("BAKED_OPEN_EXPLODE_DIR_X", all_open_explode_dir_x),
        *glsl_float_lut_paged("BAKED_OPEN_EXPLODE_DIR_Y", all_open_explode_dir_y),
        "      int baked_rank_index(int variant, bool gl, int cell) {",
        "          return (variant * 2 + (gl ? 1 : 0)) * BAKED_VORONOI_CELLS + cell;",
        "      }",
        "",
        "      int baked_crumble_rank(int variant, bool gl, int cell) {",
        "          return int(BAKED_CRUMBLE_RANK_raw(baked_rank_index(variant, gl, cell)) + 0.5);",
        "      }",
        "",
        "      int baked_explode_rank(int variant, bool gl, int cell) {",
        "          return int(BAKED_EXPLODE_RANK_raw(baked_rank_index(variant, gl, cell)) + 0.5);",
        "      }",
        "",
        "      int baked_cell_index(int variant, int cell) {",
        "          return variant * BAKED_VORONOI_CELLS + cell;",
        "      }",
        "",
        "      int baked_center_rank(int variant, int cell) {",
        "          return int(BAKED_CENTER_RANK_raw(baked_cell_index(variant, cell)) + 0.5);",
        "      }",
        "",
        "      float baked_rv_speed(int variant, int cell) {",
        "          return BAKED_RV_SPEED_raw(baked_cell_index(variant, cell));",
        "      }",
        "",
        "      float baked_rv_rot(int variant, int cell) {",
        "          return BAKED_RV_ROT_raw(baked_cell_index(variant, cell));",
        "      }",
        "",
        "      vec2 baked_rnd(int variant, int cell) {",
        "          int idx = baked_cell_index(variant, cell);",
        "          return vec2(BAKED_RND_X_raw(idx), BAKED_RND_Y_raw(idx));",
        "      }",
        "",
        "      float baked_corner_d2_norm(int variant, bool gl, int cell) {",
        "          return BAKED_CORNER_D2_NORM_raw(baked_rank_index(variant, gl, cell));",
        "      }",
        "",
        "      float baked_top_d2_norm(int variant, bool gl, int cell) {",
        "          return BAKED_TOP_D2_NORM_raw(baked_rank_index(variant, gl, cell));",
        "      }",
        "",
        "      float baked_corner_d2(int variant, bool gl, int cell, vec2 sz) {",
        "          float m = min(sz.x, sz.y);",
        "          return baked_corner_d2_norm(variant, gl, cell) * m * m;",
        "      }",
        "",
        "      float baked_top_d2(int variant, bool gl, int cell, vec2 sz) {",
        "          float m = min(sz.x, sz.y);",
        "          return baked_top_d2_norm(variant, gl, cell) * m * m;",
        "      }",
        "",
        "      bool baked_use_top(int variant, bool gl, int cell) {",
        "          return BAKED_USE_TOP_raw(baked_rank_index(variant, gl, cell)) > 0.5;",
        "      }",
        "",
        "      vec2 baked_crumble_dir(int variant, bool gl, int cell) {",
        "          int idx = baked_rank_index(variant, gl, cell);",
        "          return vec2(BAKED_CRUMBLE_DIR_X_raw(idx), BAKED_CRUMBLE_DIR_Y_raw(idx));",
        "      }",
        "",
        "      vec2 baked_explode_dir(int variant, bool gl, int cell) {",
        "          int idx = baked_rank_index(variant, gl, cell);",
        "          return vec2(BAKED_EXPLODE_DIR_X_raw(idx), BAKED_EXPLODE_DIR_Y_raw(idx));",
        "      }",
        "",
        "      vec2 baked_open_crumble_dir(int variant, int cell) {",
        "          int idx = baked_cell_index(variant, cell);",
        "          return vec2(BAKED_OPEN_CRUMBLE_DIR_X_raw(idx), BAKED_OPEN_CRUMBLE_DIR_Y_raw(idx));",
        "      }",
        "",
        "      vec2 baked_open_explode_dir(int variant, int cell) {",
        "          int idx = baked_cell_index(variant, cell);",
        "          return vec2(BAKED_OPEN_EXPLODE_DIR_X_raw(idx), BAKED_OPEN_EXPLODE_DIR_Y_raw(idx));",
        "      }",
        "",
        "      int baked_voronoi_variant(float vs_raw) {",
        "          return int(min(vs_raw * float(BAKED_VORONOI_VARIANTS),",
        "                         float(BAKED_VORONOI_VARIANTS - 1)));",
        "      }",
        "",
        "      vec2 baked_cell_center(int variant, int cell, vec2 sz) {",
        "          int idx = (variant * BAKED_VORONOI_CELLS + cell) * 2;",
        "          return vec2(BAKED_CC_NXY_raw(idx), BAKED_CC_NXY_raw(idx + 1)) * sz;",
        "      }",
        "  '';",
        "}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.stdout.write(emit_nix())


if __name__ == "__main__":
    main()
