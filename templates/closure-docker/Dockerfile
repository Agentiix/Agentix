# Example closure Dockerfile.
#
# Agentix doesn't mandate any particular Dockerfile — only that the final
# image satisfies the closure convention:
#
#   * VOLUME /nix
#   * /nix/store/<hash>-*/     — Nix store deps
#   * /nix/entry/bin/start     — executable entry point (no CLI args)
#
# The `start` binary is invoked with no arguments; the runtime sets
# AGENTIX_SOCKET (path to the Unix socket to bind) in its environment.
#
# Build: docker build -t my-closure:1.0 -f <path>/Dockerfile ./my-closure

ARG NIX_IMAGE=nixos/nix:latest

FROM ${NIX_IMAGE} AS builder
RUN mkdir -p ~/.config/nix && \
    echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf && \
    echo 'substituters = https://mirror.sjtu.edu.cn/nix-channels/store https://cache.nixos.org' >> ~/.config/nix/nix.conf && \
    echo 'https://mirror.sjtu.edu.cn/nix-channels/nixpkgs-unstable nixpkgs' > ~/.nix-channels && \
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
