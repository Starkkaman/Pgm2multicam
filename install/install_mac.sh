#!/usr/bin/env bash
# pgm2multicam installer for macOS. Idempotent: re-run to update.
#   install/install_mac.sh [--skip-ffmpeg-check] [--uninstall]
# Env: PGM2MC_SKIP_PIP=1 skips "pip install numpy" (offline testing only).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/pgm2multicam"
VENV_DIR="$APP_DIR/venv"
ENGINE_DIR="$APP_DIR/engine"
SCRIPT_NAME="PGM to Multicam.py"
SCRIPTS_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"

SKIP_FFMPEG=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --skip-ffmpeg-check) SKIP_FFMPEG=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            sed -n '2,4p' "$0"
            exit 0 ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2 ;;
    esac
done

if [ "$UNINSTALL" = 1 ]; then
    rm -f "$SCRIPTS_DIR/$SCRIPT_NAME"
    rm -rf "$APP_DIR"
    echo "pgm2multicam removed (Resolve script and $APP_DIR)."
    echo "Output folders next to your media were left in place."
    exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Note: this installer targets macOS; continuing anyway." >&2
fi

# --- python3 >= 3.8
PY=""
for cand in python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.8 or newer is required." >&2
    echo "Install it from https://www.python.org/downloads/macos/ (recommended: Resolve also uses it)" >&2
    echo "or run: brew install python" >&2
    exit 1
fi
echo "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# --- ffmpeg / ffprobe (Resolve's GUI PATH lacks Homebrew, so record absolute paths)
find_tool() {
    local name="$1" p
    p="$(command -v "$name" 2>/dev/null || true)"
    if [ -n "$p" ]; then
        echo "$p"
        return 0
    fi
    for d in /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$d/$name" ]; then
            echo "$d/$name"
            return 0
        fi
    done
    return 1
}
FFMPEG="$(find_tool ffmpeg || true)"
FFPROBE="$(find_tool ffprobe || true)"
if [ -z "$FFMPEG" ] || [ -z "$FFPROBE" ]; then
    if [ "$SKIP_FFMPEG" = 1 ]; then
        echo "Warning: ffmpeg/ffprobe not found; continuing (--skip-ffmpeg-check)." >&2
    else
        echo "ffmpeg and ffprobe are required. Install them with:" >&2
        echo "    brew install ffmpeg" >&2
        echo "(Homebrew: https://brew.sh). Then run this installer again." >&2
        exit 1
    fi
else
    echo "ffmpeg: $FFMPEG"
    echo "ffprobe: $FFPROBE"
fi

# --- venv + numpy
mkdir -p "$APP_DIR"
if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "Creating virtual environment in $VENV_DIR"
    "$PY" -m venv --clear "$VENV_DIR"
fi
if [ "${PGM2MC_SKIP_PIP:-0}" = 1 ]; then
    echo "Skipping pip install (PGM2MC_SKIP_PIP=1)."
else
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade numpy
fi

# --- engine
if [ ! -d "$REPO_DIR/engine/pgm2mc" ]; then
    echo "Engine package not found at $REPO_DIR/engine/pgm2mc" >&2
    exit 1
fi
rm -rf "$ENGINE_DIR/pgm2mc"
mkdir -p "$ENGINE_DIR"
cp -R "$REPO_DIR/engine/pgm2mc" "$ENGINE_DIR/"
find "$ENGINE_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

# --- config.json (values passed via env to avoid quoting problems)
PGM2MC_CFG_PY="$VENV_DIR/bin/python" PGM2MC_CFG_ENGINE="$ENGINE_DIR" \
PGM2MC_CFG_FFMPEG="$FFMPEG" PGM2MC_CFG_FFPROBE="$FFPROBE" \
"$PY" - "$APP_DIR/config.json" <<'EOF'
import json, os, sys
cfg = {"python": os.environ["PGM2MC_CFG_PY"], "engine_dir": os.environ["PGM2MC_CFG_ENGINE"],
       "ffmpeg": os.environ["PGM2MC_CFG_FFMPEG"] or None, "ffprobe": os.environ["PGM2MC_CFG_FFPROBE"] or None}
with open(sys.argv[1], "w") as fh:
    json.dump(cfg, fh, indent=2)
EOF
echo "Config: $APP_DIR/config.json"

# --- smoke check: the engine imports in the venv
if [ "${PGM2MC_SKIP_PIP:-0}" != 1 ]; then
    if ! PYTHONPATH="$ENGINE_DIR" "$VENV_DIR/bin/python" -c "import numpy, pgm2mc" 2>/dev/null; then
        echo "Warning: 'import pgm2mc' failed in the venv; check the output above." >&2
    fi
fi

# --- Resolve script
mkdir -p "$SCRIPTS_DIR"
cp "$REPO_DIR/resolve/$SCRIPT_NAME" "$SCRIPTS_DIR/$SCRIPT_NAME"
echo "Resolve script: $SCRIPTS_DIR/$SCRIPT_NAME"

cat <<EOF

pgm2multicam is installed.
Next steps:
  1. Restart DaVinci Resolve (it scans the Scripts folder at startup).
  2. In the Media Pool select the program clip and the ISO clips.
  3. Workspace > Scripts > PGM to Multicam.
CLI: PYTHONPATH="$ENGINE_DIR" "$VENV_DIR/bin/python" -m pgm2mc PGM.mov CAM1.mov CAM2.mov -o OUTDIR
Uninstall: $0 --uninstall
EOF
