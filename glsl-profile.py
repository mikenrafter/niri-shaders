#!/usr/bin/env python3
"""
SPIR-V–based GLSL op profiler for niri window animation shaders.

Shader source (two paths):
  1. NIRI_SHADER_STORE or --store PATH — read windowClose.glsl / windowOpen.glsl /
     windowResize.glsl from the niri-shader-check build output (preferred).
  2. Fallback — `nix eval --raw -f niri-shaders.nix <attr>` when no store is set.

Pipeline:
  1. Load assembled GLSL body (store file or nix eval)
  2. Wrap in Vulkan GLSL 450 boilerplate + main()
  3. nix run nixpkgs#glslang → compile to SPIR-V
  4. spirv-dis (from nixpkgs spirv-tools) → disassemble
  5. Walk the SPIR-V CFG, separate in-loop vs fixed instructions
  6. Weight by iGPU clock cost; report fixed + per-iteration breakdown

Loop iteration estimates:
  Structural SPIR-V bounds (OpSLessThan on induction vars) often over-count
  when GLSL uses early break/continue on runtime values (nsegs, t_rel, dsi_lim).
  Resolution order per function:
    1. Explicit loop_iters argument (tests / CLI)
    2. GLSL source annotations on loops inside the function:
         // @profile-effective N
         // @profile-loop bound=32 effective=5
    3. Semantic auto-derive (close_shredder): Nix let-block constants from
       niri-shader-anims.nix + reference window geometry
    4. Raw SPIR-V structural bounds (last resort)

Usage:
  python3 glsl-profile.py [windowClose|windowOpen|windowResize]
  NIRI_SHADER_STORE=$(nix build .#checks.niri-shaders --print-out-paths) \\
    python3 glsl-profile.py windowClose

Requires:
  nix run nixpkgs#glslang   (glslangValidator)
  spirv-dis                  (from nixpkgs spirv-tools, auto-detected in /nix/store)
"""

import argparse
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT        = Path(__file__).resolve().parent
DEFAULT_SHADERS  = REPO_ROOT / "niri-shaders.nix"
DEFAULT_ANIMS    = REPO_ROOT / "niri-shader-anims.nix"

ATTR_TO_STORE_FILE = {
    "windowClose": "windowClose.glsl",
    "windowOpen": "windowOpen.glsl",
    "windowResize": "windowResize.glsl",
}

# ── Approximate clock cost on Intel Xe iGPU (FMA-unit equivalent) ────────────
GLSL450_W = {
    'Sin': 25, 'Cos': 25, 'Atan': 35, 'Atan2': 35,
    'Sqrt': 10, 'InverseSqrt': 4, 'Length': 10, 'Distance': 10,
    'Normalize': 14, 'Pow': 20, 'Log': 20, 'Exp': 20,
    'Fract': 1, 'Floor': 1, 'Ceil': 1, 'Abs': 1,
    'Min': 1, 'Max': 1, 'FMax': 1, 'FMin': 1,
    'Clamp': 2, 'FClamp': 2, 'NClamp': 2,
    'Mix': 2, 'FMix': 2, 'Step': 1, 'SmoothStep': 4,
    'Sign': 1, 'Mod': 4, 'Modf': 4,
}
OP_W = {
    'OpImageSampleImplicitLod': 20,
    'OpFDiv': 4, 'OpFMul': 1, 'OpFAdd': 1, 'OpFSub': 1,
    'OpFNegate': 1, 'OpFma': 1, 'OpDot': 2,
    'OpSelect': 1,
    'OpConvertSToF': 1, 'OpConvertFToS': 1, 'OpFConvert': 1,
    'OpFOrdLessThan': 1, 'OpFOrdGreaterThan': 1,
    'OpFOrdLessThanEqual': 1, 'OpFOrdGreaterThanEqual': 1,
    'OpFUnordNotEqual': 1, 'OpSLessThan': 1, 'OpSGreaterThanEqual': 1,
    'OpIAdd': 1, 'OpISub': 1, 'OpSGreaterThan': 1,
}

