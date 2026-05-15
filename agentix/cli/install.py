"""`agentix install` — bundle multiple closures into a single image.

Usage:

    agentix install bash files claude-code -o my-agent:0.1.0
    agentix install ./primitives/bash ./my-agent  -o demo:dev
    agentix install bash files --dry-run          # stage to ./build/<tag>/

A bundle is one docker image carrying multiple closures' Python packages
+ native deps under a single `/nix/entry/` tree, with a `bundle.json`
discriminator so the runtime's `_auto_load` discovers every nested
closure on boot.

Spec resolution (per arg):

  1. **Path:** an existing directory containing `pyproject.toml` and an
     `agentix_closures/<name>/` package — used as-is.
  2. **Image ref:** a string with a `:` AND a `/` — treated as a
     pre-built closure image and pulled at bundle build time.
  3. **Short name:** searched against the framework's conventional
     extension roots — `primitives/<name>/`, `agents/<name>/`,
     `datasets/<name>/` (relative to the repo root). If none match,
     falls back to PyPI as `agentix-<kind>-<name>` for each kind.

Image-ref and PyPI paths aren't fully wired yet — they raise
NotImplementedError with a clear message. The local-path / short-name
case works end-to-end today, which covers the in-repo dev flow.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "primitives" / "_template"
GEN_MANIFEST = REPO_ROOT / "tools" / "gen_manifest.py"
EXTENSION_ROOTS = ("primitives", "agents", "datasets")
CLOSURE_KINDS = ("primitive", "agent", "dataset")


@dataclass
class ClosureSpec:
    """One resolved input to the bundle. Exactly one source field is set."""

    short: str               # the short name written into bundle.json
    kind: Literal["path", "pypi", "image"]
    path: Path | None = None
    pypi_dist: str | None = None
    image_ref: str | None = None


def _looks_like_image_ref(spec: str) -> bool:
    """Heuristic: `name/something:tag` is an image ref; `./foo` or bare
    names are not. Conservative — bare `:` without a `/` is ambiguous and
    treated as a name."""
    return "/" in spec and ":" in spec and not spec.startswith((".", "/"))


def _looks_like_path(spec: str) -> bool:
    if spec.startswith((".", "/")):
        return True
    p = Path(spec)
    return p.is_dir() and (p / "pyproject.toml").is_file()


def _find_local(name: str) -> Path | None:
    """Look up `<root>/<name>/` under each extension root in the repo."""
    for root in EXTENSION_ROOTS:
        candidate = REPO_ROOT / root / name
        if candidate.is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def resolve_spec(spec: str) -> ClosureSpec:
    if _looks_like_path(spec):
        p = Path(spec).resolve()
        if not (p / "pyproject.toml").is_file():
            raise SystemExit(f"{spec}: no pyproject.toml — not a closure source dir")
        pyproject = _read_pyproject(p)
        short = _short_from_pyproject(pyproject)
        return ClosureSpec(short=short, kind="path", path=p)
    if _looks_like_image_ref(spec):
        # No actual docker pull / extract today — surface this loudly.
        return ClosureSpec(short=_short_from_image(spec), kind="image", image_ref=spec)
    # Short name: local first, then PyPI.
    local = _find_local(spec)
    if local is not None:
        return ClosureSpec(short=spec, kind="path", path=local)
    # No local match → PyPI. Defer the actual fetch to build time;
    # resolution is best-effort here.
    return ClosureSpec(
        short=spec, kind="pypi", pypi_dist=f"agentix-???-{spec}",
    )


def _read_pyproject(closure_dir: Path) -> dict:
    with (closure_dir / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _short_from_pyproject(pyproject: dict) -> str:
    """`agentix-primitive-bash` → `bash`."""
    name = pyproject.get("project", {}).get("name", "")
    if not isinstance(name, str) or not name.startswith("agentix-"):
        raise SystemExit(f"pyproject.toml: name {name!r} must start with `agentix-`")
    parts = name.split("-")
    if len(parts) < 3:
        raise SystemExit(f"pyproject.toml: name {name!r} expected `agentix-<kind>-<short>`")
    return parts[-1]


def _short_from_image(ref: str) -> str:
    """`docker.io/me/agentix/primitive-bash:0.1.0` → `bash`."""
    # Take last path segment, strip tag, strip leading agentix-<kind>- prefix.
    last = ref.rsplit("/", 1)[-1].rsplit(":", 1)[0]
    for kind in CLOSURE_KINDS:
        pre = f"agentix-{kind}-"
        if last.startswith(pre):
            return last[len(pre):]
    return last  # best effort


def _stage_bundle(
    bundle_name: str,
    bundle_version: str,
    specs: list[ClosureSpec],
    build_dir: Path,
) -> None:
    """Lay out a self-contained docker build context for the bundle.

    Layout:
      build_dir/
      ├── Dockerfile                       # generated, multi-stage build
      ├── gen_manifest.py                  # copied from tools/
      ├── default.nix                      # copied from primitives/_template/
      ├── bundle.json                      # baked into the final image
      └── <short>/                         # per-closure staging
          ├── pyproject.toml
          └── agentix_closures/<name>/
    """
    # Per-closure source staging (path kind only at this stage).
    for spec in specs:
        if spec.kind != "path":
            # Image and PyPI paths require pulling artifacts before staging —
            # not wired yet. The Dockerfile generator below surfaces the same
            # error if we ever reach it.
            raise NotImplementedError(
                f"`agentix install {spec.short}`: {spec.kind} sourcing not "
                f"implemented yet. Use a local path or check that the closure "
                f"lives under primitives/, agents/, or datasets/ in this repo."
            )
        assert spec.path is not None
        sub = build_dir / spec.short
        sub.mkdir()
        shutil.copytree(spec.path / "agentix_closures", sub / "agentix_closures")
        shutil.copy2(spec.path / "pyproject.toml", sub / "pyproject.toml")

    # Shared build infra — same files agentix-build uses for single closures.
    shutil.copy2(TEMPLATE_DIR / "default.nix", build_dir / "default.nix")
    shutil.copy2(GEN_MANIFEST, build_dir / "gen_manifest.py")

    # Bundle.json discriminator — what the runtime keys off to switch
    # `_auto_load` into nested-entry mode.
    (build_dir / "bundle.json").write_text(json.dumps({
        "abi": 1,
        "kind": "bundle",
        "name": bundle_name,
        "version": bundle_version,
        "closures": [s.short for s in specs],
    }, indent=2) + "\n")

    # The bundle Dockerfile builds each closure's nix derivation in a
    # builder stage, then copies the resulting `entry/` trees side by side
    # under `/nix/entry/<short>/`. Same shared `default.nix` template is
    # reused for each closure — pname/version are read from each closure's
    # pyproject.toml at build time.
    (build_dir / "Dockerfile").write_text(_render_dockerfile(specs))


def _render_dockerfile(specs: list[ClosureSpec]) -> str:
    builder_steps = "\n".join(
        f"WORKDIR /src/{spec.short}\n"
        f"COPY {spec.short}/ ./\n"
        f"COPY default.nix gen_manifest.py ./\n"
        f"RUN nix-build --no-out-link default.nix -o ./result && \\\n"
        f"    STORE_PATH=$(readlink -f ./result) && \\\n"
        f"    for p in $(nix-store -qR \"$STORE_PATH\"); do \\\n"
        f"        cp -a \"$p\" /export/nix/store/; \\\n"
        f"    done && \\\n"
        f"    mkdir -p /export/nix/entry/{spec.short} && \\\n"
        f"    cp -aL \"$STORE_PATH\"/* /export/nix/entry/{spec.short}/"
        for spec in specs
    )
    return f"""\
