# bash primitive closure image.
#
# Final layout (Agentix closure convention):
#   * VOLUME /nix
#   * /nix/store/<hash>-*/        — content-addressed Nix deps (Python + the package wheel)
#   * /nix/entry/python/agentix_closures/bash/  — package the runtime imports
#   * /nix/entry/manifest.json    — ClosureManifest, abi=1, package=agentix_closures.bash
#
# Build (context = this directory):
#   docker build -t agentix/primitive-bash:0.1.0 primitives/bash/

ARG NIX_IMAGE=nixos/nix:latest

FROM ${NIX_IMAGE} AS builder
RUN mkdir -p ~/.config/nix && \
    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \
    nix-channel --update 2>/dev/null || true
WORKDIR /src
COPY . .
RUN nix-build --no-out-link default.nix -o ./result && \
    STORE_PATH=$(readlink -f ./result) && \
    mkdir -p /export/nix/store && \
    for p in $(nix-store -qR "$STORE_PATH"); do cp -a "$p" /export/nix/store/; done && \
    ln -s "$STORE_PATH" /export/nix/entry

FROM busybox:stable
COPY --from=builder /export /
VOLUME /nix
LABEL org.agentix.closure=1
LABEL org.agentix.closure.kind=primitive
LABEL org.agentix.closure.name=bash
