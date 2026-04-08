{ pkgs ? import <nixpkgs> {}
, version ? "2.1.96"
, hash ? ""
}:

let
  nodejs = pkgs.nodejs_22;

  claude-code-modules = pkgs.stdenv.mkDerivation {
    pname = "claude-code-modules";
    inherit version;
    dontUnpack = true;
    nativeBuildInputs = [ nodejs pkgs.cacert ];
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
    outputHash = hash;
    buildPhase = ''
      export HOME=$TMPDIR
      export npm_config_cache=$TMPDIR/npm-cache
      export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
      mkdir -p $out
      npm install -g @anthropic-ai/claude-code@${version} --prefix=$out
    '';
    installPhase = "true";
  };

in
pkgs.stdenv.mkDerivation {
  pname = "claude-code-runtime";
  inherit version;
  dontUnpack = true;
  installPhase = ''
    mkdir -p $out/bin $out/lib

    cp -r ${claude-code-modules}/lib/node_modules $out/lib/

    cat > $out/bin/claude <<WRAPPER
    #!/bin/sh
    exec ${nodejs}/bin/node $out/lib/node_modules/@anthropic-ai/claude-code/cli.js "\$@"
    WRAPPER
    chmod +x $out/bin/claude

    ln -s ${nodejs}/bin/node $out/bin/node
    ln -s ${nodejs}/bin/npm $out/bin/npm

    # Include runner.py in closure
    cp ${./runner.py} $out/runner.py
  '';

  meta.description = "Claude Code agent runtime";
}
