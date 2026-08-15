#!/bin/bash
#
# Script to fully build ThongSSH for macOS into a .dmg package.
#
# Two modes, auto-detected:
#   - Standalone (this script lying around on its own, outside a checkout):
#     clones the repo from git into a scratch dir, builds there, and copies
#     the final .dmg into the directory you ran it from — exactly as before.
#   - In-repo (this script sitting in its usual place, <repo>/build-packages/):
#     no git clone — builds from the checkout it's part of, and writes the
#     final .dmg into a dist/ directory (matching build-appimage.sh /
#     build-deb-rpm.sh's OUTDIR convention) instead of the working directory.
#
# Usage:
# 1. Save this script as build-macos.sh
# 2. Make it executable: chmod +x build-macos.sh
# 3. Run it: ./build-macos.sh
#
# Arguments:
#   - No arguments: builds for the machine's native architecture.
#   - arm64: force build for Apple Silicon (arm64).
#   - x86_64: force build for Intel (x86_64).
#
# Environment:
#   REPO_URL — git repo to clone in standalone mode
#              (default: https://github.com/lknsfos/thongssh.git)
#   SRC      — use an existing source tree instead of cloning or auto-detecting
#   OUTDIR   — where to put the built .dmg in in-repo mode (default: $PWD/dist)
#

set -e # Abort on any error

echo "🚀 Starting ThongSSH build for macOS..."

# --- Configuration ---
APP_NAME="ThongSSH"
ORIGINAL_PWD=$(pwd)

# --- Standalone vs in-repo detection (mirrors build-deb-rpm.sh) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN_REPO=0
if [ -n "${SRC:-}" ]; then
    SRC_ROOT="$(cd "$SRC" && pwd)"
    IN_REPO=1
    echo "📂 Using existing source tree: $SRC_ROOT"
elif [ -f "$SCRIPT_DIR/../thongssh_gtk/constants.py" ]; then
    SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    IN_REPO=1
    echo "📂 Running inside repo checkout: $SRC_ROOT"
else
    REPO_URL="${REPO_URL:-https://github.com/lknsfos/thongssh.git}"
fi
OUTDIR="${OUTDIR:-$PWD/dist}"

BUILD_DIR=$(mktemp -d -t thongssh-build-XXXXXX)

# Detach any DMG volume left mounted by a previous failed run and remove the
# temp build directory, on success OR failure. Without this, a failed build
# leaves its temp mount attached, so the next attempt's volume gets suffixed
# "Name 2", "Name 3"... by Finder, and its clone/venv/dist/ is never reclaimed.
cleanup() {
    local exit_code=$?
    for vol in "/Volumes/${APP_NAME}"*; do
        [ -d "$vol" ] && hdiutil detach "$vol" -force >/dev/null 2>&1
    done
    if [ "$exit_code" -eq 0 ]; then
        rm -rf "$BUILD_DIR"
    else
        echo "🩹 Build failed — leaving $BUILD_DIR in place for inspection (delete it manually when done)."
    fi
}
trap cleanup EXIT

# --- Dependency check ---
if [ "$IN_REPO" -eq 0 ]; then
    command -v git >/dev/null 2>&1 || { echo >&2 "🛑 Git not found. Please install the Xcode Command Line Tools."; exit 1; }
fi
command -v brew >/dev/null 2>&1 || { echo >&2 "🛑 Homebrew not found. Please install it from brew.sh."; exit 1; }

# --- Architecture detection ---
ARCH=$(uname -m)
TARGET_ARCH=${1:-$ARCH} # Use the first argument, or fall back to the native architecture

if [ "$TARGET_ARCH" = "arm64" ]; then
    echo "🔩 Target architecture: Apple Silicon (arm64)"
    ARCH_PREFIX=""
    BREW_PREFIX="/opt/homebrew"
elif [ "$TARGET_ARCH" = "x86_64" ]; then
    echo "🔩 Target architecture: Intel (x86_64)"
    if [ "$ARCH" = "arm64" ]; then
        echo "🔥 Build will run under Rosetta 2 emulation."
        ARCH_PREFIX="arch -x86_64"
    fi
    BREW_PREFIX="/usr/local"
else
    echo "🛑 Unsupported target architecture: $TARGET_ARCH. Use 'arm64' or 'x86_64'."
    exit 1
fi

export PATH="$BREW_PREFIX/bin:$PATH"

