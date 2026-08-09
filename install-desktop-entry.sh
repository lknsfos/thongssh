#!/usr/bin/env bash
# Installs a .desktop launcher for THIS checkout, so Linux docks/app menus
# (Plank, GNOME Shell, etc.) show ThongSSH's icon and let you launch it
# without a terminal. Running `python3 run.py`/`thongssh.py` directly from
# a shell has no .desktop association at all — docks read an app's icon
# from a .desktop file's Icon= key, never from the running window itself,
# so without one installed there's simply nothing for the dock to show.
#
# Safe to re-run any time (e.g. after moving the checkout) — it always
# regenerates the file from the template with this checkout's real path,
# no hand-editing needed either way.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$APP_DIR/thongssh.desktop.template"
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
TARGET="$TARGET_DIR/com.example.thongssh.desktop"

if [ ! -f "$TEMPLATE" ]; then
    echo "Template not found at $TEMPLATE" >&2
    exit 1
fi

mkdir -p "$TARGET_DIR"
sed "s|__APP_DIR__|$APP_DIR|g" "$TEMPLATE" > "$TARGET"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$TARGET_DIR" 2>/dev/null || true
fi

echo "Installed: $TARGET"
echo "Launches via: python3 $APP_DIR/thongssh.py"
