#!/bin/bash
# PGM to Multicam installer for macOS (double-click in Finder). Re-run to update.
# Installs the engine to ~/Library/Application Support/pgm2multicam/engine/pgm2mc and the Resolve script to
# ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/pgm2multicam"
ENGINE_PARENT="$APP_DIR/engine"
ENGINE_DIR="$ENGINE_PARENT/pgm2mc"
SCRIPTS_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SCRIPT_NAME="PGM to Multicam.lua"
OLD_SCRIPT="PGM to Multicam.py"
SRC_ENGINE="$HERE/engine/pgm2mc"
SRC_SCRIPT="$HERE/$SCRIPT_NAME"

pause() {
    if [ -t 0 ]; then
        echo
        read -r -n 1 -s -p "Press any key to close this window." || true
        echo
    fi
}

fail() {
    echo
    echo "INSTALL FAILED: $*"
    pause
    exit 1
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

unquarantine() {
    if command -v xattr >/dev/null 2>&1; then
        xattr -dr com.apple.quarantine "$1" 2>/dev/null || true
    fi
}

copy_tree() {
    if command -v ditto >/dev/null 2>&1; then
        ditto "$1" "$2"
    else
        cp -RPp "$1" "$2"
    fi
}

echo "PGM to Multicam installer"
echo "========================="
echo

[ -x "$SRC_ENGINE/pgm2mc" ] || fail "engine not found at $SRC_ENGINE/pgm2mc. Run Install.command from the unzipped \"PGM to Multicam\" folder."
[ -f "$SRC_SCRIPT" ] || fail "$SCRIPT_NAME not found next to Install.command."

# 1. engine: copy to a temporary name, check that it runs, then swap it in
mkdir -p "$ENGINE_PARENT" || fail "cannot create $ENGINE_PARENT."
NEW_DIR="$ENGINE_PARENT/.pgm2mc.new.$$"
OLD_DIR="$ENGINE_PARENT/.pgm2mc.old.$$"
rm -rf "$NEW_DIR"
echo "Copying the engine..."
copy_tree "$SRC_ENGINE" "$NEW_DIR" || { rm -rf "$NEW_DIR"; fail "could not copy the engine to $ENGINE_PARENT."; }
unquarantine "$NEW_DIR"
chmod +x "$NEW_DIR/pgm2mc" "$NEW_DIR/bin/ffmpeg" "$NEW_DIR/bin/ffprobe" 2>/dev/null || true

echo "Checking that the engine runs (the first start can take a few seconds)..."
if ! CHECK_OUT="$("$NEW_DIR/pgm2mc" --help 2>&1)"; then
    rm -rf "$NEW_DIR"
    echo "$CHECK_OUT" | tail -n 5
    fail "the engine did not start. This download may be for another kind of Mac (this Mac: $(uname -m)). Your previous installation, if any, was left unchanged."
fi
for tool in ffmpeg ffprobe; do
    [ -x "$NEW_DIR/bin/$tool" ] || echo "Warning: $tool is missing from the engine folder."
done

if [ -e "$ENGINE_DIR" ]; then
    mv "$ENGINE_DIR" "$OLD_DIR" || { rm -rf "$NEW_DIR"; fail "could not replace the old engine at $ENGINE_DIR."; }
fi
if ! mv "$NEW_DIR" "$ENGINE_DIR"; then
    [ -e "$OLD_DIR" ] && mv "$OLD_DIR" "$ENGINE_DIR"
    rm -rf "$NEW_DIR"
    fail "could not move the engine into place at $ENGINE_DIR."
fi
rm -rf "$OLD_DIR"
echo "Engine installed: $ENGINE_DIR"

# 2. config.json tells the Resolve script where the engine is
CONFIG="$APP_DIR/config.json"
if ! { printf '{"engine": "%s"}\n' "$(json_escape "$ENGINE_DIR")" > "$CONFIG.tmp.$$" \
        && mv -f "$CONFIG.tmp.$$" "$CONFIG"; }; then
    rm -f "$CONFIG.tmp.$$"
    fail "could not write $CONFIG."
fi

# 3. Resolve script
mkdir -p "$SCRIPTS_DIR" || fail "cannot create $SCRIPTS_DIR."
cp -f "$SRC_SCRIPT" "$SCRIPTS_DIR/$SCRIPT_NAME" || fail "could not copy the script to $SCRIPTS_DIR."
unquarantine "$SCRIPTS_DIR/$SCRIPT_NAME"
echo "Resolve script installed: $SCRIPTS_DIR/$SCRIPT_NAME"

if [ -e "$SCRIPTS_DIR/$OLD_SCRIPT" ]; then
    if mv -f "$SCRIPTS_DIR/$OLD_SCRIPT" "$SCRIPTS_DIR/$OLD_SCRIPT.disabled"; then
        echo "The old Python version of the script was renamed to \"$OLD_SCRIPT.disabled\" so the menu shows"
        echo "only one entry. Delete it if you no longer need it."
    else
        echo "Warning: could not rename the old \"$OLD_SCRIPT\"; Resolve may list the script twice."
    fi
fi

echo
echo "SUCCESS: PGM to Multicam is installed."
echo "Restart DaVinci Resolve, then use Workspace > Scripts > PGM to Multicam."
pause
exit 0