echo "📦 Temporary build directory: $BUILD_DIR"
cd "$BUILD_DIR"

# 1. Get the sources: clone (standalone) or copy the local checkout (in-repo)
if [ "$IN_REPO" -eq 1 ]; then
    echo "🚚 Copying local source tree ($SRC_ROOT)..."
    mkdir -p thongssh
    rsync -a --exclude='.git' --exclude='venv' --exclude='.venv' \
        --exclude='dist' --exclude='build' --exclude='__pycache__' --exclude='*.pyc' \
        "$SRC_ROOT/" thongssh/
else
    echo "🚚 Cloning repository from $REPO_URL..."
    git clone "$REPO_URL"
fi
cd thongssh

# APP_ID (e.g. for the app bundle identifier), read the same way build-deb-rpm.sh
# reads it — straight out of constants.py, so it can never drift from the real
# app id (which is also what StartupWMClass/dock matching relies on).
APP_ID="$(sed -nE 's/^APP_ID[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/p' thongssh_gtk/constants.py | head -1)"
[ -n "$APP_ID" ] || { echo >&2 "🛑 Could not read APP_ID from thongssh_gtk/constants.py"; exit 1; }

# 2. Install dependencies
echo "🍺 Installing system dependencies via Homebrew..."
$ARCH_PREFIX brew install gtk4 libadwaita vte3 gobject-introspection pygobject3 pkg-config sshpass create-dmg

echo "🐍 Setting up Python environment..."
$ARCH_PREFIX python3 -m venv --system-site-packages venv
source venv/bin/activate

echo "📦 Installing Python packages..."
${ARCH_PREFIX} pip install --upgrade pip
${ARCH_PREFIX} pip install -r requirements.txt
${ARCH_PREFIX} pip install py2app pyobjc-framework-cocoa pycairo

echo "🔎 Verifying paramiko is installed (required for SFTP)..."
${ARCH_PREFIX} python -c "import paramiko" || { echo >&2 "🛑 paramiko is missing from the venv. Add it to requirements.txt."; exit 1; }

# 3. Prepare resources
echo "🎨 Creating .icns icon..."
ICON_SOURCE_PATH="thongssh_gtk/icons/thongssh.png"
ICON_DEST_PATH="thongssh_gtk/icons/thongssh.icns"
if [ ! -f "$ICON_SOURCE_PATH" ]; then
    echo "🛑 Icon not found for build: $ICON_SOURCE_PATH"
    exit 1
fi
mkdir -p thongssh.iconset
$ARCH_PREFIX sips -z 16 16     "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_16x16.png
$ARCH_PREFIX sips -z 32 32     "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_32x32.png
$ARCH_PREFIX sips -z 64 64     "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_64x64.png
$ARCH_PREFIX sips -z 128 128   "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_128x128.png
$ARCH_PREFIX sips -z 256 256   "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_256x256.png
$ARCH_PREFIX sips -z 512 512   "$ICON_SOURCE_PATH" --out thongssh.iconset/icon_512x512.png
$ARCH_PREFIX cp thongssh.iconset/icon_256x256.png thongssh.iconset/icon_256x256@2x.png
$ARCH_PREFIX cp thongssh.iconset/icon_512x512.png thongssh.iconset/icon_512x512@2x.png
$ARCH_PREFIX iconutil -c icns thongssh.iconset -o "$ICON_DEST_PATH"

# 4. Create the launcher script and setup.py
echo "📝 Creating launcher and setup.py..."

cat > mac_launcher.py << 'EOF'
import os
import sys

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
frameworks_dir = os.path.join(base_dir, 'Frameworks')
resources_dir = os.path.join(base_dir, 'Resources')

os.environ['DYLD_LIBRARY_PATH'] = frameworks_dir
os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = frameworks_dir
os.environ['GI_TYPELIB_PATH'] = os.path.join(resources_dir, 'lib', 'girepository-1.0')

if os.environ.get('THONGSSH_RESTARTED') != '1':
    os.environ['THONGSSH_RESTARTED'] = '1'
    os.execv(sys.executable, [sys.executable] + sys.argv)

import ctypes
libs_to_load = [
    'libglib-2.0.0.dylib', 'libgobject-2.0.0.dylib', 'libgio-2.0.0.dylib',
    'libgmodule-2.0.0.dylib', 'libgtk-4.1.dylib', 'libgtk-4.dylib',
    'libadwaita-1.0.dylib', 'libadwaita-1.dylib', 'libvte-2.91-gtk4.dylib'
]

