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
  compileInfo = shaderLib.readNiriShaderCompileInfo patchedNiriPkg;

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

  eventTypes = [
    { key = "windowClose"; prelude = compileInfo.preludes.close; epilogue = compileInfo.epilogues.close; }
    { key = "windowOpen"; prelude = compileInfo.preludes.open; epilogue = compileInfo.epilogues.open; }
    { key = "windowResize"; prelude = compileInfo.preludes.resize; epilogue = compileInfo.epilogues.resize; }
  ];

  preludeFiles = lib.genAttrs (map (e: e.key) eventTypes) (key:
    let ev = lib.findFirst (e: e.key == key) (lib.throw "unknown event ${key}") eventTypes;
    in pkgs.writeText "niri-${key}-prelude.frag" ev.prelude
  );

  epilogueFiles = lib.genAttrs (map (e: e.key) eventTypes) (key:
    let ev = lib.findFirst (e: e.key == key) (lib.throw "unknown event ${key}") eventTypes;
    in pkgs.writeText "niri-${key}-epilogue.frag" ev.epilogue
  );

  bodyFiles = lib.genAttrs profilesToValidate (profile:
    lib.genAttrs [ "windowClose" "windowOpen" "windowResize" ] (key:
      pkgs.writeText "niri-${profile}-${key}.frag" shaderSets.${profile}.${key}
    )
  );

  metaJson = pkgs.writeText "niri-shader-meta.json" (builtins.toJSON {
    glslVersionLine = compileInfo.glslVersionLine;
    profiles = profilesToValidate;
    outputProfile = shaderProfile;
  });

in
pkgs.runCommand "niri-shader-check"
  {
    nativeBuildInputs = [ pkgs.glslang pkgs.python3 ];
    passthru = {
      inherit compileInfo shaderSets;
    };
  }
  ''
    set -euo pipefail

    compile_py=${pkgs.python3}/bin/python3
    compile_script=${./niri-shader-glsl-compile.py}
    glslang_bin=${pkgs.glslang}/bin/glslangValidator
    version_line=${lib.escapeShellArg compileInfo.glslVersionLine}

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
          --prelude-file ${preludeFiles.${ev.key}} \
          --epilogue-file ${epilogueFiles.${ev.key}} \
          --body-file ${bodyFiles.${profile}.${ev.key}} \
          --label "${profile}/${ev.key}"
      '') eventTypes
    ) profilesToValidate))}

    mkdir -p $out
    cp ${bodyFiles.${shaderProfile}.windowClose} $out/windowClose.glsl
    cp ${bodyFiles.${shaderProfile}.windowOpen} $out/windowOpen.glsl
    cp ${bodyFiles.${shaderProfile}.windowResize} $out/windowResize.glsl
    cp ${metaJson} $out/meta.json
  ''
