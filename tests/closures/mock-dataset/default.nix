{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;
  pythonPkgs = python.pkgs;
in
pythonPkgs.buildPythonApplication {
  pname = "mock-dataset";
  version = "0.1.0";
  format = "pyproject";

  src = ./.;

  nativeBuildInputs = [ pythonPkgs.hatchling ];

  propagatedBuildInputs = [
    pythonPkgs.fastapi
    pythonPkgs.uvicorn
  ];

  doCheck = false;

  meta.description = "Mock dataset closure used in Agentix tests";
}
