#!/usr/bin/env python3
"""
Assemble a niri custom shader like runtime does and compile with glslangValidator.

Niri prepends #version, then prelude + user body + epilogue. This gate uses the
same wrapping so failures match what the compositor would see at first use.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def assemble(version_line: str, prelude: str, body: str, epilogue: str) -> str:
    parts = [version_line.strip(), prelude.rstrip(), body.rstrip(), epilogue.lstrip()]
    return "\n".join(part for part in parts if part) + "\n"


def safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label)


def compile_glsl(
    *,
    glslang: str,
    source: str,
    label: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".frag", prefix=f"niri-{safe_label(label)}-", delete=False
    ) as tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)

    # Parse/validate only — niri's GLES driver compiles at runtime; glslang -G
    # targets SPIR-V and rejects ES 300 (needs 310+) so we do not use -G here.
    cmd = [glslang, "-S", "frag", str(tmp_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        sys.stderr.write(f"niri-shader-glsl-compile: {label} failed\n")
        if proc.stdout:
            sys.stderr.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compile niri-wrapped GLSL via glslangValidator")
    ap.add_argument("--glslang", default="glslangValidator", help="glslangValidator binary")
    ap.add_argument("--version-line", required=True, help='e.g. "#version 300 es"')
    ap.add_argument("--prelude-file", type=Path, required=True)
    ap.add_argument("--epilogue-file", type=Path, required=True)
    ap.add_argument("--body-file", type=Path, required=True)
    ap.add_argument("--label", default="shader", help="diagnostic label")
    args = ap.parse_args()

    source = assemble(
        args.version_line,
        args.prelude_file.read_text(),
        args.body_file.read_text(),
        args.epilogue_file.read_text(),
    )
    compile_glsl(
        glslang=args.glslang,
        source=source,
        label=args.label,
    )


if __name__ == "__main__":
    main()
