#!/usr/bin/env python3
"""
niri-glslviewer — interactive GLSL previewer for niri window animation shaders.

Reads validated shader bodies from $NIRI_SHADER_STORE (build output of
niri-shader-check.nix), wraps them in a glslViewer-compatible GLSL 330 header,
and launches glslViewer.

Controls in glslViewer:
  Mouse X  — scrubs niri_clamped_progress (left=0.0, right=1.0)  [default mode]
  Time     — auto-plays niri_clamped_progress 0→1 over ~1.25 s   [--play mode]
  q / Esc  — quit

Usage:
  niri-glslviewer [windowClose|windowOpen|windowResize] [-p]
    [--texture PATH]   # primary window content OR post-resize (niri_tex / niri_tex_next)
    [--before PATH]    # pre-resize frame, niri_tex_prev (resize only)
    [--backdrop PATH]  # desktop background (composited under result in main())

  windowClose     preview the close animation (default)
  windowOpen      preview the open animation
  windowResize    preview the resize animation (CRT cross-fade)
  -p / --play     auto-play instead of mouse-scrub

Shader source:
  Set NIRI_SHADER_STORE to the directory containing windowClose.glsl,
  windowOpen.glsl, and windowResize.glsl (written by niri-shader-check.nix).
  The NixOS/home-manager wrapper sets this automatically at build time.

  Dev fallback:
    export NIRI_SHADER_STORE=$(nix build .#checks.niri-shaders --print-out-paths)
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

# glslViewer built-ins:
#   u_resolution  vec2  — viewport size in pixels
#   u_mouse       vec2  — mouse position in pixels
#   u_time        float — seconds since launch
#   u_texN        sampler2D — texture slots (positional args after .frag)

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

OPEN_CLOSE_HEADER = """\
#version 330 core

uniform vec2      u_resolution;
uniform vec2      u_mouse;
uniform float     u_time;
uniform sampler2D u_tex0;
{backdrop_uniform}
#define niri_tex u_tex0

#define niri_clamped_progress ({progress_expr})
#define niri_random_seed 0.5

{geo_to_tex}

out vec4 fragColor;
"""

RESIZE_HEADER = """\
#version 330 core

uniform vec2      u_resolution;
uniform vec2      u_mouse;
uniform float     u_time;
uniform sampler2D u_tex0;
uniform sampler2D u_tex1;
{backdrop_uniform}
#define niri_tex_prev u_tex0
#define niri_tex_next u_tex1

#define niri_clamped_progress ({progress_expr})

{geo_to_tex_pair}

