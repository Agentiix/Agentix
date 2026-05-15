{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;
  pythonPkgs = python.pkgs;
in
pythonPkgs.buildPythonApplication {
  pname = "agentix-primitive-files";
  version = "0.1.0";
  format = "pyproject";

  src = ./.;

  nativeBuildInputs = [ pythonPkgs.hatchling ];
  propagatedBuildInputs = [];
  doCheck = false;

  postInstall = ''
    cp ${./manifest.json} $out/manifest.json
  '';

  meta.description = "Sandbox file I/O primitive — upload / download";
}
