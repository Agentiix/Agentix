#!/usr/bin/env bash
set -euo pipefail

# Build Claude Code agent closure.
#
# Usage:
#   ./build.sh                                    # build default version (hash required in default.nix)
#   ./build.sh --version 2.2.0                    # build new version (auto-discovers hash)
#   ./build.sh --version 2.1.96 --export          # build + export tarball

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=""
HASH=""
EXPORT=false
OUT_DIR="${SCRIPT_DIR}/out"

while [[ $# -gt 0 ]]; do
  case $1 in
    --version) VERSION="$2"; shift 2 ;;
    --hash)    HASH="$2"; shift 2 ;;
    --export)  EXPORT=true; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

NIX_ARGS=""
[[ -n "$VERSION" ]] && NIX_ARGS="$NIX_ARGS --argstr version $VERSION"
[[ -n "$HASH" ]]    && NIX_ARGS="$NIX_ARGS --argstr hash $HASH"

echo "=== Building claude-code ${VERSION:-default} ==="

# Build — if hash is empty/wrong, nix fails with correct hash
BUILD_OUTPUT=$(nix-build "$SCRIPT_DIR/default.nix" $NIX_ARGS --no-out-link 2>&1) && {
  STORE_PATH=$(echo "$BUILD_OUTPUT" | tail -1)
} || {
  CORRECT_HASH=$(echo "$BUILD_OUTPUT" | grep "got:" | awk '{print $2}')
  if [[ -n "$CORRECT_HASH" ]]; then
    echo "Discovered hash: $CORRECT_HASH"
    echo "Rebuilding..."
    STORE_PATH=$(nix-build "$SCRIPT_DIR/default.nix" $NIX_ARGS --argstr hash "$CORRECT_HASH" --no-out-link 2>&1 | tail -1)
  else
    echo "$BUILD_OUTPUT" >&2
    exit 1
  fi
}

echo "Store path: $STORE_PATH"
echo "Size: $(du -sh "$STORE_PATH" | cut -f1)"
"$STORE_PATH/bin/claude" --version

if $EXPORT; then
  mkdir -p "$OUT_DIR"
  CLOSURE_PATHS=$(nix-store -qR "$STORE_PATH")
  LABEL="${VERSION:-default}"
  TARBALL="$OUT_DIR/claude-code-${LABEL}.tar.gz"

  echo ""
  echo "=== Exporting closure ==="
  tar czf "$TARBALL" --hard-dereference -C / $(echo "$CLOSURE_PATHS" | sed 's|^/||')
  echo "Tarball: $TARBALL ($(du -sh "$TARBALL" | cut -f1))"

  cat > "$OUT_DIR/claude-code-${LABEL}.json" <<EOF
{"agent":"claude-code","version":"${LABEL}","store_path":"$STORE_PATH","tarball":"$TARBALL"}
EOF
fi

echo "Done."
