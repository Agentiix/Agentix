# Runtime image — Python slim + framework venv + uv.
#
# `agentix build` auto-builds this image (from the repo root) when it's
# missing locally. You don't normally run `docker build` directly.
#
# The runtime image carries:
#   /nix/runtime/          — framework venv (uv-managed); ENTRYPOINT runs from here
#   /nix/.wheels/          — the framework wheel, stashed for bundle stages to reuse
#   uv on $PATH            — used by `agentix build`'s generated Dockerfile to
#                            create one venv per namespace at /nix/<short>/
#
# Namespace bundles extend this image: each namespace gets `/nix/<short>/`
# with its own uv venv + (optional) Nix sys-deps symlinked into bin/.
# The multiplexer spawns one worker subprocess per namespace using that
# namespace's interpreter and prepends `/nix/<short>/bin` to PATH so
# `subprocess.run("git", ...)` in user code resolves transparently.

FROM python:3.11-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY agentix ./agentix
RUN python -m build --wheel --outdir /dist

FROM python:3.11-slim
RUN pip install --no-cache-dir uv

# Framework wheel — kept so each bundled namespace's venv can install it
# without reaching PyPI.
RUN mkdir -p /nix/.wheels
COPY --from=builder /dist/*.whl /nix/.wheels/

# Runtime's own venv. /nix/runtime owns the framework dispatcher;
# the multiplexer is what spawns per-namespace workers.
RUN uv venv /nix/runtime && \
    /nix/runtime/bin/pip install --no-cache-dir /nix/.wheels/agentix-*.whl

EXPOSE 8000
ENV AGENTIX_BIND_PORT=8000
ENTRYPOINT ["/nix/runtime/bin/agentix-server"]