# Window uniforms added by niri's close/open prelude (niri_window_pos,
# niri_output_size, etc.) — not in the assembler output, injected by niri at
# runtime. Declare them in a UBO so Vulkan GLSL 450 is satisfied.
_WINDOW_UNIFORMS_UBO = """\
layout(set=0, binding=3) uniform WindowUniforms {
    vec2  niri_window_size;
    vec2  niri_window_pos;
    vec2  niri_output_size;
    float niri_is_tabbed;
    float niri_total_columns;
    float niri_windows_in_column;
    float niri_window_index_in_column;
    float niri_columns_in_workspace;
    float niri_column_index_in_workspace;
} _wubo;
#define niri_window_size                 _wubo.niri_window_size
#define niri_window_pos                  _wubo.niri_window_pos
#define niri_output_size                 _wubo.niri_output_size
#define niri_is_tabbed                   _wubo.niri_is_tabbed
#define niri_total_columns               _wubo.niri_total_columns
#define niri_windows_in_column           _wubo.niri_windows_in_column
#define niri_window_index_in_column      _wubo.niri_window_index_in_column
#define niri_columns_in_workspace        _wubo.niri_columns_in_workspace
#define niri_column_index_in_workspace   _wubo.niri_column_index_in_workspace
"""

CLOSE_HEADER = """\
#version 450
layout(location=0) in  vec2 v_coords_xy;
layout(location=1) in  vec3 v_size_geo;
layout(location=0) out vec4 fragColor;
layout(set=0, binding=0) uniform Params {
    float niri_clamped_progress;
    float niri_random_seed;
} params;
#define niri_clamped_progress params.niri_clamped_progress
#define niri_random_seed      params.niri_random_seed
layout(set=0, binding=1) uniform sampler2D niri_tex;
layout(set=0, binding=2) uniform UBO { mat3 niri_geo_to_tex; } _ubo;
#define niri_geo_to_tex _ubo.niri_geo_to_tex
""" + _WINDOW_UNIFORMS_UBO
CLOSE_MAIN = """\
void main() {
    vec3 cg = vec3(v_coords_xy, 1.0);
    vec3 sg = v_size_geo;
    fragColor = close_color(cg, sg);
}
"""

CLOSE_HEADER_CONST = """\
#version 450
layout(location=0) in  vec2 v_coords_xy;
layout(location=1) in  vec3 v_size_geo;
layout(location=0) out vec4 fragColor;
layout(set=0, binding=1) uniform sampler2D niri_tex;
layout(set=0, binding=2) uniform UBO { mat3 niri_geo_to_tex; } _ubo;
#define niri_geo_to_tex _ubo.niri_geo_to_tex
#define niri_clamped_progress __NIRI_T__
#define niri_random_seed      __NIRI_SEED__
""" + _WINDOW_UNIFORMS_UBO

OPEN_HEADER = """\
#version 450
layout(location=0) in  vec2 v_coords_xy;
layout(location=1) in  vec3 v_size_geo;
layout(location=0) out vec4 fragColor;
layout(set=0, binding=0) uniform Params {
    float niri_clamped_progress;
    float niri_random_seed;
} params;
#define niri_clamped_progress params.niri_clamped_progress
#define niri_random_seed      params.niri_random_seed
layout(set=0, binding=1) uniform sampler2D niri_tex;
layout(set=0, binding=2) uniform UBO { mat3 niri_geo_to_tex; } _ubo;
#define niri_geo_to_tex _ubo.niri_geo_to_tex
""" + _WINDOW_UNIFORMS_UBO
OPEN_MAIN = """\
void main() {
    vec3 cg = vec3(v_coords_xy, 1.0);
    vec3 sg = v_size_geo;
    fragColor = open_color(cg, sg);
}
"""

RESIZE_HEADER = """\
#version 450
layout(location=0) in  vec2 v_coords_xy;
layout(location=0) out vec4 fragColor;
layout(set=0, binding=0) uniform Params {
    float niri_clamped_progress;
    float niri_progress;
} params;
#define niri_clamped_progress params.niri_clamped_progress
#define niri_progress         params.niri_progress
layout(set=0, binding=1) uniform sampler2D niri_tex_prev;
layout(set=0, binding=2) uniform sampler2D niri_tex_next;
layout(set=0, binding=3) uniform UBOPrev { mat3 niri_geo_to_tex_prev; } _ubop;
layout(set=0, binding=4) uniform UBONext { mat3 niri_geo_to_tex_next; } _ubon;
layout(set=0, binding=5) uniform UBOSize { vec2 niri_curr_geo_size; } _ubos;
#define niri_geo_to_tex_prev _ubop.niri_geo_to_tex_prev
#define niri_geo_to_tex_next _ubon.niri_geo_to_tex_next
#define niri_curr_geo_size   _ubos.niri_curr_geo_size
"""
RESIZE_MAIN = """\
void main() {
    vec3 cg = vec3(v_coords_xy, 1.0);
    vec3 sg = vec3(niri_curr_geo_size, 1.0);
    fragColor = resize_color(cg, sg);
}
"""