for lib in libs_to_load:
    p = os.path.join(frameworks_dir, lib)
    if os.path.exists(p):
        try:
            ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
        except Exception:
            pass

try:
    import keyring
    import keyring.backends.macOS
    keyring.set_keyring(keyring.backends.macOS.Keyring())
except Exception as e:
    print(f"Warning: Failed to set macOS keyring backend: {e}")

import thongssh
EOF

cat > setup.py << EOF
from setuptools import setup
import os
import re
import glob

import paramiko
import keyring
import keyring.backends.macOS
import cryptography

def get_version():
    """Reads the version from thongssh_gtk/constants.py without importing it."""
    with open('thongssh_gtk/constants.py', 'r') as f:
        content = f.read()
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]', content, re.M)
    if match:
        return match.group(1)
    raise RuntimeError("Could not find the __version__ string in constants.py")

APP_NAME = "ThongSSH"
APP_SCRIPT = "mac_launcher.py"
VERSION = get_version()

def find_data_files(source_dir, target_dir, patterns):
    files = []
    for dirpath, _, filenames in os.walk(source_dir):
        for filename in filenames:
            if any(filename.endswith(p) for p in patterns):
                relative_dir = os.path.relpath(dirpath, source_dir)
                dest_path = os.path.join(target_dir, relative_dir)
                files.append((dest_path, [os.path.join(dirpath, filename)]))
    return files

data_files = find_data_files('thongssh_gtk/icons', 'share/icons/hicolor', ['.png', '.svg'])
data_files.append(('.', ['thongssh_gtk/thongssh.gresource', 'thongssh.py']))

brew_lib_dir = "$BREW_PREFIX/lib"
dylib_files = glob.glob(f"{brew_lib_dir}/libglib-2.0*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libgobject-2.0*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libgio-2.0*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libgmodule-2.0*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libgtk-4*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libadwaita-1*.dylib") + \
              glob.glob(f"{brew_lib_dir}/libvte-2.91-gtk4*.dylib")

OPTIONS = {
    'argv_emulation': True,
    'iconfile': 'thongssh_gtk/icons/thongssh.icns',
    # paramiko, cryptography, bcrypt and nacl ship compiled C/Rust extensions;
    # 'packages' copies their full directory tree (binaries included), whereas
    # 'includes' relies on static import-graph tracing that misses those
    # dynamically-loaded native modules and silently drops them from the bundle.
    'packages': ['gi', 'AppKit', 'cairo', 'thongssh_gtk', 'keyring', 'paramiko', 'cryptography', 'bcrypt', 'nacl'],
    'includes': [
        'cffi', '_cffi_backend', 'pkg_resources', 'importlib_metadata',
        'keyring.backends', 'keyring.backends.macOS',
        'gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.Vte',
        'gi.repository.GdkPixbuf', 'gi.repository.Gio', 'gi.repository.Pango',
        'gi.repository.cairo', 'gi.repository.Atk', 'gi.repository.GLib', 'gi.repository.GObject'
    ],
    'frameworks': dylib_files,
    'plist': {
        'CFBundleName': APP_NAME,
        'CFBundleDisplayName': APP_NAME,
        'CFBundleVersion': VERSION,
        'CFBundleIdentifier': '$APP_ID',
    },
}

setup(
    name=APP_NAME,
    app=[APP_SCRIPT],
    data_files=data_files,
    options={'py2app': OPTIONS},
    setup_requires=['py2app', 'pyobjc-framework-cocoa'],
)
EOF

# 5. Build the .app
echo "🛠️ Building ThongSSH.app with py2app..."
$ARCH_PREFIX python setup.py py2app

# 6. Package into a .dmg
echo "🎁 Packaging into a .dmg image..."
APP_PATH="dist/${APP_NAME}.app"
VERSION=$($ARCH_PREFIX python -c "import re; f=open('thongssh_gtk/constants.py','r'); c=f.read(); m=re.search(r'__version__\s*=\s*[\"\']([^\"\']*)[\"\']', c); print(m.group(1))")
DMG_NAME="${APP_NAME}-${VERSION}-${TARGET_ARCH}.dmg"
# Must live outside dist/: create-dmg packages the *source* folder ("dist/")
# as-is, and if the output file is written inside it, the growing temp image
# becomes part of its own source content mid-copy, so it never fits any size
# — this is what actually caused the "No space left on device" failures.
FINAL_DMG="${DMG_NAME}"

