# Assembly library for niri custom shaders.
#
# Usage:
#   lib2 = import ./niri-shader-lib.nix { inherit pkgs; lib = pkgs.lib; };
#
# Provides:
#   validateAnimProbs  — assert sum of probs ≈ 1.0, all prob > 0, unique fn names
#   validateAnimDurations — require durationMs > 0 on every animation tuple
#   profileTotalDurationMs — max(durationMs) for a profile event list
#   mkSeedRouter       — generate the GLSL entry-point function that routes by seed
#   assembleShader     — concatenate preamble + bake constants + anim bodies + router
#   voronoiBakeDrv     — runCommand derivation that runs gen-voronoi-bake.py
#   readNiriShaderCompileInfo — extract #version and prelude/epilogue from niri source

{ pkgs, lib }:

let
  # ── fillDefaultProbs ───────────────────────────────────────────────────────
  # Assign equal share of remaining probability to entries without explicit prob.
  # Operates on the final composed list (after profile ++).
  fillDefaultProbs = anims:
    let
      specifiedProbs = lib.filter (p: p != null) (map (a: a.prob or null) anims);
      S              = lib.foldr (a: b: a + b) 0.0 specifiedProbs;
      unspecified    = lib.filter (a: !(a ? prob)) anims;
      nUnspecified   = lib.length unspecified;
      remaining      = 1.0 - S;
      defaultProb    = remaining / (1.0 * nUnspecified);
    in
      assert lib.assertMsg (S <= 1.0001)
        "niri-shader-lib: explicit animation probabilities sum to > 1.0 (got: ${toString S})";
      assert lib.assertMsg (remaining <= 0.0001 || nUnspecified > 0)
        "niri-shader-lib: probabilities sum to < 1.0 with no unspecified entries to fill (sum=${toString S})";
      map (a: if a ? prob then a else a // { prob = defaultProb; }) anims;

  # ── validateAnimDurations ──────────────────────────────────────────────────
  # Every animation tuple must declare durationMs (wall-clock length in ms).
  validateAnimDurations = anims:
    map (anim:
      assert lib.assertMsg (anim ? durationMs)
        "niri-shader-lib: animation ${anim.fn or "?"} missing required durationMs";
      assert lib.assertMsg (anim.durationMs > 0)
        "niri-shader-lib: animation ${anim.fn} durationMs must be > 0 (got: ${toString (anim.durationMs or 0)})";
      anim
    ) anims;

  # Compositor duration for an event = longest animation in the profile list.
  profileTotalDurationMs = anims:
    let
      durs = map (a: a.durationMs) (validateAnimDurations anims);
    in
      if durs == [ ] then 0
      else lib.foldl' (acc: d: if d > acc then d else acc) (lib.head durs) (lib.tail durs);

  # Map global niri progress [0,1] over totalDurationMs into per-animation progress.
  animProgressExpr = { totalDurationMs, animDurationMs }:
    let scale = totalDurationMs / (animDurationMs * 1.0);
    in "min(1.0, niri_clamped_progress * ${toString scale})";

  # ── validateAnimProbs ──────────────────────────────────────────────────────
  # Nix assertion that the probability list is valid:
  #   • all prob > 0
  #   • sum of probs ≈ 1.0 (within 1e-4 tolerance for float rounding)
  #   • fn names are unique
  validateAnimProbs = anims:
    let
      withDurs = validateAnimDurations anims;
      filled  = fillDefaultProbs withDurs;
      probs   = map (a: a.prob) filled;
      fns     = map (a: a.fn)   filled;
      total   = lib.foldr (a: b: a + b) 0.0 probs;
      allPos  = lib.all (p: p > 0.0) probs;
      sumOk   = (total >= 0.9999) && (total <= 1.0001);
      uniqFns = (lib.length fns) == (lib.length (lib.unique fns));
    in
      assert lib.assertMsg allPos
        "niri-shader-lib: all animation probabilities must be > 0 (got: ${toString probs})";
      assert lib.assertMsg sumOk
        "niri-shader-lib: animation probabilities must sum to 1.0 ± 1e-4 (got: ${toString total})";
      assert lib.assertMsg uniqFns
        "niri-shader-lib: animation fn names must be unique (got: ${toString fns})";
      filled;

  # ── mkSeedRouter ──────────────────────────────────────────────────────────
  # Generate a GLSL entry-point with three-phase conditional routing:
  #
  #   Phase 1 — per-tuple includeWhen eligibility (absent → always eligible)
  #   Phase 2 — forceWhen scan over full list in index order (first match wins)
  #   Phase 3 — seed routing among eligible tuples only (runtime prob renorm)
  #
  # Seed routing: total = sum of nominal prob for eligible tuples;
  #   s = niri_random_seed * total; walk cumulative bands;
  #   local_seed = (s - bandStart) / bandWidth.
  # Fallback when total == 0: dispatch first tuple in the list.
  #
  # routerArity:
  #   "seeded" — close/open: (coords, size, scaled_progress, local_seed)
  #   "plain"  — resize: (coords, size) only
  mkSeedRouter = { entryName, anims, routerArity ? "seeded", totalDurationMs ? null }:
    let
      filled = validateAnimProbs anims;
      totalMs =
        if totalDurationMs != null then totalDurationMs
        else profileTotalDurationMs filled;
      n = lib.length filled;
      animProgress = anim:
        animProgressExpr {
          totalDurationMs = totalMs;
          animDurationMs = anim.durationMs;
        };
      seededCall = anim: localSeedExpr:
        "${anim.fn}(coords_geo, size_geo, ${animProgress anim}, ${localSeedExpr})";
      plainCall = anim:
        "${anim.fn}(coords_geo, size_geo)";
      callAnim = anim: localSeedExpr:
        if routerArity == "plain" then plainCall anim
        else seededCall anim localSeedExpr;

      # Resize preludes omit niri_random_seed; derive a stable route seed from layout.
      routeSeedExpr =
        if routerArity == "plain" then
          "fract(niri_window_pos.x * 12.9898 + niri_window_pos.y * 78.233)"
        else
          "niri_random_seed";

      # Phase 1: per-tuple eligibility predicates
      eligibilityDecls = lib.concatMapStringsSep "\n" (idx:
        let
          anim = lib.elemAt filled idx;
          pred = anim.includeWhen or null;
        in
          if pred != null then
            "    bool eligible_${toString idx} = (${pred});"
          else
            "    bool eligible_${toString idx} = true;"
      ) (lib.genList (i: i) n);

      # Phase 2: forceWhen — full list, index order, first match wins
      forceBlocks = lib.concatMapStrings (anim:
        let pred = anim.forceWhen or null;
        in if pred != null then ''
            if (${pred}) {
                return ${callAnim anim routeSeedExpr};
            }
        '' else ""
      ) filled;

      # True when no later tuple can be eligible (runtime last-eligible check).
      isLastEligibleExpr = idx:
        let
          later = lib.genList (j: idx + 1 + j) (n - idx - 1);
          laterEligible = map (j: "eligible_${toString j}") later;
        in
          if laterEligible == [ ] then "true"
          else "!(${lib.concatStringsSep " || " laterEligible})";

      # Phase 3: dynamic weight accumulation
      weightAccum = lib.concatMapStrings (idx:
        let anim = lib.elemAt filled idx;
        in ''
            if (eligible_${toString idx}) total += ${toString anim.prob};
        ''
      ) (lib.genList (i: i) n);

      # Phase 3: cumulative dispatch among eligible tuples
      dispatchBands = lib.concatMapStrings (idx:
        let
          anim = lib.elemAt filled idx;
          lastCheck = isLastEligibleExpr idx;
        in ''
            if (eligible_${toString idx}) {
                float bandWidth_${toString idx} = ${toString anim.prob};
                if (${lastCheck} || seed < bandStart + bandWidth_${toString idx}) {
                    float local_seed = (seed - bandStart) / bandWidth_${toString idx};
                    return ${callAnim anim "local_seed"};
                }
                bandStart += bandWidth_${toString idx};
            }
        ''
      ) (lib.genList (i: i) n);

      firstAnim = lib.head filled;
      fallbackReturn = "    return ${callAnim firstAnim routeSeedExpr};";
    in ''
        vec4 ${entryName}(vec3 coords_geo, vec3 size_geo) {
        ${eligibilityDecls}
        ${forceBlocks}
            float total = 0.0;
        ${weightAccum}
            float seed = ${routeSeedExpr} * total;
            if (total == 0.0) {
        ${fallbackReturn}
            }
            float bandStart = 0.0;
        ${dispatchBands}}
    '';

  # ── assembleShader ─────────────────────────────────────────────────────────
  # Concatenate the parts of a complete niri shader body string:
  #   1. voronoiBakeConstants (if provided — only when needsVoronoiBake)
  #   2. sharedPreamble (fast_sincos, voronoi runtime helpers, etc.)
  #   3. Each animation body (full function definitions)
  #   4. mkSeedRouter output (the entry-point vec4 close_color / open_color)
  #
  # The caller decides which voronoiBakeConstants to pass (static .nix file or drv).
  # niri itself prepends #version and the prelude, and appends the epilogue.
  assembleShader = { sharedPreamble, voronoiBakeConstants ? null, anims, entryName, routerArity ? "seeded" }:
    let
      totalDurationMs = profileTotalDurationMs anims;
      bakeSection  = if voronoiBakeConstants != null then voronoiBakeConstants else "";
      bodies       = lib.concatMapStrings (a: a.body) anims;
      router       = mkSeedRouter { inherit entryName anims routerArity totalDurationMs; };
    in
      bakeSection + sharedPreamble + bodies + router;

  # ── assembleProfile ────────────────────────────────────────────────────────
  # Assemble all three event shaders from a profile attrset.
  assembleProfile = { profile, voronoiBakeStr ? null }:
    let
      anyNeedsVoronoiBake = animList: lib.any (a: a.needsVoronoiBake or false) animList;
      closeNeedsBake = anyNeedsVoronoiBake profile.close;
      openNeedsBake  = anyNeedsVoronoiBake profile.open;
      bake = voronoiBakeStr;
    in {
      windowClose = assembleShader {
        sharedPreamble       = profile.closeSharedPreamble;
        voronoiBakeConstants = if closeNeedsBake then bake else null;
        anims                = profile.close;
        entryName            = "close_color";
      };
      windowOpen = assembleShader {
        sharedPreamble       = profile.openSharedPreamble;
        voronoiBakeConstants = if openNeedsBake then bake else null;
        anims                = profile.open;
        entryName            = "open_color";
      };
      windowResize = assembleShader {
        sharedPreamble = profile.resizeSharedPreamble;
        anims          = profile.resize;
        entryName      = "resize_color";
        routerArity    = "plain";
      };
      durations = {
        windowClose  = profileTotalDurationMs profile.close;
        windowOpen   = profileTotalDurationMs profile.open;
        windowResize = profileTotalDurationMs profile.resize;
      };
    };

  # ── voronoiBakeDrv ─────────────────────────────────────────────────────────
  # Nix derivation that runs gen-voronoi-bake.py and outputs voronoi-bake.nix.
  # The output is a .nix file with the same interface as home/niri-voronoi-bake.nix:
  #   { numVariants = ...; numCells = ...; variantVs = [...]; glslConstants = ''...''; }
  #
  # Usage in assembler:
  #   bakeNix = import "${voronoiBakeDrv}/voronoi-bake.nix";
  #   voronoiBakeStr = bakeNix.glslConstants;
  voronoiBakeDrv = pkgs.runCommand "niri-voronoi-bake" {
    nativeBuildInputs = [ pkgs.python3 ];
  } ''
    mkdir -p $out
    ${pkgs.python3}/bin/python3 ${./gen-voronoi-bake.py} > $out/voronoi-bake.nix
  '';

  # ── readNiriShaderCompileInfo ──────────────────────────────────────────────
  # Extract GLSL compile-time info from a niri package's source tree.
  #
  # Returns:
  #   {
  #     glslVersionLine  — e.g. "#version 300 es"
  #     preludes         — { close, open, resize } — GLSL prelude strings
  #     epilogues        — { close, open, resize } — GLSL epilogue strings
  #   }
  #
  # After the niri-glsl-es300.patch, shader_element.rs contains:
  #   format!("#version 300 es\n{src}")
  # We search for the version string with a simple scan rather than regex since
  # Nix builtins.match has limited multiline support.
  readNiriShaderCompileInfo = niriPkg:
    let
      src        = niriPkg.src;
      shaderDir  = "${src}/src/render_helpers/shaders";
      elemRs     = builtins.readFile "${src}/src/render_helpers/shader_element.rs";
      # Simple approach: check for the es300 version string first, fall back to 100.
      glslVersionLine =
        if lib.hasInfix "#version 300 es" elemRs then "#version 300 es"
        else if lib.hasInfix "#version 100" elemRs then "#version 100"
        else "#version 100";  # conservative fallback
    in {
      inherit glslVersionLine;
      preludes = {
        close  = builtins.readFile "${shaderDir}/close_prelude.frag";
        open   = builtins.readFile "${shaderDir}/open_prelude.frag";
        resize = builtins.readFile "${shaderDir}/resize_prelude.frag";
      };
      epilogues = {
        close  = builtins.readFile "${shaderDir}/close_epilogue.frag"
               + builtins.readFile "${shaderDir}/rounding_alpha.frag";
        open   = builtins.readFile "${shaderDir}/open_epilogue.frag"
               + builtins.readFile "${shaderDir}/rounding_alpha.frag";
        resize = builtins.readFile "${shaderDir}/resize_epilogue.frag"
               + builtins.readFile "${shaderDir}/rounding_alpha.frag";
      };
    };

in {
  inherit fillDefaultProbs validateAnimProbs validateAnimDurations profileTotalDurationMs
    animProgressExpr mkSeedRouter assembleShader assembleProfile voronoiBakeDrv
    readNiriShaderCompileInfo;
}
