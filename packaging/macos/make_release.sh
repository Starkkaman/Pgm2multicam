#!/usr/bin/env bash
# Assemble the macOS release zip from a built engine bundle.
#   packaging/macos/make_release.sh DIST_PGM2MC_DIR [VERSION] [OUT_DIR]
# DIST_PGM2MC_DIR: the onedir bundle (pgm2mc/pgm2mc, pgm2mc/_internal, pgm2mc/bin/ffmpeg, pgm2mc/bin/ffprobe).
# VERSION defaults to `git describe`; OUT_DIR defaults to <repo>/dist.
# Output: OUT_DIR/PGM-to-Multicam-macos-<arch>-<version>.zip containing the folder "PGM to Multicam".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

die() {
    echo "make_release: $*" >&2
    exit 1
}

[ $# -ge 1 ] || die "usage: $0 DIST_PGM2MC_DIR [VERSION] [OUT_DIR]"
BUNDLE="$(cd "$1" && pwd)" || die "no such folder: $1"
VERSION="${2:-}"
if [ -z "$VERSION" ]; then
    VERSION="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo dev)"
fi
VERSION="${VERSION//[^A-Za-z0-9._-]/_}"
OUT_DIR="${3:-$ROOT/dist}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

EXE="$BUNDLE/pgm2mc"
[ -f "$EXE" ] && [ -x "$EXE" ] || die "$EXE is missing or not executable"
for tool in ffmpeg ffprobe; do
    [ -x "$BUNDLE/bin/$tool" ] || die "$BUNDLE/bin/$tool is missing or not executable"
done
for f in Install.command Uninstall.command README.txt; do
    [ -f "$HERE/$f" ] || die "missing $HERE/$f"
done
LUA_SCRIPT="$ROOT/resolve/PGM to Multicam.lua"
[ -f "$LUA_SCRIPT" ] || die "missing $LUA_SCRIPT"

detect_arch() {
    local archs="" desc=""
    if command -v lipo >/dev/null 2>&1; then
        archs="$(lipo -archs "$EXE" 2>/dev/null || true)"
    fi
    if [ -z "$archs" ] && command -v file >/dev/null 2>&1; then
        desc="$(file -b "$EXE" 2>/dev/null || true)"
        case "$desc" in *arm64*|*aarch64*) archs="arm64" ;; esac
        case "$desc" in *x86_64*|*x86-64*) archs="${archs:+$archs }x86_64" ;; esac
    fi
    case "$archs" in
        *arm64*x86_64*|*x86_64*arm64*) echo "universal" ;;
        *arm64*) echo "arm64" ;;
        *x86_64*) echo "x86_64" ;;
        *) uname -m ;;
    esac
}
ARCH="$(detect_arch)"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pgm2mc-release.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/PGM to Multicam"
mkdir -p "$DEST/engine"

cp "$HERE/Install.command" "$HERE/Uninstall.command" "$HERE/README.txt" "$DEST/"
cp "$LUA_SCRIPT" "$DEST/PGM to Multicam.lua"
chmod 755 "$DEST/Install.command" "$DEST/Uninstall.command"
chmod 644 "$DEST/README.txt" "$DEST/PGM to Multicam.lua"
if command -v ditto >/dev/null 2>&1; then
    ditto "$BUNDLE" "$DEST/engine/pgm2mc"
else
    cp -RPp "$BUNDLE" "$DEST/engine/pgm2mc"
fi

ZIP="$OUT_DIR/PGM-to-Multicam-macos-$ARCH-$VERSION.zip"
rm -f "$ZIP"
if [ "$(uname -s)" = "Darwin" ] && command -v ditto >/dev/null 2>&1; then
    (cd "$STAGE" && ditto -c -k --keepParent "PGM to Multicam" "$ZIP")
elif command -v zip >/dev/null 2>&1; then
    (cd "$STAGE" && zip -qry "$ZIP" "PGM to Multicam")
else
    die "neither ditto nor zip is available"
fi
echo "Release: $ZIP (arch $ARCH, version $VERSION)"