# Fixed DMG window/icon layout — kept as variables so the background artwork
# (see thongssh_gtk/icons/dmg-background.png) can be designed to match exactly.
DMG_WINDOW_W=660
DMG_WINDOW_H=400
DMG_ICON_SIZE=128
DMG_APP_ICON_X=180
DMG_APP_ICON_Y=190
DMG_APPLICATIONS_X=480
DMG_APPLICATIONS_Y=190
# The repo only ships one @2x source PNG (2x the window size, e.g. 1320x800
# for a 660x400 window); the @1x version and the Retina-aware multi-resolution
# TIFF are both generated here at build time.
DMG_BACKGROUND_SRC="thongssh_gtk/icons/dmg-background.png"
DMG_BACKGROUND_1X="thongssh_gtk/icons/dmg-background-1x.png"
DMG_BACKGROUND_TIFF="thongssh_gtk/icons/dmg-background.tiff"

BACKGROUND_ARGS=()
if [ -f "$DMG_BACKGROUND_SRC" ]; then
    echo "🖼️ Generating Retina DMG background..."
    $ARCH_PREFIX sips -z "$DMG_WINDOW_H" "$DMG_WINDOW_W" "$DMG_BACKGROUND_SRC" --out "$DMG_BACKGROUND_1X" >/dev/null
    $ARCH_PREFIX tiffutil -cathidpicheck "$DMG_BACKGROUND_1X" "$DMG_BACKGROUND_SRC" -out "$DMG_BACKGROUND_TIFF"
    BACKGROUND_ARGS=(--background "$DMG_BACKGROUND_TIFF")
else
    echo "⚠️ No DMG background source image found at $DMG_BACKGROUND_SRC, building without one."
fi

rm -f "$FINAL_DMG"

# create-dmg auto-sizes its temporary read-write image from the source
# folder's size with only a small fixed margin, which is too tight for this
# app bundle (GTK4/libadwaita/VTE/Python all bundled in) and errors out with
# "No space left on device" while mounted, even though the host disk is
# nowhere near full. Size the temp image explicitly, generously, instead.
APP_SIZE_MB=$(du -sm "$APP_PATH" | cut -f1)
DMG_SIZE_MB=$(((APP_SIZE_MB * 2) + 200))
echo "📐 Sizing temporary disk image at ${DMG_SIZE_MB}MB (app is ${APP_SIZE_MB}MB)..."

# create-dmg drives the same Finder AppleScript machinery as a hand-rolled
# script, but ships known-good retries/timing for it — a hand-rolled
# hdiutil+osascript script is prone to landing icons at random positions
# and sizes because Finder hasn't finished indexing before .DS_Store is read.
$ARCH_PREFIX create-dmg \
    --volname "${APP_NAME} ${VERSION}" \
    --volicon "thongssh_gtk/icons/thongssh.icns" \
    "${BACKGROUND_ARGS[@]}" \
    --window-pos 200 120 \
    --window-size "$DMG_WINDOW_W" "$DMG_WINDOW_H" \
    --icon-size "$DMG_ICON_SIZE" \
    --icon "${APP_NAME}.app" "$DMG_APP_ICON_X" "$DMG_APP_ICON_Y" \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link "$DMG_APPLICATIONS_X" "$DMG_APPLICATIONS_Y" \
    --disk-image-size "$DMG_SIZE_MB" \
    --no-internet-enable \
    "$FINAL_DMG" \
    "dist/" || true

if [ ! -f "$FINAL_DMG" ]; then
    echo "🛑 create-dmg failed to produce $FINAL_DMG"
    exit 1
fi

echo "✅ Build complete!"
echo "🎉 Output file: ${BUILD_DIR}/thongssh/${FINAL_DMG}"
if [ "$IN_REPO" -eq 1 ]; then
    mkdir -p "$OUTDIR"
    cp "${BUILD_DIR}/thongssh/${FINAL_DMG}" "$OUTDIR/"
    echo "✨ Final DMG copied to: ${OUTDIR}/${DMG_NAME}"
else
    cp "${BUILD_DIR}/thongssh/${FINAL_DMG}" "$ORIGINAL_PWD"
    echo "✨ Final DMG copied to the working directory: ${ORIGINAL_PWD}/${DMG_NAME}"
fi

deactivate
echo "🧹 Temporary files will be cleaned up on exit."