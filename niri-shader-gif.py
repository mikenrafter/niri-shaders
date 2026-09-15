#!/usr/bin/env python3
"""
Render seamless showcase GIFs from assembled niri window shaders.

Close timeline (loop), progress always 0→1→0:
  1. play forward at 1.0× (0 → 1)
  2. hold at 1 for 600 ms
  3. rewind at 2.0× (1 → 0)
  4. hold at 0 for 300 ms, then loop

Total close loop = duration_ms × 1.5 + 600 + 300 ms.

Durations come from niri-shader-anims.nix (DUR_CLOSE_* / DUR_RESIZE_CRT).

Resize: ping-pong small→medium→large→medium→small at DUR_RESIZE_CRT each leg,
then hold 300 ms on small before loop.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import moderngl
from PIL import Image

GEO_TO_TEX_YFLIP = """\
const mat3 niri_geo_to_tex = mat3(
    1.0,  0.0, 0.0,
    0.0, -1.0, 0.0,
    0.0,  1.0, 1.0
);
"""

GEO_TO_TEX_PAIR_YFLIP = """\
const mat3 niri_geo_to_tex_prev = mat3(
    1.0,  0.0, 0.0,
    0.0, -1.0, 0.0,
    0.0,  1.0, 1.0
);
const mat3 niri_geo_to_tex_next = mat3(
    1.0,  0.0, 0.0,
    0.0, -1.0, 0.0,
    0.0,  1.0, 1.0
);
"""

VERT = """\
#version 330 core
in vec2 in_vert;
void main() { gl_Position = vec4(in_vert, 0.0, 1.0); }
"""

# Canvas-relative close/open: cg can be outside [0,1] so travel/fall is visible.
CLOSE_OPEN_HEADER = """\
#version 330 core
uniform vec2 u_resolution;
uniform float u_progress;
uniform sampler2D u_tex0;
uniform sampler2D u_tex1;
uniform vec2 niri_window_pos;
uniform vec2 niri_window_size;
uniform vec2 niri_output_size;
uniform vec2 niri_size;
uniform vec2 niri_geo_size;
uniform float niri_alpha;
uniform float niri_scale;
uniform float niri_is_tabbed;
uniform float niri_total_columns;
uniform float niri_windows_in_column;
uniform float niri_window_index_in_column;
uniform float niri_columns_in_workspace;
uniform float niri_column_index_in_workspace;
#define niri_tex u_tex0
#define niri_clamped_progress (u_progress)
#define niri_progress (u_progress)
#define niri_random_seed 0.5
{geo_to_tex}
out vec4 fragColor;
"""

CLOSE_OPEN_MAIN = """\
void main() {{
    // Top-left origin pixels in output/canvas space.
    vec2 out_px = vec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y);
    vec2 cg_xy = (out_px - niri_window_pos) / max(niri_window_size, vec2(1.0));
    vec3 cg = vec3(cg_xy, 1.0);
    vec3 sg = vec3(niri_window_size, 1.0);
    vec4 result = {entry}(cg, sg);
    // close_* returns premultiplied alpha.
    vec4 bg = texture(u_tex1, gl_FragCoord.xy / u_resolution.xy);
    fragColor = result + bg * (1.0 - result.a);
}}
"""

RESIZE_HEADER = """\
#version 330 core
uniform vec2 u_resolution;
uniform float u_progress;
uniform sampler2D u_tex0;
uniform sampler2D u_tex1;
uniform sampler2D u_tex2;
uniform vec2 niri_window_pos;
uniform vec2 niri_window_size;
uniform vec2 niri_output_size;
uniform vec2 niri_size;
uniform float niri_alpha;
uniform float niri_scale;
#define niri_tex_prev u_tex0
#define niri_tex_next u_tex1
#define niri_clamped_progress (u_progress)
{geo_to_tex_pair}
out vec4 fragColor;
"""

RESIZE_MAIN = """\
void main() {
    vec2 out_px = vec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y);
    vec2 cg_xy = (out_px - niri_window_pos) / max(niri_window_size, vec2(1.0));
    // Outside the current window rect → backdrop only.
    if (cg_xy.x < 0.0 || cg_xy.x > 1.0 || cg_xy.y < 0.0 || cg_xy.y > 1.0) {
        fragColor = texture(u_tex2, gl_FragCoord.xy / u_resolution.xy);
        return;
    }
    vec3 cg = vec3(cg_xy, 1.0);
    vec3 sg = vec3(niri_window_size, 1.0);
    vec4 result = resize_color(cg, sg);
    vec4 bg = texture(u_tex2, gl_FragCoord.xy / u_resolution.xy);
    fragColor = mix(bg, result, result.a);
}
"""

ANIM_TO_FILE = {
    "windowClose": "windowClose.glsl",
    "windowOpen": "windowOpen.glsl",
    "windowResize": "windowResize.glsl",
}
ANIM_TO_ENTRY = {
    "windowClose": "close_color",
    "windowOpen": "open_color",
    "windowResize": "resize_color",
}

HOLD_AT_1_S = 0.600    # wait at progress=1 before rewinding (closes)
HOLD_AT_0_S = 0.300    # wait at progress=0 after rewind / resize settle
REVERSE_SPEED = 2.0    # rewind multiplier for close clips
# 50 fps → GIF delay is exactly 2 cs (20 ms). At 60 fps ffmpeg writes
# delay=1 (10 ms) on 1/3 of frames; browsers clamp those to 20 ms → 1.2× slow.
FPS = 50
# Canonical wall-clock durations from niri-shader-anims.nix
DUR_CLOSE_UNHOOK_MS = 900
DUR_CLOSE_SHREDDER_MS = 3000
DUR_CLOSE_VORONOI_MS = 1200
DUR_RESIZE_CRT_MS = 1000


def progress_timeline(duration_ms: int, fps: int = FPS) -> list[float]:
    """Forward 0→1 at 1×, hold at 1, rewind at REVERSE_SPEED, hold at 0.

    Total = duration_ms * (1 + 1/REVERSE_SPEED) + HOLD_AT_1_S*1000 + HOLD_AT_0_S*1000
    e.g. 900 → 900*1.5 + 600 + 300 = 2250 ms.
    """
    dur_s = duration_ms / 1000.0
    frames: list[float] = []

    n_fwd = max(1, round(dur_s * fps))
    for i in range(n_fwd):
        frames.append(i / max(n_fwd - 1, 1))

    frames.extend([1.0] * max(1, round(HOLD_AT_1_S * fps)))

    n_rev = max(1, round((dur_s / REVERSE_SPEED) * fps))
    for i in range(n_rev):
        frames.append(1.0 - (i / max(n_rev - 1, 1)))

    frames.extend([0.0] * max(1, round(HOLD_AT_0_S * fps)))
    return frames


def resize_chain_timeline(
    legs: list[tuple[Path, Path]],
    duration_ms: int = DUR_RESIZE_CRT_MS,
    fps: int = FPS,
) -> list[tuple[Path, Path, float]]:
    """Ping-pong legs at DUR_RESIZE_CRT each, then HOLD_AT_0_S on the final size.

    Shared endpoints between legs are not duplicated (leg N's t=1 is leg N+1's t=0).
    """
    n_seg = max(1, round((duration_ms / 1000.0) * fps))
    n_hold = max(1, round(HOLD_AT_0_S * fps))
    out: list[tuple[Path, Path, float]] = []

    for i, (prev, nxt) in enumerate(legs):
        start = 0 if i == 0 else 1
        for j in range(start, n_seg):
            t = j / max(n_seg - 1, 1)
            out.append((prev, nxt, t))

    out.extend([(legs[-1][1], legs[-1][1], 1.0)] * n_hold)
    return out


def load_shader_body(anim: str) -> str:
    store_raw = os.environ.get("NIRI_SHADER_STORE")
    if not store_raw:
        sys.exit(
            "NIRI_SHADER_STORE is not set.\n"
            "  export NIRI_SHADER_STORE=$(nix build --no-link --print-out-paths "
            ".#checks.x86_64-linux.default)"
        )
    store = Path(store_raw).resolve()
    path = store / ANIM_TO_FILE[anim]
    if not path.is_file():
        sys.exit(f"missing {path}")
    return path.read_text(encoding="utf-8")


def load_rgba_gl(path: Path, size: tuple[int, int]) -> bytes:
    """RGBA bytes for moderngl (bottom-left origin)."""
    img = Image.open(path).convert("RGBA")
    img = img.resize(size, Image.Resampling.LANCZOS)
    # PIL top-left → OpenGL bottom-left
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img.tobytes()


def make_ctx_program(frag_src: str):
    ctx = moderngl.create_standalone_context(require=330)
    prog = ctx.program(vertex_shader=VERT, fragment_shader=frag_src)
    vbo = ctx.buffer(
        b"".join(struct.pack("2f", *xy) for xy in [(-1, -1), (1, -1), (-1, 1), (1, 1)])
    )
    vao = ctx.simple_vertex_array(prog, vbo, "in_vert")
    return ctx, prog, vao


def set_if(prog, name: str, val) -> None:
    if name in prog:
        prog[name] = val


def write_gif(frames_dir: Path, n_frames: int, out: Path) -> None:
    # full stats + no dither: bayer/diff mode was quantizing near-black terminal
    # greens into (0,16,0)-style bands on the DMS backdrop.
    palette = frames_dir / "palette.png"
    pattern = str(frames_dir / "frame_%05d.png")
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(FPS), "-i", pattern,
            "-vf", "palettegen=max_colors=256:stats_mode=full",
            str(palette),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(FPS), "-i", pattern,
            "-i", str(palette),
            "-lavfi", "paletteuse=dither=none:diff_mode=rectangle",
            "-loop", "0",
            str(out),
        ],
        check=True,
    )
    # GIF delays are centiseconds so 60fps becomes 10/20 ms pairs; totals still
    # average to n_frames/FPS seconds (verified against duration_ms math).
    print(
        f"[niri-shader-gif] wrote {out} "
        f"({n_frames} frames @ {FPS}fps, {round(n_frames * 1000 / FPS)} ms)"
    )


def save_fbo(fbo, width: int, height: int, path: Path) -> None:
    data = fbo.read(components=4, alignment=1)
    img = Image.frombytes("RGBA", (width, height), data).transpose(
        Image.Transpose.FLIP_TOP_BOTTOM
    ).convert("RGB")
    img.save(path)


def render_close_open_gif(
    *,
    anim: str,
    texture: Path,
    backdrop: Path,
    duration_ms: int,
    out: Path,
    canvas: tuple[int, int],
    window_size: tuple[int, int],
    window_pos: tuple[float, float],
    output_size: tuple[float, float] | None = None,
) -> None:
    body = load_shader_body(anim)
    entry = ANIM_TO_ENTRY[anim]
    frag_src = (
        CLOSE_OPEN_HEADER.format(geo_to_tex=GEO_TO_TEX_YFLIP)
        + body
        + CLOSE_OPEN_MAIN.format(entry=entry)
    )
    ctx, prog, vao = make_ctx_program(frag_src)
    width, height = canvas
    fbo = ctx.framebuffer(color_attachments=[ctx.texture((width, height), 4)])

    win_w, win_h = window_size
    tex0 = ctx.texture((win_w, win_h), 4, load_rgba_gl(texture, (win_w, win_h)))
    tex1 = ctx.texture((width, height), 4, load_rgba_gl(backdrop, (width, height)))
    tex0.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex1.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex0.use(0)
    tex1.use(1)
    set_if(prog, "u_tex0", 0)
    set_if(prog, "u_tex1", 1)

    res = (float(width), float(height))
    # Logical output for physics (fall clearance, travel). Keep close to a real
    # monitor so shredder fall_time_scale stays ~1; canvas may be larger for framing.
    out_size = output_size if output_size is not None else res
    win_size_f = (float(win_w), float(win_h))
    win_pos_f = (float(window_pos[0]), float(window_pos[1]))
    for name, val in (
        ("u_resolution", res),
        ("niri_size", res),
        ("niri_output_size", (float(out_size[0]), float(out_size[1]))),
        ("niri_geo_size", win_size_f),
        ("niri_window_size", win_size_f),
        ("niri_window_pos", win_pos_f),
        ("niri_alpha", 1.0),
        ("niri_scale", 1.0),
        ("niri_is_tabbed", 0.0),
        ("niri_total_columns", 1.0),
        ("niri_windows_in_column", 1.0),
        ("niri_window_index_in_column", 0.0),
        ("niri_columns_in_workspace", 1.0),
        ("niri_column_index_in_workspace", 0.0),
    ):
        set_if(prog, name, val)

    frames = progress_timeline(duration_ms)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="niri-gif-") as td:
        td_path = Path(td)
        for i, prog_val in enumerate(frames):
            prog["u_progress"] = float(prog_val)
            fbo.use()
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            vao.render(moderngl.TRIANGLE_STRIP)
            save_fbo(fbo, width, height, td_path / f"frame_{i:05d}.png")
        write_gif(td_path, len(frames), out)
    ctx.release()


def render_resize_chain_gif(
    *,
    backdrop: Path,
    legs: list[tuple[Path, Path]],
    duration_ms: int,
    out: Path,
    canvas: tuple[int, int],
) -> None:
    body = load_shader_body("windowResize")
    frag_src = (
        RESIZE_HEADER.format(geo_to_tex_pair=GEO_TO_TEX_PAIR_YFLIP)
        + body
        + RESIZE_MAIN
    )
    ctx, prog, vao = make_ctx_program(frag_src)
    width, height = canvas
    fbo = ctx.framebuffer(color_attachments=[ctx.texture((width, height), 4)])

    bg_tex = ctx.texture((width, height), 4, load_rgba_gl(backdrop, (width, height)))
    bg_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

    # Cache resized textures per path at canvas max window box.
    # Per-frame we upload at current lerped size.
    img_cache: dict[Path, Image.Image] = {}

    def pil_of(path: Path) -> Image.Image:
        if path not in img_cache:
            img_cache[path] = Image.open(path).convert("RGBA")
        return img_cache[path]

    def native_size(path: Path) -> tuple[int, int]:
        im = pil_of(path)
        return im.size

    timeline = resize_chain_timeline(legs, duration_ms)
    out.parent.mkdir(parents=True, exist_ok=True)

    res = (float(width), float(height))
    set_if(prog, "u_resolution", res)
    set_if(prog, "niri_size", res)
    set_if(prog, "niri_output_size", res)
    set_if(prog, "niri_alpha", 1.0)
    set_if(prog, "niri_scale", 1.0)

    with tempfile.TemporaryDirectory(prefix="niri-gif-resize-") as td:
        td_path = Path(td)
        prev_tex = next_tex = None
        for i, (prev_path, next_path, t) in enumerate(timeline):
            pw, ph = native_size(prev_path)
            nw, nh = native_size(next_path)
            curr_w = max(1, int(round(pw + (nw - pw) * t)))
            curr_h = max(1, int(round(ph + (nh - ph) * t)))
            # Scale both sources into current geo (identity UV mapping).
            prev_bytes = load_rgba_gl(prev_path, (curr_w, curr_h))
            next_bytes = load_rgba_gl(next_path, (curr_w, curr_h))
            if prev_tex is not None:
                prev_tex.release()
            if next_tex is not None:
                next_tex.release()
            prev_tex = ctx.texture((curr_w, curr_h), 4, prev_bytes)
            next_tex = ctx.texture((curr_w, curr_h), 4, next_bytes)
            prev_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            next_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            prev_tex.use(0)
            next_tex.use(1)
            bg_tex.use(2)
            set_if(prog, "u_tex0", 0)
            set_if(prog, "u_tex1", 1)
            set_if(prog, "u_tex2", 2)

            win_x = (width - curr_w) * 0.5
            win_y = (height - curr_h) * 0.5
            set_if(prog, "niri_window_size", (float(curr_w), float(curr_h)))
            set_if(prog, "niri_window_pos", (float(win_x), float(win_y)))
            prog["u_progress"] = float(t)

            fbo.use()
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            vao.render(moderngl.TRIANGLE_STRIP)
            save_fbo(fbo, width, height, td_path / f"frame_{i:05d}.png")

        write_gif(td_path, len(timeline), out)

    if prev_tex is not None:
        prev_tex.release()
    if next_tex is not None:
        next_tex.release()
    ctx.release()


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("anim", nargs="?", default="windowClose", choices=list(ANIM_TO_FILE))
    p.add_argument("--texture", type=Path, help="Window content (close/open)")
    p.add_argument("--backdrop", required=True, type=Path)
    p.add_argument("--duration-ms", type=int, required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--canvas-width", type=int, default=1280)
    p.add_argument("--canvas-height", type=int, default=720)
    p.add_argument("--window-width", type=int, default=0, help="0 = derive from texture")
    p.add_argument("--window-height", type=int, default=0)
    p.add_argument(
        "--layout",
        choices=("center", "right-half"),
        default="center",
        help="Window placement on canvas (right-half for voronoi throw-left)",
    )
    p.add_argument("--resize-small", type=Path)
    p.add_argument("--resize-medium", type=Path)
    p.add_argument("--resize-large", type=Path)
    args = p.parse_args()

    if not args.backdrop.is_file():
        sys.exit(f"--backdrop not found: {args.backdrop}")

    if args.anim == "windowResize":
        for label, path in (
            ("--resize-small", args.resize_small),
            ("--resize-medium", args.resize_medium),
            ("--resize-large", args.resize_large),
        ):
            if path is None or not path.is_file():
                sys.exit(f"{label} required/found for windowResize")
        legs = [
            (args.resize_small, args.resize_medium),
            (args.resize_medium, args.resize_large),
            (args.resize_large, args.resize_medium),
            (args.resize_medium, args.resize_small),
        ]
        # Canvas fits large terminal with a little margin.
        lw, lh = Image.open(args.resize_large).size
        canvas = (max(args.canvas_width, lw + 80), max(args.canvas_height, lh + 80))
        render_resize_chain_gif(
            backdrop=args.backdrop,
            legs=legs,
            duration_ms=args.duration_ms,
            out=args.out,
            canvas=canvas,
        )
        return

    if args.texture is None or not args.texture.is_file():
        sys.exit("--texture required for close/open")

    tw, th = Image.open(args.texture).size
    win_w = args.window_width or tw
    win_h = args.window_height or th
    canvas_w, canvas_h = args.canvas_width, args.canvas_height

    if args.layout == "right-half":
        # Moderate throw: window ~40% of canvas width, left edge near midpoint.
        # travel_end = pos.x/win_w ≈ 0.75 — readable right→left without a sprint.
        scale = min(1.0, (canvas_w * 0.40) / win_w, (canvas_h * 0.80) / win_h)
        win_w = max(1, int(win_w * scale))
        win_h = max(1, int(win_h * scale))
        win_x = canvas_w * 0.50
        win_y = (canvas_h - win_h) * 0.5
        # Logical output == canvas so travel lands near the left edge.
        out_size = (float(canvas_w), float(canvas_h))
    else:
        scale = min(1.0, (canvas_w * 0.55) / win_w, (canvas_h * 0.70) / win_h)
        win_w = max(1, int(win_w * scale))
        win_h = max(1, int(win_h * scale))
        win_x = (canvas_w - win_w) * 0.5
        win_y = canvas_h * 0.10
        # Physics output: just enough margin below the window that pieces clear
        # without inflating fall_time_scale (tall canvases used to slow shredder).
        phys_h = win_y + win_h + win_h * 0.35
        out_size = (float(canvas_w), float(phys_h))

    render_close_open_gif(
        anim=args.anim,
        texture=args.texture,
        backdrop=args.backdrop,
        duration_ms=args.duration_ms,
        out=args.out,
        canvas=(canvas_w, canvas_h),
        window_size=(win_w, win_h),
        window_pos=(win_x, win_y),
        output_size=out_size,
    )


if __name__ == "__main__":
    main()