# Generated by `agentix install`. Do not hand-edit.
ARG NIX_IMAGE=nixos/nix:latest

FROM ${{NIX_IMAGE}} AS builder
RUN mkdir -p ~/.config/nix && \\
    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \\
    nix-channel --update 2>/dev/null || true
RUN mkdir -p /export/nix/store /export/nix/entry

{builder_steps}

FROM busybox:stable
COPY --from=builder /export /
COPY bundle.json /nix/entry/bundle.json
VOLUME /nix
LABEL org.agentix.closure=1
LABEL org.agentix.closure.kind=bundle
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentix install",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("specs", nargs="+",
                        help="closure short names, paths, or image refs")
    parser.add_argument("-o", "--output", required=True,
                        help="bundle image tag, e.g. my-agent:0.1.0")
    parser.add_argument("--dry-run", action="store_true",
                        help="stage to ./build/<bundle>/ and print path; do NOT invoke docker")
    args = parser.parse_args(argv)

    if ":" not in args.output:
        raise SystemExit(f"--output must include a tag (got {args.output!r})")
    bundle_name, bundle_version = args.output.rsplit(":", 1)

    specs = [resolve_spec(s) for s in args.specs]
    # Validate uniqueness on `short` — same closure twice would collide
    # both in the build dir and at runtime registration.
    shorts = [s.short for s in specs]
    dupes = {n for n in shorts if shorts.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate closure short names: {sorted(dupes)}")

    if args.dry_run:
        out = REPO_ROOT / "build" / bundle_name.rsplit("/", 1)[-1]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        _stage_bundle(bundle_name, bundle_version, specs, out)
        print(f"staged bundle build context → {out}")
        print(f"would build → {args.output}")
        print(f"  closures: {', '.join(shorts)}")
        return 0

    with TemporaryDirectory(prefix="agentix-bundle-") as tmp:
        build_dir = Path(tmp)
        _stage_bundle(bundle_name, bundle_version, specs, build_dir)
        print(f"building {args.output}…", file=sys.stderr)
        proc = subprocess.run(
            ["docker", "build", "-t", args.output, str(build_dir)],
            check=False,
        )
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
