# Build-time validation for niri custom window shaders.
#
# - GLSL compile gate (niri prelude/epilogue + extracted #version)
# - Seed-scaling isolation on close/open animation bodies (resize exempt)
# - Writes user-body GLSL + meta.json for glslviewer / HM consumers
#
# Arguments:
#   niriPkg         — patched niri (same as home/niri.nix)
#   shaderProfile   — profile whose assembled bodies land in $out/*.glsl
#   shaderProfiles  — profiles to validate (default: [ shaderProfile ])

{ pkgs
, lib
, niriPkg
, shaderProfile ? "full"
, shaderProfiles ? null
, niriPatches ? [
    ./patches/niri-glsl-es300.patch
    ./patches/niri-is-floating-prelim.patch
    ./patches/niri-window-uniforms.patch
  ]
}:

let
  shaderLib = import ./niri-shader-lib.nix { inherit pkgs lib; };
  animsRegistry = import ./niri-shader-anims.nix;

  patchedNiriPkg = niriPkg // {
    src = pkgs.applyPatches {
      name = "niri-src-shader-check";
      src = niriPkg.src;
      patches = niriPatches;
    };
  };

  profilesToValidate =
    if shaderProfiles == null then [ shaderProfile ] else shaderProfiles;

  mkShaders = profile:
    import ./niri-shaders.nix { inherit pkgs niriPkg; shaderProfile = profile; };

  shaderSets = lib.genAttrs profilesToValidate mkShaders;
  outputShaders = shaderSets.${shaderProfile};

  # Bodies to scan for forbidden seed routing (close/open only).
  seedCheckBodies =
    lib.unique (lib.flatten (lib.mapAttrsToList (_: profile:
      map (a: a.body) (profile.close ++ profile.open)
    ) (lib.genAttrs profilesToValidate (p: animsRegistry.${p}))));

  seedCheckBodyFiles = map (body:
    pkgs.writeText "niri-seed-check-body.frag" body
  ) seedCheckBodies;

  seedPatterns = [
    "niri_random_seed"
    "seed /"
    "(seed -"
    ")/ 0."
  ];

  # Prelude/epilogue/#version come from the patched niri source at build time;
  # reading them at eval time would be import-from-derivation, which breaks
  # `nix flake check --no-build` on a cold store.
  patchedSrc = patchedNiriPkg.src;
  shaderDir = "${patchedSrc}/src/render_helpers/shaders";

  eventTypes = [
    { key = "windowClose"; stem = "close"; }
    { key = "windowOpen"; stem = "open"; }
    { key = "windowResize"; stem = "resize"; }
  ];

  bodyFiles = lib.genAttrs profilesToValidate (profile:
    lib.genAttrs [ "windowClose" "windowOpen" "windowResize" ] (key:
      pkgs.writeText "niri-${profile}-${key}.frag" shaderSets.${profile}.${key}
    )
  );

in
pkgs.runCommand "niri-shader-check"
  {
    nativeBuildInputs = [ pkgs.glslang pkgs.python3 ];
    passthru = {
      inherit shaderSets;
    };
  }
  ''
    set -euo pipefail

    compile_py=${pkgs.python3}/bin/python3
    compile_script=${./niri-shader-glsl-compile.py}
    glslang_bin=${pkgs.glslang}/bin/glslangValidator

    # ── #version / prelude / epilogue from patched niri source ───────────────
    # Mirrors shaderLib.readNiriShaderCompileInfo.
    elem_rs=${patchedSrc}/src/render_helpers/shader_element.rs
    if grep -Fq '#version 300 es' "$elem_rs"; then
      version_line='#version 300 es'
    else
      version_line='#version 100'
    fi

    frag_dir=$TMPDIR/frag
    mkdir -p "$frag_dir"
    ${lib.concatMapStringsSep "\n" (ev: ''
      cp ${shaderDir}/${ev.stem}_prelude.frag "$frag_dir/${ev.key}-prelude.frag"
      cat ${shaderDir}/${ev.stem}_epilogue.frag ${shaderDir}/rounding_alpha.frag \
        > "$frag_dir/${ev.key}-epilogue.frag"
    '') eventTypes}

    # ── committed Voronoi bake matches generator ─────────────────────────────
    if ! diff -q ${shaderLib.voronoiBakeDrv}/voronoi-bake.nix ${./niri-voronoi-bake.nix} >/dev/null; then
      echo "niri-shader-check: niri-voronoi-bake.nix is stale; regenerate with" >&2
      echo "  python3 gen-voronoi-bake.py > niri-voronoi-bake.nix" >&2
      exit 1
    fi

    # ── seed-scaling isolation (animation bodies only; resize exempt) ────────
    ${lib.concatMapStringsSep "\n" (file:
      lib.concatMapStringsSep "\n" (pat: ''
        if grep -Fq ${lib.escapeShellArg pat} ${file}; then
          echo "niri-shader-check: forbidden seed pattern ${pat} in animation body" >&2
          exit 1
        fi
      '') seedPatterns
    ) seedCheckBodyFiles}

    # ── GLSL compile gate for every validated profile ────────────────────────
    ${lib.concatStringsSep "\n" (lib.concatLists (map (profile:
      map (ev: ''
        $compile_py $compile_script \
          --glslang "$glslang_bin" \
          --version-line "$version_line" \
          --prelude-file "$frag_dir/${ev.key}-prelude.frag" \
          --epilogue-file "$frag_dir/${ev.key}-epilogue.frag" \
          --body-file ${bodyFiles.${profile}.${ev.key}} \
          --label "${profile}/${ev.key}"
      '') eventTypes
    ) profilesToValidate))}

    mkdir -p $out
    cp ${bodyFiles.${shaderProfile}.windowClose} $out/windowClose.glsl
    cp ${bodyFiles.${shaderProfile}.windowOpen} $out/windowOpen.glsl
    cp ${bodyFiles.${shaderProfile}.windowResize} $out/windowResize.glsl
    VERSION_LINE="$version_line" $compile_py -c '
    import json, os, sys
    meta = json.loads(sys.argv[1])
    meta["glslVersionLine"] = os.environ["VERSION_LINE"]
    sys.stdout.write(json.dumps(meta, sort_keys=True, separators=(",", ":")))
    ' ${lib.escapeShellArg (builtins.toJSON {
      profiles = profilesToValidate;
      outputProfile = shaderProfile;
    })} > $out/meta.json
  ''