def read_shader_from_store(attr: str, store: Path) -> str | None:
    filename = ATTR_TO_STORE_FILE.get(attr)
    if filename is None:
        return None
    path = store / filename
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").replace('texture2D(', 'texture(')


def extract_glsl_nix_eval(attr: str, shader_path: Path) -> str:
    r = subprocess.run(
        ['nix', 'eval', '--raw', '-f', str(shader_path), attr],
        capture_output=True, text=True, cwd=shader_path.parent.parent,
    )
    if r.returncode:
        sys.exit(f"nix eval failed:\n{r.stderr}")
    return r.stdout.replace('texture2D(', 'texture(')


def extract_glsl(attr: str, shader_path: Path, store: Path | None) -> str:
    if store is not None:
        body = read_shader_from_store(attr, store)
        if body is not None:
            return body
        sys.exit(
            f"Shader store {store} has no {ATTR_TO_STORE_FILE.get(attr, attr)} "
            f"for attr '{attr}'"
        )
    return extract_glsl_nix_eval(attr, shader_path)


def compile_to_spv(glsl_src: str, spv_path: str, optimize: bool = False) -> None:
    with tempfile.NamedTemporaryFile(suffix='.frag', mode='w', delete=False) as f:
        f.write(glsl_src)
        frag_path = f.name
    cmd = ['nix', 'run', 'nixpkgs#glslang', '--',
           '-V', frag_path, '-o', spv_path, '--target-env', 'vulkan1.0']
    if optimize:
        cmd.append('-Os')
    r = subprocess.run(
        cmd,
        capture_output=True, text=True,
    )
    if r.returncode:
        sys.exit(f"glslang compile failed:\n{r.stdout}\n{r.stderr}")


def disassemble(spv_path: str) -> list[str]:
    spirv_bin = next(
        (Path(p) / 'bin' / 'spirv-dis'
         for p in Path('/nix/store').iterdir()
         if 'spirv-tools' in p.name and (Path(p) / 'bin' / 'spirv-dis').exists()),
        None,
    )
    if spirv_bin is None:
        # try nix run fallback
        r = subprocess.run(
            ['nix', 'run', 'nixpkgs#spirv-tools', '--', 'dis', spv_path],
            capture_output=True, text=True,
        )
    else:
        r = subprocess.run(
            [str(spirv_bin), spv_path],
            capture_output=True, text=True,
        )
    if r.returncode:
        sys.exit(f"spirv-dis failed:\n{r.stderr}")
    return r.stdout.splitlines()


def inst_weight(line: str) -> tuple[str, int]:
    m = re.search(r'OpExtInst\s+\S+\s+\S+\s+(\w+)', line)
    if m:
        name = m.group(1)
        return f'OpExtInst:{name}', GLSL450_W.get(name, 2)
    m = re.match(r'\s*(?:\S+\s+=\s+)?(\w+)', line)
    if m:
        op = m.group(1)
        return op, OP_W.get(op, 0)
    return '', 0


# Legacy manual overrides — prefer GLSL @profile-effective or semantic auto-derive.
FN_LOOP_OVERRIDES: dict[str, list[int]] = {
    'open_voronoi_shatter': [16, 16, 6],
}

# Reference fragment geometry for close_shredder trip-count estimation.
# Envelope pre-pass runs earlier in the animation (fewer released segments);
# phase-2 inverse search uses a typical tall window's segment count.
CLOSE_SHREDDER_PROFILE = {
    'envelope_height': 750.0,
    'envelope_progress': 0.35,
    'phase2_height': 1200.0,
    'phase2_progress': 0.50,
}


