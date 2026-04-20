# Universal closure Dockerfile

Package any closure — in any language — as a Docker image that Agentix can mount into a sandbox. Nix runs inside the builder stage, so the host only needs Docker.

## Author's directory

```
my-closure/
├── default.nix         # Nix derivation — must emit bin/start
└── <source files>      # whatever your derivation builds from
```

`default.nix` is the only required input. Build from anywhere:

```bash
docker build \
    -t my-closure:1.0 \
    -f /path/to/templates/closure-docker/Dockerfile \
    ./my-closure
```

## What the image contains

| Path | Content | Purpose |
|---|---|---|
| `/nix/store/<hash>-*/` | closure's full transitive store closure | content-addressed; unique per closure |
| `/nix/entry` | symlink to the derivation's output | exposes `bin/start` at a stable path |
| `VOLUME /nix` | declared volume | triggers Docker's volume-init-from-image rule |

At sandbox create time, Agentix's deployment runs `docker run --rm -v <named-volume>:/nix <image> true` — Docker auto-populates the named volume from the image's `/nix` layer on first attach, then the sandbox mounts that volume at `/mnt/<ns>:ro`. Because every closure's store paths are content-addressed (hash-prefixed), merging multiple closures into a shared `/nix/store` symlink forest never collides.

## Using it from SandboxConfig

```python
SandboxConfig(
    image="ubuntu:24.04",
    runtime="agentix/runtime:0.1.0",
    closures={"mine": "my-closure:1.0"},
)
```

## Minimal `default.nix` (Python closure)

```nix
{ pkgs ? import <nixpkgs> {} }:
let python = pkgs.python312; in
python.pkgs.buildPythonApplication {
  pname = "my-closure";
  version = "0.1.0";
  format = "pyproject";
  src = ./.;
  nativeBuildInputs = [ python.pkgs.hatchling ];
  propagatedBuildInputs = [ python.pkgs.fastapi python.pkgs.uvicorn ];
  doCheck = false;
}
```

Pair it with a `pyproject.toml` that declares `[project.scripts] start = "<pkg>.__main__:main"` and a package with `__main__.py` that binds uvicorn on the Unix socket at `$AGENTIX_SOCKET`. See `tests/closures/mock-agent/` in this repo for a working reference.

## Non-Python closures

`default.nix` can build anything — Go, Rust, a shell script. The only contract is that the derivation's output contains a `bin/start` that implements the closure ABI: no CLI args, reads `AGENTIX_SOCKET` from env, binds an HTTP server on that Unix socket. See `docs/closure-protocol.md`.
