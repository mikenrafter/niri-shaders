{
  description = "niri-shaders — Nix/Python generator + GLSL validator for niri custom-shader window animations";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
  let
    system = "x86_64-linux";
    lib = nixpkgs.lib;
    pkgs = import nixpkgs { inherit system; };

    mkShaders = import ./niri-shaders.nix;
    mkShaderCheck = import ./niri-shader-check.nix;

    shaderCheck = pkgs.callPackage mkShaderCheck {
      niriPkg = pkgs.niri;
      shaderProfiles = [
        "full"
        "simple"
        "unhook-only"
        "shredder-only"
        "voronoi-only"
      ];
      shaderProfile = "full";
    };
  in
  {
    # Plain functions — same call signature as the files themselves, so
    # consumers do `inputs.niri-shaders.lib.mkShaders { inherit pkgs niriPkg shaderProfile; }`.
    lib = {
      inherit mkShaders mkShaderCheck;
      shaderLib = pkgs: import ./niri-shader-lib.nix { inherit pkgs; lib = pkgs.lib; };
      anims = import ./niri-shader-anims.nix;
    };

    homeManagerModules.default = import ./home-module.nix;

    checks.${system}.default = shaderCheck;

    devShells.${system}.default = pkgs.mkShell {
      packages = with pkgs; [
        python3
        glslang
        glslviewer
      ];
    };
  };
}
