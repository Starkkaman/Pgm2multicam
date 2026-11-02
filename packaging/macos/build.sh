#!/usr/bin/env bash
# Build the self-contained macOS pgm2mc engine bundle (PyInstaller onedir) with ffmpeg/ffprobe included.
#
#   bash packaging/macos/build.sh            # rerunnable; downloads are cached and checksum-verified
#
# Output (the Resolve Lua script and the installer depend on this layout exactly):
#   dist/pgm2mc/pgm2mc          executable (runs engine/pgm2mc/__main__.py via packaging/macos/pgm2mc_launcher.py)
#   dist/pgm2mc/_internal/...   PyInstaller payload (Python 3.12, numpy, ...)
#   dist/pgm2mc/bin/ffmpeg
#   dist/pgm2mc/bin/ffprobe
#
# ARCHITECTURE: arm64 (Apple Silicon) only. python-build-standalone ships no universal2 CPython and numpy ships
# no universal2 wheels, so an x86_64 bundle would be a separate build on an Intel toolchain; not done here.
# pip picks numpy's macosx_14_0_arm64 wheel (Accelerate BLAS) on macOS 14+, so the bundle needs macOS 14 or newer.
#
# Nothing is installed system-wide: the interpreter, venv, pip cache and downloads live under build/macos/.
# Requires: curl, shasum, ditto, codesign, tar (all part of macOS).
#
# Signing: every Mach-O in the PyInstaller payload and the pgm2mc executable are ad-hoc signed ("-").
# bin/ffmpeg and bin/ffprobe keep their publisher signature (Developer ID Application: Martin Riedl
# (KU3N25YGLU), notarized) because that passes Gatekeeper for quarantined copies; set ADHOC_FFMPEG=1 to
# replace it with an ad-hoc signature like the rest of the bundle.
#
# Gatekeeper: an ad-hoc signature is not notarized. A quarantined copy (downloaded with a browser, then
# unzipped) is blocked when pgm2mc is first run, even from the CLI: exec stalls in dyld waiting for Gatekeeper.
# Whatever installs the bundle must strip the attribute BEFORE the first launch:
#     xattr -dr com.apple.quarantine /path/to/pgm2mc
# (once a launch has been blocked, stripping afterwards did not unblock that copy on macOS 27.2).
set -euo pipefail

# --- pinned inputs -------------------------------------------------------------------------------------------
# CPython: python-build-standalone release 20261013, CPython 3.12.15, aarch64-apple-darwin, install_only.
# sha256 checked against the release's SHA256SUMS file and the GitHub asset digest.
PY_VERSION="3.12.15"
PBS_RELEASE="20261013"
PY_ARCHIVE="cpython-${PY_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PY_ARCHIVE//+/%2B}"
PY_SHA256="ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440"

# Python packages (exact pins)
NUMPY_VERSION="2.5.4"
PYINSTALLER_VERSION="6.22.3"
PYINSTALLER_HOOKS_VERSION="2026.8"

# ffmpeg / ffprobe: Martin Riedl's FFmpeg build server, release build 9.0.2, macOS arm64, static.
# sha256 matches the publisher's ffmpeg.zip.sha256 / ffprobe.zip.sha256 next to each download; the binaries
# inside are signed "Developer ID Application: Martin Riedl (KU3N25YGLU)" and notarized (checked below).
FFMPEG_VERSION="9.0.2"
FFMPEG_BASE="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
FFMPEG_ZIP_SHA256="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"
FFPROBE_ZIP_SHA256="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"
FFMPEG_TEAM_ID="KU3N25YGLU"

# --- paths ---------------------------------------------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$REPO/build/macos"
CACHE="$WORK/cache"
VENV="$WORK/venv"
DIST="$REPO/dist"
APP="$DIST/pgm2mc"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "build.sh: needs macOS on Apple Silicon (arm64); this bundle is arm64-only." >&2
    exit 1
fi

log() { printf '==> %s\n' "$*"; }

# fetch URL DEST SHA256: download into the cache once, always verify the checksum
fetch() {
    local url="$1" dest="$2" sha="$3"
    if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ]; then
        return 0
    fi
    rm -f "$dest" "$dest.part"
    log "download $url"
    curl -fsSL --retry 3 --proto '=https' -o "$dest.part" "$url"
    local got
    got="$(shasum -a 256 "$dest.part" | cut -d' ' -f1)"
    if [ "$got" != "$sha" ]; then
        echo "checksum mismatch for $url: expected $sha, got $got" >&2
        rm -f "$dest.part"
        exit 1
    fi
    mv "$dest.part" "$dest"
}

mkdir -p "$CACHE"

