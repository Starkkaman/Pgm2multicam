#!/bin/bash
# PGM to Multicam uninstaller for macOS: removes what Install.command installed, nothing else.
set -u

APP_DIR="$HOME/Library/Application Support/pgm2multicam"
ENGINE_PARENT="$APP_DIR/engine"
ENGINE_DIR="$ENGINE_PARENT/pgm2mc"
CONFIG="$APP_DIR/config.json"
SCRIPTS_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SCRIPT="$SCRIPTS_DIR/PGM to Multicam.lua"
OLD_SCRIPT="$SCRIPTS_DIR/PGM to Multicam.py.disabled"

pause() {
    if [ -t 0 ]; then
        echo
        read -r -n 1 -s -p "Press any key to close this window." || true
        echo
    fi
}

echo "PGM to Multicam uninstaller"
echo "==========================="
echo
status=0
for p in "$SCRIPT" "$ENGINE_DIR" "$CONFIG"; do
    if [ -e "$p" ] || [ -L "$p" ]; then
        if rm -rf "$p"; then
            echo "Removed: $p"
        else
            echo "Could not remove: $p"
            status=1
        fi
    fi
done
rm -rf "$ENGINE_PARENT"/.pgm2mc.new.* "$ENGINE_PARENT"/.pgm2mc.old.* 2>/dev/null
rmdir "$ENGINE_PARENT" 2>/dev/null
rmdir "$APP_DIR" 2>/dev/null

echo
if [ -e "$OLD_SCRIPT" ]; then
    echo "Note: the old Python script is still there, disabled: $OLD_SCRIPT"
fi
echo "Output folders next to your media were left in place."
if [ "$status" = 0 ]; then
    echo "DONE: PGM to Multicam was removed. Restart DaVinci Resolve."
else
    echo "FAILED: some files could not be removed (see above)."
fi
pause
exit "$status"