def parse_spirv_int_constant(token: str) -> int | None:
    token = token.lstrip('%')
    m = re.match(r'int_(\d+)$', token)
    if m:
        return int(m.group(1))
    return None


def resolve_spirv_bound_token(asm_body: list[str], token: str) -> int | None:
    direct = parse_spirv_int_constant(token)
    if direct is not None:
        return direct
    var = token.lstrip('%')
    for line in asm_body:
        store = re.search(rf'OpStore\s+%{re.escape(var)}\s+(%\S+)', line)
        if store:
            return parse_spirv_int_constant(store.group(1))
        load = re.search(rf'%{re.escape(var)}\s*=\s*OpLoad\s+\S+\s+%(\S+)', line)
        if load:
            return resolve_spirv_bound_token(asm_body, load.group(1))
    return None


def detect_loop_trip_counts(asm_body: list[str]) -> list[int]:
    """Infer constant loop bounds from OpLoopMerge headers (OpSLessThan / OpSLessThanEqual)."""
    counts: list[int] = []
    for i, line in enumerate(asm_body):
        if 'OpLoopMerge' not in line:
            continue
        bound: int | None = None
        inclusive = False
        for j in range(i + 1, min(i + 24, len(asm_body))):
            probe = asm_body[j]
            if 'OpLoopMerge' in probe:
                break
            lt = re.search(r'OpSLessThan(?:Equal)?\s+%\S+\s+%\S+\s+(%\S+)', probe)
            if lt:
                bound = resolve_spirv_bound_token(asm_body, lt.group(1))
                inclusive = 'OpSLessThanEqual' in probe
                break
        if bound is None:
            counts.append(1)
        elif inclusive:
            counts.append(bound + 1)
        else:
            counts.append(bound)
    return counts