# --- 1. standalone CPython -----------------------------------------------------------------------------------
fetch "$PY_URL" "$CACHE/$PY_ARCHIVE" "$PY_SHA256"
PY_HOME="$CACHE/cpython-${PY_VERSION}+${PBS_RELEASE}"
if [ ! -x "$PY_HOME/bin/python3" ]; then
    log "unpack CPython $PY_VERSION"
    rm -rf "$PY_HOME" "$PY_HOME.tmp"
    mkdir -p "$PY_HOME.tmp"
    tar -xzf "$CACHE/$PY_ARCHIVE" -C "$PY_HOME.tmp"
    mv "$PY_HOME.tmp/python" "$PY_HOME"
    rm -rf "$PY_HOME.tmp"
fi
"$PY_HOME/bin/python3" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

# --- 2. throwaway build venv ---------------------------------------------------------------------------------
log "create build venv"
rm -rf "$VENV"
"$PY_HOME/bin/python3" -m venv "$VENV"
export PIP_CACHE_DIR="$CACHE/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1
"$VENV/bin/python" -m pip install --quiet --only-binary=:all: \
    "numpy==$NUMPY_VERSION" "pyinstaller==$PYINSTALLER_VERSION" \
    "pyinstaller-hooks-contrib==$PYINSTALLER_HOOKS_VERSION"
"$VENV/bin/python" -c 'import numpy, PyInstaller; print("numpy", numpy.__version__, "| PyInstaller", PyInstaller.__version__)'

# --- 3. PyInstaller onedir -----------------------------------------------------------------------------------
log "PyInstaller onedir build"
rm -rf "$APP" "$WORK/pyinstaller"
PGM2MC_TARGET_ARCH=arm64 "$VENV/bin/python" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath "$DIST" --workpath "$WORK/pyinstaller" "$HERE/pgm2mc.spec"
[ -x "$APP/pgm2mc" ] && [ -d "$APP/_internal" ] || { echo "PyInstaller output missing" >&2; exit 1; }

# --- 4. ffmpeg / ffprobe -------------------------------------------------------------------------------------
fetch "$FFMPEG_BASE/ffmpeg.zip" "$CACHE/ffmpeg-$FFMPEG_VERSION-arm64.zip" "$FFMPEG_ZIP_SHA256"
fetch "$FFMPEG_BASE/ffprobe.zip" "$CACHE/ffprobe-$FFMPEG_VERSION-arm64.zip" "$FFPROBE_ZIP_SHA256"
mkdir -p "$APP/bin"
for tool in ffmpeg ffprobe; do
    tmp="$WORK/unzip-$tool"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    ditto -x -k "$CACHE/$tool-$FFMPEG_VERSION-arm64.zip" "$tmp"
    [ -f "$tmp/$tool" ] || { echo "$tool not found in its zip" >&2; exit 1; }
    install -m 0755 "$tmp/$tool" "$APP/bin/$tool"
    rm -rf "$tmp"
    xattr -c "$APP/bin/$tool"
    # the publisher signature must be intact and from the expected team
    codesign --verify --strict "$APP/bin/$tool"
    signer="$(codesign -dv "$APP/bin/$tool" 2>&1)"
    case "$signer" in
        *"TeamIdentifier=$FFMPEG_TEAM_ID"*) ;;
        *) echo "$tool: unexpected signer" >&2; echo "$signer" >&2; exit 1 ;;
    esac
    version="$("$APP/bin/$tool" -hide_banner -version)"
    echo "${version%%$'\n'*}"
done

# --- 5. ad-hoc signing ---------------------------------------------------------------------------------------
log "ad-hoc sign"
xattr -cr "$APP"
is_macho() { case "$(file -b "$1")" in Mach-O*) return 0 ;; *) return 1 ;; esac; }
# inside-out: every payload Mach-O first ...
while IFS= read -r -d '' f; do
    if is_macho "$f"; then
        codesign -s - --force --timestamp=none "$f"
    fi
done < <(find "$APP/_internal" -type f -print0)
if [ "${ADHOC_FFMPEG:-0}" = "1" ]; then
    codesign -s - --force --timestamp=none "$APP/bin/ffmpeg" "$APP/bin/ffprobe"
fi
# ... then the folder itself: codesign treats dist/pgm2mc as a shallow bundle whose main executable is
# pgm2mc, signs it and seals every other file in _CodeSignature/CodeResources. --deep does not descend into
# bin/ (not a standard nested-code location), so ffmpeg/ffprobe keep their own signature and are sealed as-is.
codesign -s - --force --deep --timestamp=none "$APP"

log "verify signatures"
# each nested Mach-O on its own (the main executable is verified as the folder's code, below)
while IFS= read -r -d '' f; do
    if [ "$f" != "$APP/pgm2mc" ] && is_macho "$f"; then
        codesign --verify --strict "$f"
    fi
done < <(find "$APP" -type f -print0)
codesign --verify --deep --strict --verbose=2 "$APP"

# --- 6. smoke test -------------------------------------------------------------------------------------------
log "smoke test"
"$APP/pgm2mc" --help >/dev/null
echo "bundle: $APP ($(du -sh "$APP" | cut -f1))"
file "$APP/pgm2mc" "$APP/bin/ffmpeg" "$APP/bin/ffprobe"