out vec4 fragColor;
"""

OPEN_CLOSE_MAIN = """\
void main() {{
    vec3 cg = vec3(gl_FragCoord.x / u_resolution.x,
                   1.0 - gl_FragCoord.y / u_resolution.y,
                   1.0);
    vec3 sg = vec3(u_resolution.xy, 1.0);
    vec4 result = {entry}(cg, sg);
{backdrop_composite}
}}
"""

RESIZE_MAIN = """\
void main() {{
    vec3 cg = vec3(gl_FragCoord.x / u_resolution.x,
                   1.0 - gl_FragCoord.y / u_resolution.y,
                   1.0);
    vec3 sg = vec3(u_resolution.xy, 1.0);
    vec4 result = resize_color(cg, sg);
{backdrop_composite}
}}
"""

BACKDROP_UNIFORM = "uniform sampler2D u_tex1;"
BACKDROP_UNIFORM_RESIZE = "uniform sampler2D u_tex2;"
BACKDROP_COMPOSITE = """\
    vec4 bg = texture(u_tex1, gl_FragCoord.xy / u_resolution.xy);
    fragColor = mix(bg, result, result.a);"""
BACKDROP_COMPOSITE_RESIZE = """\
    vec4 bg = texture(u_tex2, gl_FragCoord.xy / u_resolution.xy);
    fragColor = mix(bg, result, result.a);"""
NO_BACKDROP_COMPOSITE = "    fragColor = result;"

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


def shader_store_error() -> str:
    return (
        "NIRI_SHADER_STORE is not set.\n"
        "\n"
        "niri-glslviewer reads pre-validated GLSL from the shader store build\n"
        "output — it does not run `nix eval` at preview time.\n"
        "\n"
        "Set the store path manually for development:\n"
        "  export NIRI_SHADER_STORE=$(nix build .#checks.niri-shaders --print-out-paths)\n"
        "\n"
        "Or run via the home-manager wrapper (sets NIRI_SHADER_STORE at build time).\n"
        "Until checks.niri-shaders exists, you can point at any directory containing\n"
        "windowClose.glsl, windowOpen.glsl, and windowResize.glsl."
    )


def load_shader_body(anim: str) -> tuple[str, Path]:
    store_raw = os.environ.get("NIRI_SHADER_STORE")
    if not store_raw:
        sys.exit(shader_store_error())

    store = Path(store_raw).resolve()
    if not store.is_dir():
        sys.exit(f"NIRI_SHADER_STORE is not a directory: {store}")

    shader_file = store / ANIM_TO_FILE[anim]
    if not shader_file.is_file():
        sys.exit(
            f"Shader file not found: {shader_file}\n"
            f"Expected {ANIM_TO_FILE[anim]} under NIRI_SHADER_STORE={store}"
        )

    return shader_file.read_text(encoding="utf-8"), store


def progress_expr(play: bool) -> str:
    if play:
        return "clamp(mod(u_time, 1.25) / 1.25, 0.0, 1.0)"
    return "clamp(u_mouse.x / u_resolution.x, 0.0, 1.0)"


def build_frag(
    body: str,
    anim: str,
    *,
    play: bool,
    backdrop: bool,
) -> str:
    prog = progress_expr(play)

    if anim == "windowResize":
        header = RESIZE_HEADER.format(
            progress_expr=prog,
            backdrop_uniform=BACKDROP_UNIFORM_RESIZE if backdrop else "",
            geo_to_tex_pair=GEO_TO_TEX_PAIR_YFLIP,
        )
        composite = (
            BACKDROP_COMPOSITE_RESIZE if backdrop else NO_BACKDROP_COMPOSITE
        )
        main = RESIZE_MAIN.format(backdrop_composite=composite)
    else:
        entry = ANIM_TO_ENTRY[anim]
        header = OPEN_CLOSE_HEADER.format(
            progress_expr=prog,
            backdrop_uniform=BACKDROP_UNIFORM if backdrop else "",
            geo_to_tex=GEO_TO_TEX_YFLIP,
        )
        composite = BACKDROP_COMPOSITE if backdrop else NO_BACKDROP_COMPOSITE
        main = OPEN_CLOSE_MAIN.format(entry=entry, backdrop_composite=composite)

    return header + body + main


def resolve_texture(path: str, label: str) -> str:
    tex_path = os.path.abspath(path)
    if not os.path.exists(tex_path):
        sys.exit(f"{label} file not found: {tex_path}")
    return tex_path


def build_glslviewer_cmd(
    frag_path: str,
    anim: str,
    *,
    texture: str | None,
    before: str | None,
    backdrop: str | None,
) -> list[str]:
    cmd = ["glslViewer", frag_path]

    if anim == "windowResize":
        # glslViewer assigns u_tex0, u_tex1, u_tex2 in argument order.
        slots: list[str] = []
        if before:
            slots.append(resolve_texture(before, "--before"))
        if texture:
            slots.append(resolve_texture(texture, "--texture"))
        if backdrop:
            slots.append(resolve_texture(backdrop, "--backdrop"))
        cmd.extend(slots)
    else:
        if texture:
            cmd.append(resolve_texture(texture, "--texture"))
        if backdrop:
            cmd.append(resolve_texture(backdrop, "--backdrop"))

    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "anim",
        nargs="?",
        default="windowClose",
        choices=list(ANIM_TO_FILE),
        help="Which animation to preview (default: windowClose)",
    )
    parser.add_argument(
        "-p", "--play",
        action="store_true",
        help="Auto-play mode: time-driven progress instead of mouse scrub",
    )
    parser.add_argument(
        "--texture",
        metavar="PATH",
        help="Window texture (open/close: niri_tex) or post-resize frame (niri_tex_next)",
    )
    parser.add_argument(
        "--before",
        metavar="PATH",
        help="Pre-resize frame (windowResize only: niri_tex_prev)",
    )
    parser.add_argument(
        "--backdrop",
        metavar="PATH",
        help="Desktop background composited under the animation result",
    )
    args = parser.parse_args()

    if args.before and args.anim != "windowResize":
        sys.exit("--before is only valid for windowResize")

    body, store = load_shader_body(args.anim)
    entry = ANIM_TO_ENTRY[args.anim]

    print(f"[niri-glslviewer] store: {store}")
    print(f"[niri-glslviewer] shader: {ANIM_TO_FILE[args.anim]} → {entry}()")

    frag_src = build_frag(
        body,
        args.anim,
        play=args.play,
        backdrop=args.backdrop is not None,
    )

    tmp = tempfile.NamedTemporaryFile(
        suffix=f"_{args.anim}.frag",
        prefix="niri_preview_",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    tmp.write(frag_src)
    tmp.flush()
    tmp_path = tmp.name
    tmp.close()

    cmd = build_glslviewer_cmd(
        tmp_path,
        args.anim,
        texture=args.texture,
        before=args.before,
        backdrop=args.backdrop,
    )

    if args.play:
        mode_hint = "AUTO-PLAY: progress loops 0→1 every 1.25 s"
    else:
        mode_hint = "MOUSE SCRUB: move mouse horizontally — left=0.0, right=1.0"

    print()
    print(f"  Shader    : {args.anim}")
    print(f"  Temp file : {tmp_path}")
    print(f"  Mode      : {mode_hint}")
    if args.texture:
        print(f"  Texture   : {args.texture}")
    if args.before:
        print(f"  Before    : {args.before}")
    if args.backdrop:
        print(f"  Backdrop  : {args.backdrop}")
    print()
    print("  q / Esc to quit glslViewer.")
    print()

    os.execvp("glslViewer", cmd)


if __name__ == "__main__":
    main()