def extract_glsl_function_body(glsl_src: str, fn_name: str) -> str | None:
    m = re.search(rf'\b(?:vec4|void)\s+{re.escape(fn_name)}\s*\([^)]*\)\s*\{{', glsl_src)
    if not m:
        return None
    depth = 1
    i = m.end()
    body_chars: list[str] = []
    while i < len(glsl_src) and depth:
        ch = glsl_src[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                break
        body_chars.append(ch)
        i += 1
    return ''.join(body_chars)


def parse_glsl_profile_effective(glsl_src: str, fn_name: str) -> list[int] | None:
    """Parse loop profile annotations inside fn_name.

    Supported on the same line as a for-loop header or on the line above:
      // @profile-effective N
      // @profile-loop bound=32 effective=5
    """
    body = extract_glsl_function_body(glsl_src, fn_name)
    if body is None:
        return None
    vals: list[int] = []
    for line in body.splitlines():
        loop_eff = re.search(
            r'//\s*@profile-loop\b[^/\n]*\beffective=(\d+)', line,
        )
        if loop_eff:
            vals.append(int(loop_eff.group(1)))
            continue
        plain_eff = re.search(r'//\s*@profile-effective\s+(\d+)', line)
        if plain_eff:
            vals.append(int(plain_eff.group(1)))
    return vals or None


def parse_nix_shader_constants(shader_path: Path) -> dict[str, float]:
    """Extract numeric Animation C constants from the Nix let-block."""
    text = shader_path.read_text()
    consts: dict[str, float] = {}
    for m in re.finditer(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*([\d.]+)\s*;', text, re.M):
        consts[m.group(1)] = float(m.group(2))
    return consts


def _gi_loop_trips(
    *,
    height: float,
    progress: float,
    seg_h: float,
    slide_end: float,
    t_scale: float,
    gi_cap: int = 32,
) -> int:
    """Count envelope / phase-2 gi iterations before gi>=nsegs or t<t_rel break."""
    t_c = progress * t_scale
    nsegs = min(gi_cap, math.ceil(height / seg_h))
    release_step = slide_end * seg_h / height
    trips = 0
    for gi in range(gi_cap):
        if gi >= nsegs:
            break
        if t_c < gi * release_step:
            break
        trips += 1
    return max(trips, 1)


def derive_close_shredder_loop_iters(
    anims_path: Path,
    detected: list[int],
    profile: dict[str, float] | None = None,
) -> list[int]:
    """Estimate effective loop multipliers from Nix anims constants + reference geometry."""
    consts = parse_nix_shader_constants(anims_path)
    seg_h = consts.get('SEG_H_C', 150.0)
    slide_end = consts.get('SLIDE_END_C', 0.4)
    t_scale = consts.get('SHREDDER_PHYSICS_SCALE', 2.5)

    prof = profile or CLOSE_SHREDDER_PROFILE
    env_h = prof['envelope_height']
    env_p = prof['envelope_progress']
    p2_h = prof['phase2_height']

    envelope_gi = _gi_loop_trips(
        height=env_h, progress=env_p,
        seg_h=seg_h, slide_end=slide_end, t_scale=t_scale,
    )
    phase2_gi = min(
        32,
        math.ceil(p2_h / seg_h),
    )
    # Inner dsi loop has structural bound 2*SCATTER_MAX+1 but continue guards on
    # |dsi|>dsi_lim collapse typical trips to ~2*dsi_lim+1 per fragment that
    # reaches it. Horizontal-reach and scanline culls mean only ~one gi iteration
    # per fragment enters the inner loop; folding that into the phase-2 gi
    # multiplier avoids double-counting (see niri-animations-spec.md).
    inner_dsi = 1

    derived = [envelope_gi, phase2_gi, inner_dsi]
    if len(detected) > len(derived):
        derived.extend(detected[len(derived):])
    elif len(detected) < len(derived):
        derived = derived[:len(detected)]
    return derived


def resolve_loop_iters(
    asm_body: list[str],
    fn_substr: str,
    loop_iters: list[int] | None = None,
    glsl_src: str | None = None,
    shader_path: Path | None = None,
) -> tuple[list[int], list[int], list[str], str]:
    detected = detect_loop_trip_counts(asm_body)
    warnings: list[str] = []
    source = 'structural'

    if loop_iters is not None:
        if loop_iters != detected:
            warnings.append(
                f"provided loop_iters {loop_iters} != SPIR-V structural {detected}"
            )
        return loop_iters, detected, warnings, 'provided'

    glsl_effective = parse_glsl_profile_effective(glsl_src, fn_substr) if glsl_src else None
    if glsl_effective is not None:
        if glsl_effective != detected:
            warnings.append(
                f"GLSL @profile-effective {glsl_effective} != SPIR-V structural {detected}"
            )
        return glsl_effective, detected, warnings, 'glsl'

    manual = FN_LOOP_OVERRIDES.get(fn_substr)
    if manual is not None:
        if manual != detected:
            warnings.append(
                f"FN_LOOP_OVERRIDES {manual} != SPIR-V structural {detected}"
            )
        return manual, detected, warnings, 'manual'

    if fn_substr == 'close_shredder' and shader_path is not None:
        derived = derive_close_shredder_loop_iters(shader_path, detected)
        return derived, detected, warnings, 'semantic'

    return detected, detected, warnings, source


def analyse_function(
    asm_lines: list[str],
    fn_substr: str,
    loop_iters: list[int] | None = None,
    glsl_src: str | None = None,
    shader_path: Path | None = None,
) -> None:
    totals = collect_function_totals(
        asm_lines, fn_substr, loop_iters=loop_iters, glsl_src=glsl_src,
        shader_path=shader_path,
    )
    if totals is None:
        print(f"  (function matching '{fn_substr}' not found in SPIR-V — likely inlined)")
        return
    fn_start = totals['fn_start']
    fn_end = totals['fn_end']
    body_len = totals['body_len']
    static_counts = totals['static_counts']
    static_clocks = totals['static_clocks']
    dyn_counts = totals['dyn_counts']
    dyn_clocks = totals['dyn_clocks']
    total_static_inst = totals['total_static_inst']
    total_dyn_inst = totals['total_dyn_inst']
    total_static_clk = totals['total_static_clk']
    total_dyn_clk = totals['total_dyn_clk']
    resolved_iters = totals['loop_iters']
    detected_iters = totals['detected_iters']
    iter_source = totals['iter_source']
    warnings = totals['warnings']

    print(f"  SPIR-V lines: {fn_start}–{fn_end} ({body_len} instructions)")

    W = 92
    print(f"\n  {'op':<28} {'static#':>8}  {'dyn-est#':>10}  {'static clk':>10}  {'dyn-est clk':>12}")
    print(f"  {'─'*W}")
    all_ops = set(static_counts) | set(dyn_counts)
    for op in sorted(all_ops, key=lambda o: (-dyn_clocks.get(o, 0), -dyn_counts.get(o, 0), o)):
        sc = static_counts.get(op, 0)
        dc = dyn_counts.get(op, 0)
        sw = static_clocks.get(op, 0)
        dw = dyn_clocks.get(op, 0)
        if sc + dc == 0:
            continue
        print(f"  {op:<28} {sc:>8}  {dc:>10}  {sw:>10}  {dw:>12}")
    print(f"  {'─'*W}")
    print(f"  {'totals':<28} {total_static_inst:>8}  {total_dyn_inst:>10}  {total_static_clk:>10}  {total_dyn_clk:>12}")
    if detected_iters == resolved_iters:
        print(f"  loop multipliers: {resolved_iters} (auto-detected)")
    elif iter_source == 'semantic':
        print(
            f"  loop multipliers: {resolved_iters} "
            f"(semantic derive; structural: {detected_iters})"
        )
    else:
        print(f"  loop multipliers: {resolved_iters} (detected structural: {detected_iters})")
    for w in warnings:
        print(f"  warning: {w}")


def collect_function_totals(
    asm_lines: list[str],
    fn_substr: str,
    loop_iters: list[int] | None = None,
    glsl_src: str | None = None,
    shader_path: Path | None = None,
) -> dict | None:
    # Find function boundaries
    fn_start = fn_end = None
    for i, l in enumerate(asm_lines):
        if fn_substr in l and 'OpFunction ' in l:
            fn_start = i
        if fn_start is not None and 'OpFunctionEnd' in l:
            fn_end = i + 1
            break
    if fn_start is None:
        return None

    body = asm_lines[fn_start:fn_end]

    resolved_iters, detected_iters, warnings, iter_source = resolve_loop_iters(
        body, fn_substr, loop_iters=loop_iters, glsl_src=glsl_src,
        shader_path=shader_path,
    )

    # Structured-loop walk: track active loop merge labels and apply configured
    # iteration multipliers in encounter order. This gives a better dynamic
    # estimate than the old single "per-iter" bucket.
    loop_index = 0
    loop_stack: list[tuple[str, int]] = []

    static_counts = defaultdict(int)
    static_clocks = defaultdict(int)
    dyn_counts    = defaultdict(int)
    dyn_clocks    = defaultdict(int)

    cur_label = None
    for l in body:
        lm = re.match(r'\s+(%\S+)\s*=\s*OpLabel', l)
        if lm:
            cur_label = lm.group(1)
            while loop_stack and cur_label == loop_stack[-1][0]:
                loop_stack.pop()

        merge_m = re.search(r'OpLoopMerge\s+(%\S+)\s+(%\S+)', l)
        if merge_m:
            merge_label = merge_m.group(1)
            iters = resolved_iters[loop_index] if loop_index < len(resolved_iters) else 1
            loop_stack.append((merge_label, iters))
            loop_index += 1

        op, w = inst_weight(l)
        if op and w:
            mult = 1
            for _, iters in loop_stack:
                mult *= iters
            static_counts[op] += 1
            static_clocks[op] += w
            dyn_counts[op] += mult
            dyn_clocks[op] += w * mult

    total_static_inst = sum(static_counts.values())
    total_dyn_inst    = sum(dyn_counts.values())
    total_static_clk  = sum(static_clocks.values())
    total_dyn_clk     = sum(dyn_clocks.values())
    return {
        'fn_start': fn_start,
        'fn_end': fn_end,
        'body_len': len(body),
        'static_counts': static_counts,
        'static_clocks': static_clocks,
        'dyn_counts': dyn_counts,
        'dyn_clocks': dyn_clocks,
        'total_static_inst': total_static_inst,
        'total_dyn_inst': total_dyn_inst,
        'total_static_clk': total_static_clk,
        'total_dyn_clk': total_dyn_clk,
        'loop_iters': resolved_iters,
        'detected_iters': detected_iters,
        'iter_source': iter_source,
        'warnings': warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="glsl-profile.py")
    parser.add_argument("attr", nargs="?", default="windowClose",
                        help="shader to profile (windowClose, windowOpen, windowResize)")
    parser.add_argument("--store", dest="shader_store", default=None,
                        help="shader store directory (default: $NIRI_SHADER_STORE)")
    parser.add_argument("--file", dest="shader_file", default=str(DEFAULT_SHADERS),
                        help="path to niri-shaders.nix for nix-eval fallback")
    parser.add_argument("--anims", dest="anims_file", default=str(DEFAULT_ANIMS),
                        help="path to niri-shader-anims.nix for close_shredder constants")
    parser.add_argument("--fps", type=float, default=60.0,
                        help="simulation framerate (default: 60)")
    parser.add_argument("--duration-ms", type=float, default=3000.0,
                        help="close animation duration in ms (default: 3000)")
    parser.add_argument("--flatness-close-voronoi-raw", action="store_true",
                        help="raw GLSL/SPIR-V frame flatness by compiling per-frame constants")
    parser.add_argument("--flatness-close-b-raw", action="store_true",
                        help=argparse.SUPPRESS)  # alias for --flatness-close-voronoi-raw
    parser.add_argument("--raw-local-seeds", default="0.10,0.20,0.70,0.80",
                        help="comma-separated close_voronoi_crumble local seeds in [0,1]")
    return parser.parse_args()


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def parse_seed_list(seed_csv: str) -> list[float]:
    vals = []
    for tok in seed_csv.split(','):
        tok = tok.strip()
        if not tok:
            continue
        vals.append(float(tok))
    if not vals:
        return [0.10, 0.20, 0.70, 0.80]
    return vals


def compile_const_close(glsl: str, t_const: float, seed_const: float, spv_path: str) -> None:
    header = (
        CLOSE_HEADER_CONST
        .replace("__NIRI_T__", f"{t_const:.9f}")
        .replace("__NIRI_SEED__", f"{seed_const:.9f}")
    )
    src = header + '\n' + glsl + '\n' + CLOSE_MAIN
    compile_to_spv(src, spv_path, optimize=True)


def profile_frame_totals(
    asm: list[str], glsl: str, anims_path: Path | None = None,
) -> tuple[dict | None, str | None]:
    """Pick the best inlined/surviving function and auto-detect its loop bounds."""
    for fn in ('close_color', 'close_voronoi_crumble', 'main'):
        totals = collect_function_totals(
            asm, fn, glsl_src=glsl, shader_path=anims_path,
        )
        if totals is not None:
            return totals, fn
    return None, None


def print_close_voronoi_flatness_raw(
    glsl: str, args: argparse.Namespace, anims_path: Path,
) -> None:
    frames = max(2, int(round(args.duration_ms * args.fps / 1000.0)))
    local_seeds = parse_seed_list(args.raw_local_seeds)

    per_frame = []
    profiled_fn: str | None = None
    for fi in range(frames):
        t = fi / (frames - 1)
        frame_vals = []
        for ls in local_seeds:
            ls = clamp(ls, 0.0, 1.0)
            # close_voronoi_crumble uses niri_random_seed directly (no band rescaling)
            global_seed = ls
            with tempfile.NamedTemporaryFile(suffix='.spv', delete=False) as f:
                spv_path = f.name
            compile_const_close(glsl, t_const=t, seed_const=global_seed, spv_path=spv_path)
            asm = disassemble(spv_path)
            totals, fn = profile_frame_totals(asm, glsl, anims_path=anims_path)
            if fn is not None:
                profiled_fn = fn
            if totals is None:
                frame_vals.append(0.0)
            else:
                frame_vals.append(float(totals['total_dyn_clk']))
        per_frame.append(statistics.fmean(frame_vals) if frame_vals else 0.0)

    mean_v = statistics.fmean(per_frame)
    std_v = statistics.pstdev(per_frame)
    p95 = sorted(per_frame)[int(0.95 * (len(per_frame) - 1))]
    peak = max(per_frame)
    valley = min(per_frame)
    swing = peak - valley
    cv = (std_v / mean_v) if mean_v > 1e-9 else 0.0
    worst_step = max(abs(per_frame[i + 1] - per_frame[i]) for i in range(len(per_frame) - 1))

    print("\n" + "═" * 64)
    print("  close_voronoi_crumble frame flatness (raw GLSL compile)")
    print("═" * 64)
    print("  compile mode: glslang -Os (per-frame constants)")
    if profiled_fn:
        print(f"  profiled SPIR-V function: {profiled_fn}() (auto loop detection)")
    else:
        print("  warning: no profiled function found — frames may be zero")
    print(
        f"  sampling: {frames} frames @ {args.fps:g}fps over {args.duration_ms:g}ms; "
        f"local seeds={','.join(f'{s:.3f}' for s in local_seeds)}"
    )
    print("\n  metric                             value")
    print("  " + "─" * 44)
    print(f"  mean dyn-est clk/frame        {mean_v:>10.2f}")
    print(f"  stddev dyn-est clk/frame      {std_v:>10.2f}")
    print(f"  coeff. variation (std/mean)   {cv:>10.4f}")
    print(f"  min dyn-est clk/frame         {valley:>10.2f}")
    print(f"  p95 dyn-est clk/frame         {p95:>10.2f}")
    print(f"  max dyn-est clk/frame         {peak:>10.2f}")
    print(f"  peak-valley swing             {swing:>10.2f}")
    print(f"  worst adjacent-frame step     {worst_step:>10.2f}")


def profile_one(
    attr: str,
    shader_path: Path,
    anims_path: Path,
    store: Path | None,
    args: argparse.Namespace,
) -> dict | None:
    """Compile and analyse a single shader attribute."""
    is_close  = attr.startswith('windowClose')
    is_resize = attr.startswith('windowResize')
    if is_close:
        header, main_fn = CLOSE_HEADER, CLOSE_MAIN
    elif is_resize:
        header, main_fn = RESIZE_HEADER, RESIZE_MAIN
    else:
        header, main_fn = OPEN_HEADER, OPEN_MAIN

    glsl = extract_glsl(attr, shader_path, store)
    src  = header + '\n' + glsl + '\n' + main_fn

    flatness_raw = args.flatness_close_voronoi_raw or args.flatness_close_b_raw
    if flatness_raw and is_close:
        print_close_voronoi_flatness_raw(glsl, args, anims_path)
        return None

    with tempfile.NamedTemporaryFile(suffix='.spv', delete=False) as f:
        spv_path = f.name

    # Resize helpers don't inline without -Os; use optimize mode so everything
    # lands in main() and we see the true per-fragment cost.
    optimize = is_resize
    compile_to_spv(src, spv_path, optimize=optimize)
    asm = disassemble(spv_path)
    mode = "glslang -Os (inlined into main)" if optimize else "glslang (default)"
    print(f"Total SPIR-V lines: {len(asm)}")
    print(f"Compile mode: {mode}")

    W = 64
    if is_close:
        fns = [
            ('close_voronoi_crumble', 'close_voronoi_crumble'),
            ('close_shredder', 'close_shredder'),
        ]
    elif is_resize:
        # With -Os the helpers inline into main(); profile that.
        fns = [('resize_color (inlined → main)', 'main')]
    else:
        fns = [
            ('open_voronoi_shatter', 'open_voronoi_shatter'),
            ('ngon_reveal', 'ngon_reveal'),
        ]

    v_totals = None
    for display, substr in fns:
        print(f"\n{'═'*W}")
        print(f"  {display}()")
        print(f"{'═'*W}")
        analyse_function(asm, substr, glsl_src=glsl, shader_path=anims_path)
        if substr == 'close_voronoi_crumble':
            v_totals = collect_function_totals(
                asm, substr, glsl_src=glsl, shader_path=anims_path,
            )

    return v_totals


def resolve_shader_store(args: argparse.Namespace) -> Path | None:
    raw = args.shader_store or os.environ.get("NIRI_SHADER_STORE")
    if not raw:
        return None
    store = Path(raw).expanduser().resolve()
    if not store.is_dir():
        sys.exit(f"Shader store is not a directory: {store}")
    return store


def main() -> None:
    args = parse_args()
    shader_path = Path(args.shader_file).resolve()
    anims_path = Path(args.anims_file).resolve()
    store = resolve_shader_store(args)

    attr = args.attr
    if store is not None:
        print(f"Reading {attr} from shader store {store}...")
    else:
        print(f"Extracting {attr} from {shader_path} (nix eval fallback)...")
    profile_one(attr, shader_path, anims_path, store, args)


if __name__ == '__main__':
    main()
