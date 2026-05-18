# Library function: produce a stream-layered docker image for an Agentix
# bundle, given a workspace root (pyproject.toml + uv.lock) and an optional
# list of plugin default.nix files contributing system binaries.
#
# Plugin default.nix files MUST be functions of the form `{ pkgs }: drv` —
# they receive the same nixpkgs the bundle builder is using, so all
# derivations share one Nixpkgs revision (no per-plugin pin drift).
#
# The Python side comes from uv2nix reading the workspace's uv.lock.
# Plugins that need only Python deps don't ship a default.nix at all —
# `pip install <plugin>` (already done in the user's pyproject) puts them
# in the lock, and uv2nix picks them up automatically.

{ pyproject-nix, uv2nix, pyproject-build-systems }:

{
  pkgs,
  name,
  tag ? "latest",
  workspaceRoot,
  pluginNixFiles ? [ ],
  pythonVersion ? "311",
  entryPoint ? "agentix-server",
}:

let
  inherit (pkgs) lib;
  python = pkgs."python${pythonVersion}";

  # Load workspace from pyproject.toml + uv.lock at workspaceRoot.
  workspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };

  # Convert pyproject.toml + uv.lock into a Nix overlay over the python set.
  # `sourcePreference = "wheel"` is faster — uses upstream wheels where
  # possible instead of building from sdist.
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

  pythonSet =
    (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope
      (lib.composeManyExtensions [
        pyproject-build-systems.overlays.default
        overlay
      ]);

  # Virtual env containing user project + all transitive deps.
  pythonEnv = pythonSet.mkVirtualEnv "${name}-env" workspace.deps.default;

  # System deps contributed by plugin default.nix files.
  pluginSysDrvs = map (f: import f { inherit pkgs; }) pluginNixFiles;

  # One joined tree exposing /bin/* from python env and every plugin's bins.
  joined = pkgs.symlinkJoin {
    name = "${name}-rootfs";
    paths = [ pythonEnv ] ++ pluginSysDrvs;
  };
in
pkgs.dockerTools.streamLayeredImage {
  inherit name tag;

  # `contents` pulls `joined`'s closure into the image's /nix/store. The
  # default behaviour also surfaces /bin etc. at the image root from
  # `joined`; that's harmless for standalone `docker run` and is ignored
  # by the deployment paths (both `--mount type=image,subpath=nix` and
  # `--volumes-from` consume only `/nix`).
  contents = [ joined ];

  # Stable entry path inside the image's /nix tree. Deployments mount
  # /nix into the task container (subpath=nix or volumes-from); the same
  # absolute path resolves in both cases.
  extraCommands = ''
    mkdir -p nix/runtime/bin
    for f in ${joined}/bin/*; do
      ln -s "$(readlink -f "$f")" nix/runtime/bin/$(basename "$f")
    done
  '';

  config = {
    Entrypoint = [ "/nix/runtime/bin/${entryPoint}" ];
    Env = [
      "PATH=/nix/runtime/bin"
      # Agentix server reads this; deployments override per sandbox.
      "AGENTIX_BIND_PORT=8000"
    ];
    ExposedPorts = { "8000/tcp" = { }; };
    # Declared so `--volumes-from <carrier>:ro` exposes /nix to the task
    # container when `--mount type=image,subpath=nix` is unavailable.
    Volumes = { "/nix" = { }; };
  };
}
