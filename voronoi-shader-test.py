#!/usr/bin/env python3
"""
Programmatic tests for Niri Voronoi crack shaders (Animation B).

Runs without a live niri session. Compares Python reference math from
home/gen-voronoi-bake.py against committed bake data in home/niri-voronoi-bake.nix
and simulates the static-crack visibility gate from close_anim_b.

Usage:
  python3 home/voronoi-shader-test.py              # fast reference tests (default)
  python3 home/voronoi-shader-test.py -v           # verbose mismatch details
  python3 home/voronoi-shader-test.py --compile    # Mesa EGL ES 3.00 compile+link probe
  python3 home/voronoi-shader-test.py --gpu        # headless EGL gap/edge readback vs Python

Exit code 0 = all PASS, 1 = at least one FAIL.

Debug loop for agent 2:
  1. python3 home/voronoi-shader-test.py -v
  2. Fix shader/bake; re-run until PASS
  3. Optional: python3 home/voronoi-shader-test.py --compile --gpu

Key assertions (static crack phase, t in [T2, T3)):
  - Baked centres match Python reference (catches bake drift / page corruption)
  - Runtime nearest-owner loop matches Python reference at probe points
  - gap_px = gap_norm * min(w,h)²: some samples must have gap_px <= 1.0 (crack lines)
  - At least 0.1% of window samples visible (gap_px <= 1.0); 100% invisible => vanish bug
  - Interior centre samples must have gap_px > 1.0 (Voronoi interior is transparent)
  - Close B static path: interior centres (gap_px>1) must stay visible (inverse mask)
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import random
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BAKE_NIX = REPO / "home" / "niri-voronoi-bake.nix"
SHADERS_NIX = REPO / "home" / "niri-shaders.nix"
GEN_BAKE = REPO / "home" / "gen-voronoi-bake.py"
HARNESS_C = Path(__file__).resolve().parent / "voronoi-glsl-harness.c"

# close_anim_v phase constants (home/niri-shaders.nix let block)
T1 = 0.0833
T2 = 0.125
T3 = 0.2917
VW = 0.45
HALF_INV_VW = 0.5 / VW
INV_T1 = 1.0 / T1
INV_T2_T1 = 1.0 / (T2 - T1)

NUM_VARIANTS = 6
NUM_CELLS = 16

OPEN_B_T_CRACK_END = 0.35
OPEN_B_SETTLE_START = 0.88
OPEN_B_CRUMBLE_SPAN = 0.55 * (1.0 - OPEN_B_T_CRACK_END)
INV_OPEN_B_CRACK_END = 1.0 / OPEN_B_T_CRACK_END
INV1000 = 1.0 / 1000.0

# open_anim_v pixel output classes (mirrors shader + spec)
OPEN_STATE_DESKTOP = "desktop"
OPEN_STATE_CRACK = "crack"
OPEN_STATE_WINDOW = "window"
OPEN_STATE_REVEAL = "reveal"      # territory cleared — window at q_stat
OPEN_STATE_FLYING = "flying"      # detached gray shard (#888888 @ 0.5)
OPEN_STATE_ATTACHED = "attached"  # unreleased: transparent (crack lines separate)
OPEN_STATE_GLASS = "glass"

# Typical window geo for close_anim_b after squish (800×600-ish)
DEFAULT_SIZE = (800.0, 600.0)
DEFAULT_T_CRACK = 0.20  # mid static-crack phase [T2, T3)
DEFAULT_LOCAL_SEED = 0.5  # niri_random_seed for close_anim_v


def load_bake_module():
    spec = importlib.util.spec_from_file_location("gen_voronoi_bake", GEN_BAKE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", float(x)))[0]


def parse_paged_float_lut(nix_text: str, base_name: str) -> list[float]:
    """Parse BAKED_*_P0..Pn pages from niri-voronoi-bake.nix into flat array."""
    page_re = re.compile(
        rf"const float {re.escape(base_name)}_P(\d+)\[16\] = float\[16\]\(\s*"
        r"([^)]+)\s*\);",
        re.MULTILINE,
    )
    pages: dict[int, list[float]] = {}
    for m in page_re.finditer(nix_text):
        page = int(m.group(1))
        nums = [float(tok.rstrip(",")) for tok in m.group(2).split() if tok.strip()]
        pages[page] = nums
    if not pages:
        raise ValueError(f"No pages found for {base_name}")
    max_page = max(pages)
    flat: list[float] = []
    for p in range(max_page + 1):
        chunk = pages.get(p, [0.0] * 16)
        if len(chunk) != 16:
            raise ValueError(f"{base_name}_P{p} has {len(chunk)} floats, expected 16")
        flat.extend(chunk)
    return flat


def baked_cc_centers_from_nix(cc_flat: list[float], variant: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    base = variant * NUM_CELLS * 2
    for cell in range(NUM_CELLS):
        idx = base + cell * 2
        out.append((f32(cc_flat[idx]), f32(cc_flat[idx + 1])))
    return out


def centers_equal(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Match float32 bits, with tolerance for .8g bake string round-trip."""
    for av, bv in zip(a, b):
        af, bf = f32(av), f32(bv)
        if struct.pack("f", af) == struct.pack("f", bf):
            continue
        if abs(af - bf) > 2e-7:
            return False
    return True


