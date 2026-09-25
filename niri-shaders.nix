# Thin assembler — delegates to niri-shader-lib + niri-shader-anims.
#
# Arguments:
#   pkgs          — nixpkgs instance
#   niriPkg       — niri package (used by readNiriShaderCompileInfo; pass null to skip)
#   shaderProfile — "full" | "simple" | "unhook-only" | "shredder-only" | "voronoi-only"
#
# Returns { windowClose, windowOpen, windowResize, durations } — GLSL body strings
# and compositor duration-ms values (max per event) for niri.nix.

{ pkgs ? import <nixpkgs> {}, niriPkg ? null, shaderProfile ? "full" }:
let
  lib2  = import ./niri-shader-lib.nix { inherit pkgs; lib = pkgs.lib; };
  anims = import ./niri-shader-anims.nix;

  profile = anims.${shaderProfile};

  anyNeedsVoronoiBake = animList: pkgs.lib.any (a: a.needsVoronoiBake or false) animList;
  closeNeedsBake = anyNeedsVoronoiBake profile.close;
  openNeedsBake  = anyNeedsVoronoiBake profile.open;

  # Committed bake, not voronoiBakeDrv: importing a build output is
  # import-from-derivation. niri-shader-check fails if this file drifts from
  # gen-voronoi-bake.py.
  voronoiBakeStr =
    if closeNeedsBake || openNeedsBake
    then (import ./niri-voronoi-bake.nix).glslConstants
    else null;

in lib2.assembleProfile {
  inherit profile;
  inherit voronoiBakeStr;
}