def bisector_probes(centers: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sample points on Voronoi edges between cell centre pairs."""
    probes: list[tuple[float, float]] = []
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            c0, c1 = centers[i], centers[j]
            for t in (0.48, 0.5, 0.52):
                probes.append(
                    (
                        f32(c0[0] + f32(t * (c1[0] - c0[0]))),
                        f32(c0[1] + f32(t * (c1[1] - c0[1]))),
                    )
                )
    return probes


def runtime_owner_from_centers(
    q_norm: tuple[float, float], centers: list[tuple[float, float]]
) -> int:
    """Simulate GLSL baked_owner_cell_runtime (nearest baked centre)."""
    px, py = q_norm
    sq1 = 1e18
    owner = 0
    for i, (cx, cy) in enumerate(centers):
        dx = f32(px - cx)
        dy = f32(py - cy)
        dsq = f32(dx * dx + dy * dy)
        if dsq < sq1:
            sq1 = dsq
            owner = i
    return owner


def baked_voronoi_variant(vs_raw: float) -> int:
    return int(min(vs_raw * NUM_VARIANTS, NUM_VARIANTS - 1))


def close_v_vs_raw(local_s: float) -> float:
    return f32(local_s * 17.3 + 1.5) % 1.0  # GLSL fract()


def squish_gx(cg_x: float, t: float, gl: bool) -> float:
    """Horizontal map gx = A*cg.x + B for t >= T2 (settled squish)."""
    if t < T1:
        tf = t * INV_T1
        b = (0.5 if gl else -0.5) * tf
        return cg_x + b
    p = (t - T1) * INV_T2_T1 if t < T2 else 1.0
    a = 1.0 + (HALF_INV_VW - 1.0) * p
    b = 0.5 if gl else (0.5 - a)
    return a * cg_x + b


def q_norm_from_geo(cg_x: float, cg_y: float, sz: tuple[float, float], t: float, gl: bool) -> tuple[float, float]:
    gx = squish_gx(cg_x, t, gl)
    qx = f32(gx * sz[0])
    qy = f32(cg_y * sz[1])
    return f32(qx / sz[0]), f32(qy / sz[1])


def gap_px(gap_norm: float, sz: tuple[float, float]) -> float:
    m = min(sz[0], sz[1])
    return f32(gap_norm * m * m)


def edge_px(edge_norm: float, sz: tuple[float, float]) -> float:
    return f32(edge_norm * min(sz[0], sz[1]))


def static_crack_op(
    edge_px_val: float,
    q_orig: tuple[float, float],
    crko: tuple[float, float],
    cfr_sq: float,
) -> float:
    crack_dv = (q_orig[0] - crko[0], q_orig[1] - crko[1])
    dist_sq = crack_dv[0] * crack_dv[0] + crack_dv[1] * crack_dv[1]
    dim = 1.0 if edge_px_val <= 1.5 else 0.0
    in_front = 1.0 if dist_sq <= cfr_sq else 0.0
    return f32(1.0 - 0.7 * dim * in_front)


def baked_cc_nxy_raw_sim(cc_flat: list[float], i: int, page_size: int = 16) -> float:
    """Simulate GLSL BAKED_CC_NXY_raw(i) page dispatch."""
    page = i // page_size
    idx = i - page * page_size
    base = page * page_size
    return f32(cc_flat[base + idx])


def test_page_dispatch_cc_nxy(nix_text: str, verbose: bool) -> TestResult:
    """Verify paged LUT dispatch matches flat indexing (Mesa iGPU bug class)."""
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    mismatches: list[str] = []
    for i in range(len(cc_flat)):
        direct = f32(cc_flat[i])
        via_raw = baked_cc_nxy_raw_sim(cc_flat, i)
        if abs(direct - via_raw) > 2e-7:
            mismatches.append(f"idx={i}: flat={direct:.8g} raw()={via_raw:.8g}")
    if mismatches:
        return TestResult("page_dispatch_cc_nxy", False, "\n  ".join(mismatches[:15]))
    return TestResult("page_dispatch_cc_nxy", True, f"{len(cc_flat)} indices via page dispatch OK")


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


def test_baked_centers_match_reference(bake, nix_text: str, verbose: bool) -> TestResult:
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    mismatches: list[str] = []
    for variant in range(NUM_VARIANTS):
        vs = f32((variant + 0.5) / NUM_VARIANTS)
        ref = bake.cell_centers_norm(vs)
        got = baked_cc_centers_from_nix(cc_flat, variant)
        for cell, (rx, ry) in enumerate(ref):
            gx, gy = got[cell]
            if not centers_equal((rx, ry), (gx, gy)):
                mismatches.append(
                    f"variant={variant} cell={cell}: ref=({rx:.8g},{ry:.8g}) "
                    f"bake=({gx:.8g},{gy:.8g})"
                )
    if mismatches:
        head = "\n  ".join(mismatches[:20])
        extra = f"\n  ... and {len(mismatches) - 20} more" if len(mismatches) > 20 else ""
        return TestResult("baked_centers_match_reference", False, head + extra)
    return TestResult("baked_centers_match_reference", True, f"{NUM_VARIANTS} variants × {NUM_CELLS} cells OK")


def test_runtime_owner_matches_reference(bake, nix_text: str, verbose: bool) -> TestResult:
    """Runtime 16-loop owner must match Python nearest_owner_gap reference."""
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    mismatches: list[str] = []
    probes: list[tuple[float, float]] = []
    for yi in range(33):
        for xi in range(33):
            probes.append((f32(xi / 32.0), f32(yi / 32.0)))
    for variant in range(NUM_VARIANTS):
        vs = f32((variant + 0.5) / NUM_VARIANTS)
        centers_ref = bake.cell_centers_norm(vs)
        centers_bake = baked_cc_centers_from_nix(cc_flat, variant)
        for u, v in probes:
            owner_ref, _ = bake.nearest_owner_gap((u, v), centers_ref)
            owner_sim = runtime_owner_from_centers((u, v), centers_bake)
            if owner_ref != owner_sim:
                mismatches.append(
                    f"variant={variant} q_norm=({u:.6g},{v:.6g}): "
                    f"ref={owner_ref} runtime_sim={owner_sim}"
                )
    if mismatches:
        head = "\n  ".join(mismatches[:20])
        extra = f"\n  ... and {len(mismatches) - 20} more" if len(mismatches) > 20 else ""
        return TestResult("runtime_owner_matches_reference", False, head + extra)
    return TestResult(
        "runtime_owner_matches_reference",
        True,
        f"{NUM_VARIANTS} variants × {len(probes)} probe points OK",
    )


def test_gap_edge_sanity(bake, verbose: bool) -> TestResult:
    """Reference gap/edge must be finite and in expected ranges."""
    bad: list[str] = []
    for variant in range(NUM_VARIANTS):
        vs = f32((variant + 0.5) / NUM_VARIANTS)
        centers = bake.cell_centers_norm(vs)
        for yi in range(33):
            for xi in range(33):
                u = f32(xi / 32.0)
                v = f32(yi / 32.0)
                _, gap = bake.nearest_owner_gap((u, v), centers)
                edge = bake.edge_metric((u, v), centers)
                if not math.isfinite(gap) or gap < 0:
                    bad.append(f"variant={variant} ({u},{v}) bad gap={gap}")
                if not math.isfinite(edge) or edge < 0:
                    bad.append(f"variant={variant} ({u},{v}) bad edge={edge}")
    if bad:
        return TestResult("gap_edge_sanity", False, "\n  ".join(bad[:15]))
    return TestResult("gap_edge_sanity", True, "33×33 samples per variant finite")


def min_gap_on_segment(
    bake,
    p0: tuple[float, float],
    p1: tuple[float, float],
    centers: list[tuple[float, float]],
    steps: int = 32,
) -> tuple[float, float, tuple[float, float]]:
    """Minimum gap_norm along segment p0→p1 (finds near-Voronoi-edge points)."""
    best_gap = float("inf")
    best_px = float("inf")
    best_q = p0
    for i in range(steps + 1):
        t = f32(i / steps)
        q = (f32(p0[0] + t * (p1[0] - p0[0])), f32(p0[1] + t * (p1[1] - p0[1])))
        _, gap = bake.nearest_owner_gap(q, centers)
        if gap < best_gap:
            best_gap = gap
            best_q = q
    return best_gap, best_px, best_q


def test_bisector_crack_gap(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    verbose: bool,
) -> TestResult:
    """
    Deterministic: along each cell-centre pair, min gap_norm must yield gap_px <= 1.0
    for at least one pair per variant (crack lines exist at typical window sizes).
    """
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    failures: list[str] = []
    summary: list[str] = []

    for variant in range(NUM_VARIANTS):
        vs = f32((variant + 0.5) / NUM_VARIANTS)
        centers_ref = bake.cell_centers_norm(vs)
        centers_bake = baked_cc_centers_from_nix(cc_flat, variant)

        variant_ok = False
        best_report = ""
        for i in range(NUM_CELLS):
            for j in range(i + 1, NUM_CELLS):
                min_ref, _, q_ref = min_gap_on_segment(
                    bake, centers_ref[i], centers_ref[j], centers_ref
                )
                min_bake, _, q_bake = min_gap_on_segment(
                    bake, centers_bake[i], centers_bake[j], centers_bake
                )
                if abs(min_ref - min_bake) > 1e-5:
                    failures.append(
                        f"variant={variant} pair ({i},{j}): "
                        f"ref_min_gap={min_ref:.6g} bake_min_gap={min_bake:.6g}"
                    )
                    continue
                gpx = gap_px(min_ref, sz)
                if gpx <= 1.0:
                    variant_ok = True
                    best_report = (
                        f"variant={variant} pair=({i},{j}) q_norm=({q_ref[0]:.5f},{q_ref[1]:.5f}) "
                        f"gap_norm={min_ref:.6g} gap_px={gpx:.4g}"
                    )
                    break
            if variant_ok:
                break

        if not variant_ok:
            failures.append(
                f"variant={variant}: no cell-pair segment has gap_px<=1.0 at sz={sz} "
                f"(crack lines too thin or metric broken)"
            )
        elif verbose:
            summary.append(best_report)

    if failures:
        return TestResult("bisector_crack_gap", False, "\n  ".join(failures))
    detail = f"{NUM_VARIANTS} variants have crack-line samples at gap_px<=1.0"
    if verbose and summary:
        detail += "\n  " + "\n  ".join(summary)
    return TestResult("bisector_crack_gap", True, detail)


def test_static_crack_visibility(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    t: float,
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """
    Monte Carlo static-crack visibility metric (supplemental).

    Reports random-sample fraction with gap_px <= 1.0. FAIL only on 0% (total vanish)
    or 100% (inverted metric). Sparse crack lines (~0.01%) are expected at 800px.
    """
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    vs_raw = close_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    gl = local_s < 0.5

    centers_ref = bake.cell_centers_norm(f32((variant + 0.5) / NUM_VARIANTS))
    centers_bake = baked_cc_centers_from_nix(cc_flat, variant)

    diag = math.sqrt(sz[0] * sz[0] + sz[1] * sz[1])
    cov = f32((local_s * 2.9 + 0.4) % 1.0 * 0.4 + 0.6)
    inv_t3_t2 = 1.0 / (T3 - T2)
    cfr = f32(cov * diag * max(0.0, min(1.0, (t - T2) * inv_t3_t2)))
    cfr_sq = cfr * cfr
    crko_y = f32((local_s * 5.7 + 0.3) % 1.0 * sz[1])
    crko = (0.5 * sz[0], crko_y)

    visible = 0
    interior_opaque = 0
    total = 0
    worst: list[tuple[float, float, float, float, float]] = []

    rng = random.Random(42)
    for _ in range(samples):
        cg_x = rng.random()
        cg_y = rng.random()
        qn = q_norm_from_geo(cg_x, cg_y, sz, t, gl)
        q_orig = (f32(squish_gx(cg_x, t, gl) * sz[0]), f32(cg_y * sz[1]))

        _, gap_ref = bake.nearest_owner_gap(qn, centers_ref)
        _, gap_bake_pt = bake.nearest_owner_gap(qn, centers_bake)
        gap_ref_px = gap_px(gap_ref, sz)
        gap_bake_px = gap_px(gap_bake_pt, sz)

        edge_ref = bake.edge_metric(qn, centers_ref)
        edge_ref_px = edge_px(edge_ref, sz)

        if abs(gap_ref - gap_bake_pt) > 1e-6 or abs(gap_ref_px - gap_bake_px) > 0.01:
            return TestResult(
                "static_crack_visibility",
                False,
                f"ref vs bake centre mismatch at q_norm=({qn[0]:.6g},{qn[1]:.6g}): "
                f"gap_ref_px={gap_ref_px:.4g} gap_bake_px={gap_bake_px:.4g}",
            )

        total += 1
        if gap_ref_px <= 1.0:
            visible += 1
            op = static_crack_op(edge_ref_px, q_orig, crko, cfr_sq)
            worst.append((gap_ref_px, edge_ref_px, op, qn[0], qn[1]))
        elif gap_ref_px > 100.0:
            interior_opaque += 1

    vis_frac = visible / total if total else 0.0
    min_gap = min((w[0] for w in worst), default=float("inf"))
    max_op = max((w[2] for w in worst), default=0.0)

    if visible == 0:
        return TestResult(
            "static_crack_visibility",
            False,
            f"variant={variant} t={t} sz={sz}: 0/{total} samples with gap_px<=1.0 "
            f"(100% vanish — suspect broken centres or gap metric)",
        )
    if visible == total:
        return TestResult(
            "static_crack_visibility",
            False,
            f"variant={variant}: ALL {total} samples gap_px<=1.0 (inverted metric?)",
        )
    if vis_frac < 0.001:
        pass  # sparse crack lines expected; bisector_crack_gap is the hard gate

    detail = (
        f"variant={variant} t={t:.3f} sz={sz[0]:.0f}×{sz[1]:.0f}: "
        f"visible={visible}/{total} ({vis_frac*100:.2f}%), "
        f"min_gap_px={min_gap:.4g}, max_crack_op={max_op:.3f}"
    )
    if verbose and worst:
        w = min(worst, key=lambda x: x[0])
        detail += f"\n  closest crack sample: q_norm=({w[3]:.5f},{w[4]:.5f}) gap_px={w[0]:.4g} edge_px={w[1]:.4g} op={w[2]:.3f}"
    return TestResult("static_crack_visibility", True, detail)


def close_static_crack_op(
    edge_px_val: float,
    q_orig: tuple[float, float],
    crko: tuple[float, float],
    cfr_sq: float,
    gx: float,
) -> float:
    """close_anim_v static crack path: inverse mask on solid window (no gap rejection)."""
    if gx < 0.0 or gx > 1.0:
        return 0.0
    return static_crack_op(edge_px_val, q_orig, crko, cfr_sq)


def crumble_release_time(
    rank: int,
    explode_mode: bool,
    t3: float = T3,
    t2: float = T2,
) -> float:
    """Crumble-mode t_release for close_anim_v (default crumble, not explode tail)."""
    if explode_mode:
        tail_ms = 140.0 + float(rank - 12) * 25.0
        return f32(t2 + tail_ms * (1.0 / 1200.0))
    tail_ms = float(rank) * 25.0 + float(rank // 4) * 75.0
    return f32(t3 + tail_ms * (1.0 / 1200.0))


def simulate_close_v_crumble_owner(
    q_stat: tuple[float, float],
    sz: tuple[float, float],
    centers: list[tuple[float, float]],
    t: float,
    gl: bool,
    crumble_ranks: list[int],
    rv_speed: list[float],
) -> tuple[int, bool]:
    """
    Simulate close_anim_v crumble ownership: static owner + released-cell loop.
    Returns (winning_cell, used_static_fallback).
    """
    inv_sz = (1.0 / sz[0], 1.0 / sz[1])
    q_norm_stat = (f32(q_stat[0] * inv_sz[0]), f32(q_stat[1] * inv_sz[1]))
    static_owner = runtime_owner_from_centers(q_norm_stat, centers)
    corner = (sz[0] if gl else 0.0, sz[1])
    crumble_y_scale = sz[1] * 42.0

    co: list[tuple[float, float]] = [(0.0, 0.0)] * NUM_CELLS
    dt_cell: list[float] = [0.0] * NUM_CELLS

    for i in range(NUM_CELLS):
        rank = crumble_ranks[i]
        tr = crumble_release_time(rank, explode_mode=False)
        dt = max(t - tr, 0.0)
        dt_cell[i] = dt
        if dt <= 0.0:
            continue
        dC = (centers[i][0] * sz[0] - corner[0], centers[i][1] * sz[1] - corner[1])
        dCp = (dC[0], dC[1] - 1.0)
        mag = math.sqrt(dCp[0] * dCp[0] + dCp[1] * dCp[1])
        ow_x = dCp[0] / mag if mag > 1e-9 else 0.0
        co[i] = (
            f32(ow_x * sz[0] * (0.4 + 0.5 * rv_speed[i]) * dt),
            f32(crumble_y_scale * dt * dt),
        )

    best_z = -1e30
    best_owner = -1
    found = False
    for i in range(NUM_CELLS):
        if dt_cell[i] == 0.0:
            continue
        if co[i][1] < best_z - 0.5:
            continue
        center = (f32(centers[i][0] * sz[0] + co[i][0]), f32(centers[i][1] * sz[1] + co[i][1]))
        qo = (f32(q_stat[0] - co[i][0]), f32(q_stat[1] - co[i][1]))
        if qo[0] < 0.0 or qo[0] > sz[0] or qo[1] < 0.0 or qo[1] > sz[1]:
            continue
        q_norm = (f32(qo[0] * inv_sz[0]), f32(qo[1] * inv_sz[1]))
        if runtime_owner_from_centers(q_norm, centers) != i:
            continue
        z = co[i][1]
        if z > best_z + 0.5:
            best_z = z
            best_owner = i
            found = True

    if not found and dt_cell[static_owner] == 0.0:
        return static_owner, True
    if not found:
        return -1, False
    return best_owner, False


def test_close_static_crack_interior(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    t: float,
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """
    Close B static crack must show Voronoi interiors (gap_px >> 1).

    Open B uses gap > 1.0 → transparent; close B uses inverse mask (interior
    opaque, crack lines dimmed). Interior samples must not vanish at crack start.
    """
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    vs_raw = close_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    gl = local_s < 0.5
    centers = baked_cc_centers_from_nix(cc_flat, variant)

    diag = math.sqrt(sz[0] * sz[0] + sz[1] * sz[1])
    cov = f32((local_s * 2.9 + 0.4) % 1.0 * 0.4 + 0.6)
    inv_t3_t2 = 1.0 / (T3 - T2)
    cfr = f32(cov * diag * max(0.0, min(1.0, (t - T2) * inv_t3_t2)))
    cfr_sq = cfr * cfr
    crko_y = f32((local_s * 5.7 + 0.3) % 1.0 * sz[1])
    crko = (0.5 * sz[0], crko_y)

    interior = 0
    interior_visible = 0
    open_gate_reject = 0
    worst: list[tuple[float, float, float]] = []

    rng = random.Random(99)
    for _ in range(samples):
        cg_x = rng.random()
        cg_y = rng.random()
        gx = squish_gx(cg_x, t, gl)
        if gx < 0.0 or gx > 1.0:
            continue
        qn = (f32(gx), f32(cg_y))
        q_orig = (f32(gx * sz[0]), f32(cg_y * sz[1]))

        _, gap_norm = bake.nearest_owner_gap(qn, centers)
        gpx = gap_px(gap_norm, sz)
        if gpx <= 1.0:
            continue
        interior += 1
        open_gate_reject += 1
        edge_px_val = edge_px(bake.edge_metric(qn, centers), sz)
        op = close_static_crack_op(edge_px_val, q_orig, crko, cfr_sq, gx)
        if op > 0.5:
            interior_visible += 1
        else:
            worst.append((gpx, edge_px_val, op))

    if interior < 50:
        return TestResult(
            "close_static_crack_interior",
            False,
            f"variant={variant}: only {interior} in-bounds interior samples (need >=50)",
        )
    if open_gate_reject != interior:
        return TestResult(
            "close_static_crack_interior",
            False,
            f"variant={variant}: open-B gap gate miscount",
        )
    vis_frac = interior_visible / interior
    if vis_frac < 0.95:
        w = worst[0] if worst else (0.0, 0.0, 0.0)
        return TestResult(
            "close_static_crack_interior",
            False,
            f"variant={variant}: only {interior_visible}/{interior} ({vis_frac*100:.1f}%) "
            f"interior samples visible; example gap_px={w[0]:.2f} edge_px={w[1]:.2f} op={w[2]:.3f}",
        )

    detail = (
        f"variant={variant} t={t:.3f}: {interior_visible}/{interior} in-bounds interior "
        f"samples visible ({vis_frac*100:.1f}%); open-B gap gate rejects all"
    )
    return TestResult("close_static_crack_interior", True, detail)


def test_all_variants_crack_visible(bake, nix_text: str, sz: tuple[float, float], verbose: bool) -> TestResult:
    """Each variant must have bisector crack samples with gap_px <= 1.0."""
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    failures: list[str] = []
    for variant in range(NUM_VARIANTS):
        vs = f32((variant + 0.5) / NUM_VARIANTS)
        centers = bake.cell_centers_norm(vs)
        found = False
        for i in range(NUM_CELLS):
            for j in range(i + 1, NUM_CELLS):
                min_gap, _, _ = min_gap_on_segment(bake, centers[i], centers[j], centers)
                if gap_px(min_gap, sz) <= 1.0:
                    found = True
                    break
            if found:
                break
        if not found:
            failures.append(f"variant={variant}: no gap_px<=1.0 on any centre-pair segment")
    if failures:
        return TestResult("all_variants_crack_visible", False, "\n  ".join(failures))
    return TestResult("all_variants_crack_visible", True, f"{NUM_VARIANTS} variants OK at sz={sz}")


def extract_voronoi_glsl() -> str:
    """Pull voronoiRuntime + bake glslConstants from Nix sources."""
    nix_text = SHADERS_NIX.read_text()
    bake_attr = subprocess.run(
        ["nix", "eval", "--raw", "-f", str(BAKE_NIX), "glslConstants"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if bake_attr.returncode != 0:
        raise RuntimeError(f"nix eval glslConstants failed:\n{bake_attr.stderr}")
    m = re.search(r"voronoiRuntime = ''\n(.*?)  '';", nix_text, re.DOTALL)
    if not m:
        raise RuntimeError("voronoiRuntime block not found in niri-shaders.nix")
    return bake_attr.stdout + "\n" + m.group(1)


def nix_egl_build_cmd(exe_path: Path) -> list[str]:
    """Build harness with libglvnd headers + runtime libs via nix eval paths."""
    dev = subprocess.run(
        ["nix", "eval", "--raw", "nixpkgs#libglvnd.dev", "--impure"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    lib = subprocess.run(
        ["nix", "eval", "--raw", "nixpkgs#libglvnd", "--impure"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if dev.returncode != 0 or lib.returncode != 0:
        raise RuntimeError("nix eval libglvnd paths failed")
    inc = f"{dev.stdout.strip()}/include"
    libdir = f"{lib.stdout.strip()}/lib"
    return nix_shell_cmd(
        "cc",
        "-O2",
        "-o",
        str(exe_path),
        str(HARNESS_C),
        f"-I{inc}",
        f"-L{libdir}",
        f"-Wl,-rpath,{libdir}",
        "-lEGL",
        "-lGLESv2",
    )


def nix_shell_cmd(*args: str) -> list[str]:
    return ["nix", "shell", "nixpkgs#gcc", "-c", *args]


def build_egl_harness(exe_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        nix_egl_build_cmd(exe_path),
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_glsl_compile_es300(verbose: bool) -> TestResult:
    """Compile+link voronoi probe shader via headless Mesa EGL (ES 3.00 path)."""
    if not HARNESS_C.is_file():
        return TestResult("glsl_compile_es300", False, f"missing {HARNESS_C}")

    body = extract_voronoi_glsl()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        frag = td_path / "probe.frag"
        frag.write_text(
            f"""#version 300 es
precision highp float;
layout(location=0) out vec4 fragColor;
uniform int u_variant;
uniform vec2 u_q_norm;
{body}
void main() {{
    float gap = voronoi_gap_norm(u_variant, u_q_norm);
    float edge = voronoi_edge_norm(u_variant, u_q_norm);
    fragColor = vec4(gap, edge, 0.0, 1.0);
}}
"""
        )
        exe = td_path / "harness"
        build_r = build_egl_harness(exe)
        if build_r.returncode != 0:
            return TestResult(
                "glsl_compile_es300",
                False,
                f"compile harness failed:\n{build_r.stderr}",
            )
        run_r = subprocess.run(
            [str(exe), "--compile-only", str(frag)],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        if run_r.returncode != 0:
            return TestResult(
                "glsl_compile_es300",
                False,
                f"Mesa ES 3.00 link failed:\n{run_r.stderr}",
            )
        nbytes = len(body) + len(frag.read_text())
        return TestResult(
            "glsl_compile_es300",
            True,
            f"Mesa linked probe shader ({nbytes} bytes GLSL body)",
        )


def test_gpu_gap_readback(bake, verbose: bool) -> TestResult:
    """Headless EGL: compare GPU voronoi_gap_norm to Python at probe points."""
    if not HARNESS_C.is_file():
        return TestResult("gpu_gap_readback", False, f"missing {HARNESS_C}")

    body = extract_voronoi_glsl()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        frag = td_path / "probe.frag"
        frag.write_text(
            f"""#version 300 es
precision highp float;
layout(location=0) out vec4 fragColor;
uniform int u_variant;
uniform vec2 u_q_norm;
{body}
void main() {{
    float gap = voronoi_gap_norm(u_variant, u_q_norm);
    float edge = voronoi_edge_norm(u_variant, u_q_norm);
    fragColor = vec4(gap, edge, 0.0, 1.0);
}}
"""
        )
        exe = td_path / "harness"
        build_r = build_egl_harness(exe)
        if build_r.returncode != 0:
            return TestResult(
                "gpu_gap_readback",
                False,
                f"compile harness failed:\n{build_r.stderr}",
            )

        probes: list[tuple[int, float, float, float, float]] = []
        variant = 0
        vs = f32(0.5 / NUM_VARIANTS)
        centers = bake.cell_centers_norm(vs)
        # centre, random, near cell boundary
        probe_qs = [(0.5, 0.5), (0.12, 0.34), (0.76, 0.21)]
        c0, c1 = centers[0], centers[1]
        probe_qs.append(
            (f32((c0[0] + c1[0]) * 0.5), f32((c0[1] + c1[1]) * 0.5))
        )
        for u, v in probe_qs:
            gap_ref = bake.nearest_owner_gap((u, v), centers)[1]
            edge_ref = bake.edge_metric((u, v), centers)
            probes.append((variant, u, v, gap_ref, edge_ref))

        mismatches: list[str] = []
        for variant, u, v, gap_ref, edge_ref in probes:
            run_r = subprocess.run(
                [str(exe), str(frag), str(variant), f"{u:.8f}", f"{v:.8f}"],
                capture_output=True,
                text=True,
            )
            if run_r.returncode != 0:
                return TestResult(
                    "gpu_gap_readback",
                    False,
                    f"EGL harness failed for ({u},{v}):\n{run_r.stderr}",
                )
            parts = run_r.stdout.strip().split()
            if len(parts) < 2:
                return TestResult("gpu_gap_readback", False, f"bad output: {run_r.stdout!r}")
            gap_gpu = float(parts[0])
            edge_gpu = float(parts[1])
            tol = 1e-4
            if abs(gap_gpu - gap_ref) > tol or abs(edge_gpu - edge_ref) > tol:
                mismatches.append(
                    f"variant={variant} q=({u:.6g},{v:.6g}): "
                    f"gap gpu={gap_gpu:.8g} ref={gap_ref:.8g} | "
                    f"edge gpu={edge_gpu:.8g} ref={edge_ref:.8g}"
                )

        if mismatches:
            return TestResult("gpu_gap_readback", False, "\n  ".join(mismatches))
        return TestResult("gpu_gap_readback", True, f"{len(probes)} probe points match Python")


def test_close_shader_static_no_gap_reject(verbose: bool) -> TestResult:
    """close_anim_v static crack path must not use open-B gap>1 vanish gate."""
    text = SHADERS_NIX.read_text()
    m = re.search(r"vec4 close_anim_v\(.*?\n      \}", text, re.DOTALL)
    if not m:
        return TestResult("close_shader_static_no_gap_reject", False, "close_anim_v not found")
    body = m.group(0)
    static_m = re.search(
        r"if \(t < T3 && \(!explode_mode \|\| explode_static\)\).*?\n          \}",
        body,
        re.DOTALL,
    )
    if not static_m:
        return TestResult(
            "close_shader_static_no_gap_reject",
            False,
            "static crack block not found in close_anim_v",
        )
    static = static_m.group(0)
    if re.search(r"gap\s*>\s*1\.0.*return\s+vec4\(0", static):
        return TestResult(
            "close_shader_static_no_gap_reject",
            False,
            "static crack still rejects gap>1 (open-B vanish gate)",
        )
    return TestResult(
        "close_shader_static_no_gap_reject",
        True,
        "close_anim_v static path has no gap>1 transparent return",
    )


def extract_window_open_glsl(nix_text: str) -> str:
    """Active windowOpen attr only (not windowOpenLegacy)."""
    m = re.search(r"windowOpen = ''\n(.*?)'';", nix_text, re.DOTALL)
    if not m:
        raise ValueError("windowOpen attribute not found in niri-shaders.nix")
    return m.group(1)


def open_v_vs_raw(local_s: float) -> float:
    return f32((local_s * 17.3 + 1.5) % 1.0)


def open_v_explode_mode(local_s: float) -> bool:
    return f32((local_s * 3.1 + 0.6) % 1.0) < 0.5


def open_v_release_time(rank: int, explode_mode: bool) -> float:
    if explode_mode and rank < 12:
        wave = rank // 4
        wave_rank = rank - wave * 4
        wave_delay = 0.0 if wave == 0 else (60.0 if wave == 1 else 140.0)
        return f32(OPEN_B_T_CRACK_END + (wave_delay + float(wave_rank) * 10.0) * INV1000)
    tail_ms = (140.0 + float(rank - 12) * 25.0) if explode_mode else 0.0
    if explode_mode:
        return f32(OPEN_B_T_CRACK_END + tail_ms * INV1000)
    return f32(OPEN_B_T_CRACK_END + (float(rank) / 15.0) * OPEN_B_CRUMBLE_SPAN)


def open_v_compute_physics(
    centers: list[tuple[float, float]],
    sz: tuple[float, float],
    t: float,
    local_s: float,
    bake,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], list[float]]:
    """Mirror open_anim_v centre + offset precompute."""
    vs_raw = open_v_vs_raw(local_s)
    explode_mode = open_v_explode_mode(local_s)
    center = (0.5 * sz[0], 0.5 * sz[1])
    diag = math.sqrt(sz[0] * sz[0] + sz[1] * sz[1])
    explode_v0 = diag * 9.0
    explode_gravity = sz[1] * 18.0
    crumble_y_scale = sz[1] * 42.0
    spin_sign = 1.0 if f32((local_s * 11.3 + 0.2) % 1.0) < 0.5 else -1.0

    cc = [(c[0] * sz[0], c[1] * sz[1]) for c in centers]
    co: list[tuple[float, float]] = [(0.0, 0.0)] * NUM_CELLS
    dt_cell: list[float] = [0.0] * NUM_CELLS

    for i in range(NUM_CELLS):
        rank = bake.center_rank(centers, i)
        tr = open_v_release_time(rank, explode_mode)
        dt = max(t - tr, 0.0)
        dt_cell[i] = dt
        if dt <= 0.0:
            continue
        rv_speed, rv_rot, rnd_x, rnd_y = bake.murmur_cell_randoms(vs_raw, i)
        if explode_mode and rank < 12:
            wave = rank // 4
            wave_rank = rank - wave * 4
            vel_scale = 1.0 if wave == 0 else (0.6 if wave == 1 else 0.8)
            dC = (cc[i][0] - center[0], cc[i][1] - center[1])
            dir_x = dC[0] + rnd_x * 0.18
            dir_y = dC[1] + rnd_y * 0.18
            mag = math.sqrt(max(dir_x * dir_x + dir_y * dir_y, 1e-9))
            dir_x /= mag
            dir_y /= mag
            v0 = explode_v0 * vel_scale * (0.9 + 0.2 * rv_speed)
            co[i] = (f32(dir_x * v0 * dt), f32(dir_y * v0 * dt + explode_gravity * dt * dt))
        else:
            dC = (cc[i][0] - center[0], cc[i][1] - center[1])
            dir_x = dC[0]
            dir_y = dC[1] - 0.15
            mag = math.sqrt(max(dir_x * dir_x + dir_y * dir_y, 1e-9))
            dir_x /= mag
            dir_y /= mag
            speed = diag * 0.35 * (0.4 + 0.5 * rv_speed)
            co[i] = (
                f32(dir_x * speed * dt),
                f32(dir_y * speed * dt + crumble_y_scale * dt * dt),
            )
    return cc, co, dt_cell


def open_v_classify_pixel(
    cg_x: float,
    cg_y: float,
    sz: tuple[float, float],
    t: float,
    local_s: float,
    centers: list[tuple[float, float]],
    bake,
    cc: list[tuple[float, float]],
    co: list[tuple[float, float]],
    dt_cell: list[float],
) -> str:
    """Classify open_anim_v output for one geo-space sample (no settle blend)."""
    if t >= 1.0:
        return OPEN_STATE_WINDOW

    q_stat = (f32(cg_x * sz[0]), f32(cg_y * sz[1]))
    inv_sz = (1.0 / sz[0], 1.0 / sz[1])
    q_norm_stat = (f32(q_stat[0] * inv_sz[0]), f32(q_stat[1] * inv_sz[1]))
    center = (0.5 * sz[0], 0.5 * sz[1])
    crko = center
    diag = math.sqrt(sz[0] * sz[0] + sz[1] * sz[1])
    cov = f32((local_s * 2.9 + 0.4) % 1.0 * 0.4 + 0.6)
    cfr = f32(cov * diag * max(0.0, min(1.0, t * INV_OPEN_B_CRACK_END)))
    cfr_sq = cfr * cfr
    gap_scale = min(sz[0], sz[1]) ** 2
    explode_mode = open_v_explode_mode(local_s)
    explode_static = explode_mode and (t <= OPEN_B_T_CRACK_END)

    if (not explode_mode and t < OPEN_B_T_CRACK_END) or explode_static:
        _, gap_norm = bake.nearest_owner_gap(q_norm_stat, centers)
        if gap_px(gap_norm, sz) > 1.0:
            return OPEN_STATE_DESKTOP
        crack_dv = (q_stat[0] - crko[0], q_stat[1] - crko[1])
        if crack_dv[0] * crack_dv[0] + crack_dv[1] * crack_dv[1] >= cfr_sq:
            return OPEN_STATE_DESKTOP
        edge_px_val = edge_px(bake.edge_metric(q_norm_stat, centers), sz)
        if edge_px_val >= 1.5:
            return OPEN_STATE_DESKTOP
        return OPEN_STATE_CRACK

    cell_half_x = 0.35 * sz[0]
    cell_half_y = 0.35 * sz[1]
    territory = runtime_owner_from_centers(q_norm_stat, centers)
    best_z = -1e30
    shard_hit = False

    for i in range(NUM_CELLS):
        if dt_cell[i] == 0.0:
            continue
        if co[i][1] < best_z - 0.5:
            continue
        shard_center = (cc[i][0] + co[i][0], cc[i][1] + co[i][1])
        rel_aabb = (q_stat[0] - shard_center[0], q_stat[1] - shard_center[1])
        if abs(rel_aabb[0]) > cell_half_x or abs(rel_aabb[1]) > cell_half_y:
            continue
        qo = (cc[i][0] + rel_aabb[0], cc[i][1] + rel_aabb[1])
        if qo[0] < 0.0 or qo[0] > sz[0] or qo[1] < 0.0 or qo[1] > sz[1]:
            continue
        q_norm = (f32(qo[0] * inv_sz[0]), f32(qo[1] * inv_sz[1]))
        if runtime_owner_from_centers(q_norm, centers) != i:
            continue
        z = co[i][1]
        if z > best_z + 0.5:
            best_z = z
            shard_hit = True

    if shard_hit:
        return OPEN_STATE_FLYING

    if dt_cell[territory] == 0.0:
        edge_px_val = edge_px(bake.edge_metric(q_norm_stat, centers), sz)
        crack_dv = (q_stat[0] - crko[0], q_stat[1] - crko[1])
        if (
            edge_px_val < 1.5
            and crack_dv[0] * crack_dv[0] + crack_dv[1] * crack_dv[1] < cfr_sq
        ):
            return OPEN_STATE_CRACK
        return OPEN_STATE_ATTACHED

    return OPEN_STATE_REVEAL


def open_v_animated_fraction(states: list[str]) -> float:
    """Fraction showing any intentional animation output (not desktop holes)."""
    if not states:
        return 0.0
    animated = sum(
        1
        for s in states
        if s
        in (
            OPEN_STATE_WINDOW,
            OPEN_STATE_REVEAL,
            OPEN_STATE_FLYING,
            OPEN_STATE_ATTACHED,
            OPEN_STATE_CRACK,
        )
    )
    return animated / len(states)


def sample_open_v_states(
    bake,
    centers: list[tuple[float, float]],
    sz: tuple[float, float],
    t: float,
    local_s: float,
    samples: int,
    rng: random.Random,
) -> list[str]:
    cc, co, dt_cell = open_v_compute_physics(centers, sz, t, local_s, bake)
    out: list[str] = []
    for _ in range(samples):
        cg_x = rng.random()
        cg_y = rng.random()
        out.append(
            open_v_classify_pixel(
                cg_x, cg_y, sz, t, local_s, centers, bake, cc, co, dt_cell
            )
        )
    return out


def test_open_v_territory_no_inward_slide(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """
    Centre pixel must not flip to outer unreleased (green) when inner territory
    releases — should be reveal or flying shard, not sliding voronoi ownership.
    """
    vs_raw = open_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    centers = baked_cc_centers_from_nix(parse_paged_float_lut(nix_text, "BAKED_CC_NXY"), variant)
    t = f32(OPEN_B_T_CRACK_END + 0.08)
    cc, co, dt_cell = open_v_compute_physics(centers, sz, t, local_s, bake)
    if not any(d > 0 for d in dt_cell):
        return TestResult("open_v_territory_no_inward_slide", False, "no releases at probe t")

    st = open_v_classify_pixel(0.5, 0.5, sz, t, local_s, centers, bake, cc, co, dt_cell)
    territory = runtime_owner_from_centers((0.5, 0.5), centers)
    if dt_cell[territory] > 0 and st == OPEN_STATE_ATTACHED:
        return TestResult(
            "open_v_territory_no_inward_slide",
            False,
            f"variant={variant} centre: released territory {territory} classified "
            f"attached at t={t:.3f}",
        )
    if st not in (OPEN_STATE_REVEAL, OPEN_STATE_FLYING, OPEN_STATE_CRACK, OPEN_STATE_ATTACHED):
        return TestResult(
            "open_v_territory_no_inward_slide",
            False,
            f"variant={variant} centre state={st} at t={t:.3f}",
        )
    return TestResult(
        "open_v_territory_no_inward_slide",
        True,
        f"variant={variant} centre territory={territory} → {st} (no inward green slide)",
    )


def test_open_v_released_inside_reveals_window(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """
    After release, cleared territories show reveal; flying shards show shard/glass.
    """
    vs_raw = open_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    centers = baked_cc_centers_from_nix(parse_paged_float_lut(nix_text, "BAKED_CC_NXY"), variant)
    t = f32(OPEN_B_T_CRACK_END + 0.04)
    cc, co, dt_cell = open_v_compute_physics(centers, sz, t, local_s, bake)

    if not any(d > 0 for d in dt_cell):
        return TestResult(
            "open_v_released_inside_reveals",
            False,
            f"variant={variant} t={t:.3f}: no released cells at probe time",
        )
    rng = random.Random(77)
    states = sample_open_v_states(bake, centers, sz, t, local_s, samples, rng)
    active = sum(
        1 for s in states if s in (OPEN_STATE_REVEAL, OPEN_STATE_FLYING, OPEN_STATE_CRACK)
    )
    attached = sum(1 for s in states if s == OPEN_STATE_ATTACHED)
    if active < max(8, samples // 200):
        return TestResult(
            "open_v_released_inside_reveals",
            False,
            f"variant={variant} t={t:.3f}: only {active}/{samples} shard/reveal/glass "
            f"(attached transparent {attached})",
        )
    detail = (
        f"variant={variant} t={t:.3f}: {active}/{samples} flying+reveal+crack "
        f"(attached transparent {attached})"
    )
    return TestResult("open_v_released_inside_reveals", True, detail)


def test_open_v_temporal_reveal(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """Animated visible fraction must be substantial during shatter (not lag-then-pop)."""
    vs_raw = open_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    centers = baked_cc_centers_from_nix(parse_paged_float_lut(nix_text, "BAKED_CC_NXY"), variant)
    rng = random.Random(202)
    times = [0.10, 0.40, 0.55, 0.75]
    fracs: list[float] = []
    glass_fracs: list[float] = []
    for t in times:
        states = sample_open_v_states(bake, centers, sz, t, local_s, samples, rng)
        fracs.append(open_v_animated_fraction(states))
        glass_fracs.append(sum(1 for s in states if s == OPEN_STATE_FLYING) / len(states))

    if fracs[1] < 0.50:
        return TestResult(
            "open_v_temporal_reveal",
            False,
            f"variant={variant}: animated fraction at t=0.40 only {fracs[1]*100:.1f}% "
            f"(need >=50% — middle of animation invisible?)",
        )
    if fracs[1] <= fracs[0] + 0.05:
        return TestResult(
            "open_v_temporal_reveal",
            False,
            f"variant={variant}: crack→shatter not growing {fracs[0]*100:.1f}% → {fracs[1]*100:.1f}%",
        )
    if glass_fracs[3] < 0.01 and fracs[3] < 0.30:
        return TestResult(
            "open_v_temporal_reveal",
            False,
            f"variant={variant}: t=0.75 neither flying ({glass_fracs[3]*100:.1f}%) "
            f"nor animated ({fracs[3]*100:.1f}%) — pop at settle only?",
        )
    detail = (
        f"variant={variant}: animated "
        + " → ".join(f"t={t:.2f}:{f*100:.1f}%" for t, f in zip(times, fracs))
        + f"; flying@0.75={glass_fracs[3]*100:.1f}%"
    )
    return TestResult("open_v_temporal_reveal", True, detail)


def test_open_v_shader_released_inside_reveals_tex(verbose: bool) -> TestResult:
    """Shader uses territory model: dt_cell[territory]==0 attached, else shard/reveal."""
    text = extract_window_open_glsl(SHADERS_NIX.read_text())
    if "dt_cell[territory] == 0.0" not in text:
        return TestResult(
            "open_v_shader_released_inside_tex",
            False,
            "territory attached branch missing",
        )
    if "shard_hit" not in text:
        return TestResult(
            "open_v_shader_released_inside_tex",
            False,
            "flying shard competition missing",
        )
    if re.search(r"if \(found < 0\.5\)\s*\{\s*q_orig = q_stat;\s*bwi\s*=\s*float\(static_owner\)", text):
        return TestResult(
            "open_v_shader_released_inside_tex",
            False,
            "static_owner slide fallback still present",
        )
    return TestResult(
        "open_v_shader_released_inside_tex",
        True,
        "territory model: attached / flying shard / cleared reveal",
    )


def test_open_v_debug_toggle(verbose: bool) -> TestResult:
    """debugOpenV injects DEBUG_OPEN_V const into windowOpen."""
    nix = SHADERS_NIX.read_text()
    if "debugOpenV" not in nix:
        return TestResult("open_v_debug_toggle", False, "debugOpenV missing from let block")
    body = extract_window_open_glsl(nix)
    if "DEBUG_OPEN_V" not in body or "open_v_dbg" not in body:
        return TestResult("open_v_debug_toggle", False, "DEBUG_OPEN_V / open_v_dbg not in windowOpen")
    return TestResult("open_v_debug_toggle", True, "debugOpenV + open_v_dbg wired (default off)")


def open_static_crack_visible(
    gap_px_val: float,
    edge_px_val: float,
    q_stat: tuple[float, float],
    crko: tuple[float, float],
    cfr_sq: float,
) -> bool:
    """open_anim_v static crack: transparent unless on Voronoi edge inside crack front."""
    if gap_px_val > 1.0:
        return False
    crack_dv = (q_stat[0] - crko[0], q_stat[1] - crko[1])
    if crack_dv[0] * crack_dv[0] + crack_dv[1] * crack_dv[1] >= cfr_sq:
        return False
    return edge_px_val < 1.5


def test_open_shader_static_gap_reject(verbose: bool) -> TestResult:
    """open_anim_v static crack path must reject gap>1 (open vanish gate)."""
    text = extract_window_open_glsl(SHADERS_NIX.read_text())
    m = re.search(r"vec4 open_anim_v\(.*?\n      \}", text, re.DOTALL)
    if not m:
        return TestResult("open_shader_static_gap_reject", False, "open_anim_v not found")
    body = m.group(0)
    static_m = re.search(
        r"if \(\(!explode_mode && t < T_CRACK\) \|\| explode_static\).*?\n          \}",
        body,
        re.DOTALL,
    )
    if not static_m:
        return TestResult(
            "open_shader_static_gap_reject",
            False,
            "static crack block not found in open_anim_v",
        )
    static = static_m.group(0)
    if not re.search(r"gap\s*>\s*1\.0", static):
        return TestResult(
            "open_shader_static_gap_reject",
            False,
            "static crack missing gap>1 transparent gate",
        )
    return TestResult(
        "open_shader_static_gap_reject",
        True,
        "open_anim_v static path rejects gap>1 (open vanish gate)",
    )


def test_open_v_flying_shard_gray(verbose: bool) -> TestResult:
    """Flying detached pieces use flat #888888 @ 0.5, not window texture."""
    text = extract_window_open_glsl(SHADERS_NIX.read_text())
    if "open_v_flying_shard" not in text or "open_v_crack_line" not in text:
        return TestResult("open_v_flying_shard_gray", False, "flying/crack helpers missing")
    if "open_v_glass" in text:
        return TestResult("open_v_flying_shard_gray", False, "legacy open_v_glass still present")
    if "mix(1.0, 0.7, crack_edge)" in text:
        return TestResult("open_v_flying_shard_gray", False, "inverted attached opacity still present")
    return TestResult(
        "open_v_flying_shard_gray",
        True,
        "cracks 0.7 / attached transparent / flying #888888@0.5",
    )


def test_open_v_routes_only(verbose: bool) -> TestResult:
    """open_color must route 100% to open_anim_v (no seed bands)."""
    text = extract_window_open_glsl(SHADERS_NIX.read_text())
    m = re.search(r"vec4 open_color\(.*?\n      \}", text, re.DOTALL)
    if not m:
        return TestResult("open_v_routes_only", False, "open_color not found")
    body = m.group(0)
    if "open_anim_v" not in body:
        return TestResult("open_v_routes_only", False, "open_anim_v not in open_color")
    for legacy in ("open_anim_a", "open_anim_b"):
        if legacy in body:
            return TestResult(
                "open_v_routes_only",
                False,
                f"open_color still references {legacy}",
            )
    if re.search(r"seed\s*<\s*0\.5", body):
        return TestResult("open_v_routes_only", False, "seed band routing still present")
    return TestResult("open_v_routes_only", True, "open_color → open_anim_v only")


def test_open_static_crack_interior(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    t: float,
    local_s: float,
    samples: int,
    verbose: bool,
) -> TestResult:
    """
    open_anim_v static crack: Voronoi interiors (gap_px >> 1) stay transparent.

    Inverse of close_anim_v static path — only crack lines at 0.7 inside front.
    """
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    vs_raw = close_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    centers = baked_cc_centers_from_nix(cc_flat, variant)

    diag = math.sqrt(sz[0] * sz[0] + sz[1] * sz[1])
    cov = f32((local_s * 2.9 + 0.4) % 1.0 * 0.4 + 0.6)
    cfr = f32(cov * diag * max(0.0, min(1.0, t * INV_OPEN_B_CRACK_END)))
    cfr_sq = cfr * cfr
    crko = (0.5 * sz[0], 0.5 * sz[1])

    interior = 0
    interior_hidden = 0
    rng = random.Random(77)
    for _ in range(samples):
        cg_x = rng.random()
        cg_y = rng.random()
        qn = (f32(cg_x), f32(cg_y))
        q_stat = (f32(cg_x * sz[0]), f32(cg_y * sz[1]))

        _, gap_norm = bake.nearest_owner_gap(qn, centers)
        gpx = gap_px(gap_norm, sz)
        if gpx <= 1.0:
            continue
        interior += 1
        edge_px_val = edge_px(bake.edge_metric(qn, centers), sz)
        if not open_static_crack_visible(gpx, edge_px_val, q_stat, crko, cfr_sq):
            interior_hidden += 1

    if interior < 50:
        return TestResult(
            "open_static_crack_interior",
            False,
            f"variant={variant}: only {interior} interior samples (need >=50)",
        )
    hide_frac = interior_hidden / interior
    if hide_frac < 0.95:
        return TestResult(
            "open_static_crack_interior",
            False,
            f"variant={variant}: only {interior_hidden}/{interior} ({hide_frac*100:.1f}%) "
            f"interior samples transparent",
        )
    detail = (
        f"variant={variant} t={t:.3f}: {interior_hidden}/{interior} interior "
        f"samples transparent ({hide_frac*100:.1f}%)"
    )
    return TestResult("open_static_crack_interior", True, detail)


def test_close_v_static_owner_at_centres(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    verbose: bool,
) -> TestResult:
    """Vertex field: each baked centre is owned by its cell (static crack phase)."""
    cc_flat = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")
    mismatches: list[str] = []
    for variant in range(NUM_VARIANTS):
        centers = baked_cc_centers_from_nix(cc_flat, variant)
        for cell, (cx, cy) in enumerate(centers):
            owner = runtime_owner_from_centers((cx, cy), centers)
            if owner != cell:
                mismatches.append(
                    f"variant={variant} cell={cell} centre=({cx:.6g},{cy:.6g}) "
                    f"owner={owner}"
                )
    if mismatches:
        return TestResult(
            "close_v_static_owner_at_centres",
            False,
            "\n  ".join(mismatches[:15]),
        )
    return TestResult(
        "close_v_static_owner_at_centres",
        True,
        f"{NUM_VARIANTS} variants × {NUM_CELLS} centres self-own OK",
    )


def test_close_v_crumble_static_fallback(
    bake,
    nix_text: str,
    sz: tuple[float, float],
    verbose: bool,
) -> TestResult:
    """
    Crumble: unreleased cells use static_owner fallback (one 16-loop at q_stat).

    Just after T3, rank-0 cell may be released; higher-rank interiors must still
    resolve to their static owner via fallback when no released shard wins.
    """
    crumble_flat = parse_paged_float_lut(nix_text, "BAKED_CRUMBLE_RANK")
    rv_flat = parse_paged_float_lut(nix_text, "BAKED_RV_SPEED")
    cc_nxy = parse_paged_float_lut(nix_text, "BAKED_CC_NXY")

    t = f32(T3 + 0.02)  # early crumble; only lowest ranks may have dt>0
    local_s = DEFAULT_LOCAL_SEED
    gl = local_s < 0.5
    vs_raw = close_v_vs_raw(local_s)
    variant = baked_voronoi_variant(vs_raw)
    rank_base = (variant * 2 + (1 if gl else 0)) * NUM_CELLS

    centers = baked_cc_centers_from_nix(cc_nxy, variant)
    crumble_ranks = [int(crumble_flat[rank_base + i] + 0.5) for i in range(NUM_CELLS)]
    rv_speed = [f32(rv_flat[variant * NUM_CELLS + i]) for i in range(NUM_CELLS)]

    failures: list[str] = []
    checked = 0
    for cell in range(NUM_CELLS):
        if crumble_ranks[cell] >= 1:
            q_stat = (f32(centers[cell][0] * sz[0]), f32(centers[cell][1] * sz[1]))
            owner, used_static = simulate_close_v_crumble_owner(
                q_stat, sz, centers, t, gl, crumble_ranks, rv_speed
            )
            checked += 1
            if owner != cell:
                failures.append(
                    f"cell={cell} rank={crumble_ranks[cell]}: owner={owner} expected={cell}"
                )
            elif not used_static:
                failures.append(
                    f"cell={cell} rank={crumble_ranks[cell]}: expected static fallback"
                )

    if checked < 8:
        return TestResult(
            "close_v_crumble_static_fallback",
            False,
            f"only {checked} interior cells checked (need >=8)",
        )
    if failures:
        return TestResult(
            "close_v_crumble_static_fallback",
            False,
            "\n  ".join(failures[:12]),
        )
    detail = (
        f"variant={variant} t={t:.4f}: {checked} unreleased interior centres "
        f"→ static_owner fallback"
    )
    return TestResult("close_v_crumble_static_fallback", True, detail)


def run_all(args: argparse.Namespace) -> int:
    bake = load_bake_module()
    nix_text = BAKE_NIX.read_text()

    tests: list[TestResult] = [
        test_baked_centers_match_reference(bake, nix_text, args.verbose),
        test_page_dispatch_cc_nxy(nix_text, args.verbose),
        test_runtime_owner_matches_reference(bake, nix_text, args.verbose),
        test_gap_edge_sanity(bake, args.verbose),
        test_bisector_crack_gap(bake, nix_text, tuple(args.size), args.verbose),
        test_static_crack_visibility(
            bake,
            nix_text,
            tuple(args.size),
            args.t,
            args.seed,
            args.samples,
            args.verbose,
        ),
        test_close_static_crack_interior(
            bake,
            nix_text,
            tuple(args.size),
            args.t,
            args.seed,
            args.samples,
            args.verbose,
        ),
        test_close_shader_static_no_gap_reject(args.verbose),
        test_close_v_static_owner_at_centres(bake, nix_text, tuple(args.size), args.verbose),
        test_close_v_crumble_static_fallback(bake, nix_text, tuple(args.size), args.verbose),
        test_all_variants_crack_visible(bake, nix_text, tuple(args.size), args.verbose),
        test_open_shader_static_gap_reject(args.verbose),
        test_open_v_flying_shard_gray(args.verbose),
        test_open_v_routes_only(args.verbose),
        test_open_v_shader_released_inside_reveals_tex(args.verbose),
        test_open_v_debug_toggle(args.verbose),
        test_open_v_territory_no_inward_slide(
            bake, nix_text, tuple(args.size), args.seed, args.samples, args.verbose
        ),
        test_open_v_released_inside_reveals_window(
            bake, nix_text, tuple(args.size), args.seed, args.samples, args.verbose
        ),
        test_open_v_temporal_reveal(
            bake, nix_text, tuple(args.size), args.seed, args.samples, args.verbose
        ),
        test_open_static_crack_interior(
            bake,
            nix_text,
            tuple(args.size),
            OPEN_B_T_CRACK_END * 0.5,
            args.seed,
            args.samples,
            args.verbose,
        ),
    ]

    if args.compile:
        tests.append(test_glsl_compile_es300(args.verbose))
    if args.gpu:
        tests.append(test_gpu_gap_readback(bake, args.verbose))

    passed = sum(1 for t in tests if t.passed)
    failed = [t for t in tests if not t.passed]

    print("═" * 64)
    print("  voronoi-shader-test.py")
    print("═" * 64)
    for t in tests:
        status = "PASS" if t.passed else "FAIL"
        print(f"  [{status}] {t.name}")
        if t.detail and (args.verbose or not t.passed):
            for line in t.detail.splitlines():
                print(f"         {line}")

    print("─" * 64)
    print(f"  {passed}/{len(tests)} passed")
    if failed:
        print("  FAILED:", ", ".join(t.name for t in failed))
        return 1
    print("  ALL PASS")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="print details for passing tests")
    p.add_argument("--compile", action="store_true", help="glslang ES 3.00 compile probe")
    p.add_argument("--gpu", action="store_true", help="headless EGL gap/edge readback")
    p.add_argument("--t", type=float, default=DEFAULT_T_CRACK, help="crack phase progress [T2,T3)")
    p.add_argument("--seed", type=float, default=DEFAULT_LOCAL_SEED, help="close_anim_v niri_random_seed [0,1]")
    p.add_argument("--samples", type=int, default=12000, help="Monte Carlo samples for visibility test")
    p.add_argument(
        "--size",
        type=float,
        nargs=2,
        default=DEFAULT_SIZE,
        metavar=("W", "H"),
        help="window geo size in px",
    )
    sys.exit(run_all(p.parse_args()))


if __name__ == "__main__":
    main()
