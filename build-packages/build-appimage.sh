#!/usr/bin/env bash
#
# build-appimage.sh — build a self-contained .AppImage for ThongSSH that
# bundles GTK4 4.10.5 + libadwaita 1.3.4 + VTE 0.72.2 (gtk4) — plus every
# transitive shared library either of those three actually needs — all
# compiled/resolved *inside* an Ubuntu 22.04 container. The whole point is
# for the result to only ever need glibc symbols already present on 22.04,
# regardless of how new the machine running this script is.
#
# Why not just run `ldd`/linuxdeploy on whatever's installed here and copy
# that: glibc is only ever backward-compatible, never forward — a binary
# built against a newer glibc than the target's just refuses to start there
# ("version `GLIBC_2.39' not found"). That risk isn't limited to GTK4/
# libadwaita/VTE themselves — `ldd` resolves symbolic library names (like
# "libglib-2.0.so.0") against *whatever machine you run it on*, so even
# library-bundling itself has to happen inside the 22.04 container, or
# you're right back to silently bundling this machine's own (too new) glib/
# cairo/pango/etc. instead of 22.04's. Every step here that touches a
# compiled artifact — building GTK4/libadwaita/VTE, resolving their shared
# library dependencies, bundling gdk-pixbuf loaders, even the Python
# interpreter and its compiled pip dependencies (cryptography/bcrypt/
# pynacl) — runs inside the container. Only icon resizing and final
# .desktop-file text generation (pure file/text manipulation, no compiled
# code involved) happen on the host.
#
# Package/version choices, checked against real upstream meson.build
# requirements and real Ubuntu 22.04 (jammy) package versions:
#   GTK4 4.10.5      needs glib>=2.72.0 pango>=1.50.0 cairo>=1.14
#                     gdk-pixbuf>=2.30 graphene>=1.10 harfbuzz>=2.6
#                     fribidi>=1.0.6 epoxy>=1.4
#   libadwaita 1.3.4 needs gtk4>=4.9.5 glib>=2.72.0
#                     (deliberately NOT 1.4.x — that needs gtk4>=4.11.3
#                     AND glib>=2.76, which would force rebuilding glib too)
#   VTE 0.72.2       needs gtk4>=4.0.1 glib>=2.52 pcre2>=10.21
#                     (-Dgtk4=true -Dgtk3=false — same source repo builds
#                     either GTK version; 22.04 only ships the gtk3 one)
# jammy ships glib 2.72.4, pango 1.50.6, gdk-pixbuf 2.42.8, graphene 1.10.8,
# harfbuzz 2.7.4 — every requirement above is satisfied with room to spare,
# which is why only these 3 packages need building from source at all.
#
# Usage:
#   ./build-appimage.sh [--rebuild-stack]
#
# Environment:
#   OUTDIR — where to put the .AppImage (default: $PWD/dist)
#
# Requirements: docker. That's it — everything else (meson, ninja, pip,
# linuxdeploy, appimagetool, ...) lives inside the container image this
# script builds, never on the host running this script.
set -euo pipefail

APP=thongssh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
[[ -f "$SRC_ROOT/thongssh_gtk/constants.py" ]] || {
    echo "!! $SRC_ROOT does not look like a thongssh source tree" >&2; exit 1; }

APP_ID="$(sed -nE 's/^APP_ID[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/p' \
    "$SRC_ROOT/thongssh_gtk/constants.py" | head -1)"
VERSION="$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/p' \
    "$SRC_ROOT/thongssh_gtk/constants.py" | head -1)"

OUTDIR="${OUTDIR:-$PWD/dist}"
BUILD_ROOT="$SCRIPT_DIR/.build-appimage"

REBUILD_STACK=0
for arg in "$@"; do
    case "$arg" in
        --rebuild-stack) REBUILD_STACK=1 ;;
        -h|--help)
            sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\n==> %s\n' "$1"; }

command -v docker >/dev/null || { echo "!! docker is required" >&2; exit 1; }
mkdir -p "$OUTDIR" "$BUILD_ROOT"

# ============================================================================
# Stage A — build (or reuse) the image containing GTK4/libadwaita/VTE(gtk4)
# built from source, plus Python + thongssh's pip deps, plus linuxdeploy/
# appimagetool — all Docker's own image cache, keyed by $IMAGE_TAG, which
# is derived from a hash of this Dockerfile's own content (see below) —
# not just thongssh's version number, which would otherwise happily keep
# reusing a stale image from a previous run of an *older* copy of this
# very script (e.g. one built before linuxdeploy/appimagetool got baked
# in), silently missing tools it never noticed it was missing until Stage
# B failed with "command not found".
# ============================================================================
cat > "$BUILD_ROOT/Dockerfile.stack" <<'DOCKERFILE_EOF'
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git ca-certificates curl pkg-config meson ninja-build \
    python3 python3-pip python3-gi python3-gi-cairo gir1.2-glib-2.0 \
    gobject-introspection libgirepository1.0-dev \
    libglib2.0-dev libpango1.0-dev libcairo2-dev libgdk-pixbuf-2.0-dev \
    libgraphene-1.0-dev libharfbuzz-dev libfribidi-dev libepoxy-dev \
    libx11-dev libxext-dev libxi-dev libxrandr-dev libxcursor-dev libxdamage-dev \
    libxfixes-dev libxinerama-dev libxkbcommon-dev libxkbcommon-x11-dev \
    libwayland-dev wayland-protocols libegl1-mesa-dev libgl1-mesa-dev \
    libpcre2-dev libgnutls28-dev libxml2-dev libxml2-utils \
    desktop-file-utils \
 && rm -rf /var/lib/apt/lists/*

ENV PREFIX=/opt/thongssh-stack
ENV PKG_CONFIG_PATH=$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig
ENV LD_LIBRARY_PATH=$PREFIX/lib
WORKDIR /src

# --libdir=lib (not the multiarch default) so everything ends up in one
# predictable $PREFIX/lib regardless of how meson would otherwise detect
# the install layout for this distro.
RUN git clone --branch 4.10.5 --depth 1 https://gitlab.gnome.org/GNOME/gtk.git gtk4 \
 && meson setup gtk4/build gtk4 --prefix="$PREFIX" --libdir=lib \
        -Dintrospection=enabled -Ddemos=false -Dbuild-testsuite=false \
        -Dbuild-examples=false -Dgtk_doc=false -Dmedia-gstreamer=disabled \
        -Dvulkan=disabled -Dcloudproviders=disabled \
 && ninja -C gtk4/build install

# g-ir-scanner (used while building libadwaita/VTE below, to cross-reference
# "--include=Gtk-4.0") looks for other libraries' .gir source files via
# XDG_DATA_DIRS/gir-1.0 and their .typelib via GI_TYPELIB_PATH — neither
# defaults to a non-standard --prefix like ours, so without this,
# introspection generation for anything built after GTK4 fails with
# "Couldn't find include 'Gtk-4.0.gir'" even though GTK4 itself built fine.
ENV XDG_DATA_DIRS=$PREFIX/share:/usr/local/share:/usr/share
ENV GI_TYPELIB_PATH=$PREFIX/lib/girepository-1.0

RUN git clone --branch 1.3.4 --depth 1 https://gitlab.gnome.org/GNOME/libadwaita.git libadwaita \
 && meson setup libadwaita/build libadwaita --prefix="$PREFIX" --libdir=lib \
        -Dintrospection=enabled -Dtests=false -Dexamples=false -Dgtk_doc=false -Dvapi=false \
 && ninja -C libadwaita/build install

RUN git clone --branch 0.72.2 --depth 1 https://gitlab.gnome.org/GNOME/vte.git vte \
 && meson setup vte/build vte --prefix="$PREFIX" --libdir=lib \
        -Dgtk3=false -Dgtk4=true -D_systemd=false -Dicu=false -Dvapi=false -Dglade=false \
 && ninja -C vte/build install

# thongssh's own pip deps (paramiko/keyring/cryptography — which have
# compiled C/Rust extensions: bcrypt/pynacl/cryptography's own backend)
# are installed HERE too, not on whatever machine happens to run this
# script — pip run from a *different* (newer-glibc) machine can resolve a
# manylinux wheel that machine considers compatible with itself, which
# isn't guaranteed to also be compatible with 22.04. Run from inside this
# container, pip only ever sees 22.04's own glibc when picking a wheel.
COPY requirements.appimage.txt /src/requirements.txt
RUN pip3 install --disable-pip-version-check --no-compile \
        --target=/opt/thongssh-pydeps -r /src/requirements.txt

# linuxdeploy/appimagetool baked into the image (not downloaded on the
# host) so the library-dependency resolution step below — which needs to
# see *this container's* libraries, not the host's — can run inside a
# `docker run` of this same image with no extra volume-mounted tooling.
RUN curl -L -o /usr/local/bin/linuxdeploy \
        https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage \
 && curl -L -o /usr/local/bin/appimagetool \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
 && chmod +x /usr/local/bin/linuxdeploy /usr/local/bin/appimagetool

# appimagetool shells out to `file` (libmagic) internally. librsvg2-common
# provides the SVG gdk-pixbuf loader (libgdk-pixbuf-2.0-dev above only
# pulls in the base library + PNG/JPEG support — SVG has always been a
# separate runtime plugin); without it, any icon that only exists as an
# SVG (no PNG fallback in the icon theme) — our own bundled
# split-columns-symbolic/split-rows-symbolic, and plenty of standard
# Adwaita symbolic icons too — silently renders blank instead of failing
# loudly. libgdk-pixbuf2.0-bin provides gdk-pixbuf-query-loaders itself
# (also NOT part of the -dev package) — used below to regenerate
# loaders.cache for the bundled loaders; without the binary, that step
# silently produces an empty cache (its own stderr redirected to
# /dev/null, since a missing *cache* used to be the tolerable failure
# mode this was written for) and NO loader — not just SVG — actually
# registers, though most raster formats render anyway via GTK4's own
# built-in PNG/JPEG texture loading, which masked this for everything
# except SVG. All plain runtime packages appended as their own separate,
# late layer, so adding one here never costs re-running the expensive
# GTK4/libadwaita/VTE builds above.
RUN apt-get update && apt-get install -y --no-install-recommends \
        file librsvg2-common libgdk-pixbuf2.0-bin \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE_EOF

grep -v 'pyobjc' "$SRC_ROOT/requirements.txt" > "$BUILD_ROOT/requirements.appimage.txt"

STACK_HASH="$(cat "$BUILD_ROOT/Dockerfile.stack" "$BUILD_ROOT/requirements.appimage.txt" | sha256sum | cut -c1-12)"
IMAGE_TAG="thongssh-appimage-stack:$VERSION-$STACK_HASH"

if [[ "$REBUILD_STACK" -eq 1 ]]; then
    docker rmi -f "$IMAGE_TAG" >/dev/null 2>&1 || true
fi

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    log "Stage A: building $IMAGE_TAG"
    echo "    Compiles GTK4/libadwaita/VTE from source inside ubuntu:22.04 —"
    echo "    expect 15-30+ minutes the first time. Cached as a Docker image"
    echo "    afterward; reruns skip this entirely unless this script's own"
    echo "    Dockerfile changes, or you pass --rebuild-stack."
    docker build -t "$IMAGE_TAG" -f "$BUILD_ROOT/Dockerfile.stack" "$BUILD_ROOT"
else
    log "Stage A: reusing existing image $IMAGE_TAG (pass --rebuild-stack to force a rebuild)"
fi

# ============================================================================
# Host-side asset prep — icon resizing + .desktop text. Pure file/image
# manipulation, no compiled-binary/glibc concerns, so this is fine on
# whatever machine runs this script; mounted read-only into Stage B below.
# ============================================================================
log "Preparing icons + .desktop (on the host — no compiled code involved)"
ASSETS_DIR="$BUILD_ROOT/assets"
rm -rf "$ASSETS_DIR"; install -d "$ASSETS_DIR/icons"
ICON_SIZES="512 256 128 64 48"
resizer=""
if command -v magick >/dev/null 2>&1; then resizer="magick"
elif command -v convert >/dev/null 2>&1; then resizer="convert"
elif python3 -c 'import PIL' >/dev/null 2>&1; then resizer="pil"; fi
[[ -z "$resizer" ]] && ICON_SIZES="512"
for variant in "thongssh:default" "thongssh_orig:orig"; do
    src="$SRC_ROOT/thongssh_gtk/icons/${variant%%:*}.png"
    stem="${variant##*:}"
    for s in $ICON_SIZES; do
        case "$resizer" in
            pil) python3 -c "from PIL import Image; Image.open('$src').resize(($s,$s), Image.LANCZOS).save('$ASSETS_DIR/icons/$stem-$s.png')" ;;
            "")  cp "$src" "$ASSETS_DIR/icons/$stem-$s.png" ;;
            *)   "$resizer" "$src" -resize "${s}x${s}" "$ASSETS_DIR/icons/$stem-$s.png" ;;
        esac
    done
done
echo "$ICON_SIZES" > "$ASSETS_DIR/icon-sizes.txt"

cat > "$ASSETS_DIR/app.desktop" <<EOF
[Desktop Entry]
Name=ThongSSH
GenericName=SSH Connection Manager
Comment=Minimalist SSH/Telnet/SFTP client
Exec=$APP
Icon=$APP_ID
Type=Application
Terminal=false
Categories=GTK;GNOME;Network;RemoteAccess;
Keywords=ssh;sftp;telnet;terminal;
StartupWMClass=$APP_ID
EOF

cat > "$ASSETS_DIR/AppRun" <<'APPRUN_EOF'
#!/bin/sh
# Sets up the bundled runtime by hand instead of relying on linuxdeploy's
# own patchelf-based RPATH rewriting (deliberately disabled during Stage B
# — patchelf rewriting a bundled Python interpreter/its C-extension
# modules is a well-known way to quietly corrupt it).
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$HERE/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
# loaders.cache as bundled has "@APPDIR@" placeholders (see stage_b.sh)
# standing in for $HERE, which is only known at run time — different every
# launch (a fresh /tmp/.mount_XXXXXX for a FUSE mount, or wherever this was
# extracted to). $HERE itself is read-only (squashfs), so the patched
# version is written to a per-run temp file instead, cleaned up on exit.
PIXBUF_CACHE="$(mktemp /tmp/thongssh-loaders-XXXXXX.cache)"
trap 'rm -f "$PIXBUF_CACHE"' EXIT
sed "s|@APPDIR@|$HERE|g" "$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" > "$PIXBUF_CACHE"
export GDK_PIXBUF_MODULE_FILE="$PIXBUF_CACHE"
export XDG_DATA_DIRS="$HERE/usr/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
PYVER="$(ls "$HERE/usr/lib" | grep -m1 '^python3\.')"
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/$PYVER:$HERE/usr/lib/$PYVER/site-packages:$HERE/usr/lib/thongssh${PYTHONPATH:+:$PYTHONPATH}"
exec "$HERE/usr/bin/python3" "$HERE/usr/lib/thongssh/thongssh.py" "$@"
APPRUN_EOF
chmod +x "$ASSETS_DIR/AppRun"

# ============================================================================
# Stage B — assemble AppDir, resolve+bundle every shared library, and
# package the AppImage, ALL inside a container run of $IMAGE_TAG — so
# every compiled artifact touched (the GTK stack, Python, its pip deps,
# and every transitive .so linuxdeploy discovers) is genuinely 22.04-native,
# never resolved against whatever's installed on the host running this script.
# ============================================================================
log "Stage B: assembling + bundling + packaging inside a container"

cat > "$BUILD_ROOT/stage_b.sh" <<'STAGE_B_EOF'
#!/bin/sh
set -eu
APPDIR=/build/AppDir
mkdir -p "$APPDIR/usr/lib/$APP" "$APPDIR/usr/bin" "$APPDIR/usr/lib/girepository-1.0" \
         "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor"

cp /src/thongssh.py "$APPDIR/usr/lib/$APP/"
cp -r /src/thongssh_gtk "$APPDIR/usr/lib/$APP/"
find "$APPDIR/usr/lib/$APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR/usr/lib/$APP" -name '*.pyc' -delete 2>/dev/null || true

echo "==> Icons + .desktop"
ICON_SIZES="$(cat /assets/icon-sizes.txt)"
for s in $ICON_SIZES; do
    install -D -m0644 "/assets/icons/default-$s.png" "$APPDIR/usr/share/icons/hicolor/${s}x${s}/apps/$APP_ID.png"
    install -D -m0644 "/assets/icons/orig-$s.png" "$APPDIR/usr/share/icons/hicolor/${s}x${s}/apps/$APP_ID.orig.png"
done
cp /assets/app.desktop "$APPDIR/usr/share/applications/$APP_ID.desktop"
ln -sf "usr/share/applications/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
ln -sf "usr/share/icons/hicolor/256x256/apps/$APP_ID.png" "$APPDIR/$APP_ID.png"
ln -sf "usr/share/icons/hicolor/256x256/apps/$APP_ID.png" "$APPDIR/.DirIcon"
cp /assets/AppRun "$APPDIR/AppRun"

echo "==> Python runtime (this container's own python3 + stdlib)"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
TARGET_PY_DIR="$APPDIR/usr/lib/python$PYVER"
TARGET_SITE="$TARGET_PY_DIR/site-packages"
mkdir -p "$TARGET_SITE"
cp -aL "$(readlink -f "$(command -v python3)")" "$APPDIR/usr/bin/python3"
cp -a "/usr/lib/python$PYVER/." "$TARGET_PY_DIR/"
cp -a /usr/lib/python3/dist-packages/gi "$TARGET_SITE/gi"
[ -d /usr/lib/python3/dist-packages/cairo ] && cp -a /usr/lib/python3/dist-packages/cairo "$TARGET_SITE/cairo"
cp -a /opt/thongssh-pydeps/. "$TARGET_SITE/"
find "$TARGET_SITE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Typelibs (Gtk-4.0/Adw-1/Vte-3.91/Gdk-4.0/Gsk-4.0 from the built stack, everything else from this container's own system libs)"
SYS_TYPELIB_DIR="$(dirname "$(find /usr/lib -name 'GLib-2.0.typelib' | head -1)")"
for typelib in Gtk-4.0 Adw-1 Vte-3.91 Gio-2.0 GLib-2.0 GObject-2.0 GdkPixbuf-2.0 \
               Gdk-4.0 Pango-1.0 PangoCairo-1.0 HarfBuzz-0.0 Graphene-1.0 Gsk-4.0 cairo-1.0; do
    dest="$APPDIR/usr/lib/girepository-1.0/$typelib.typelib"
    if [ -f "/opt/thongssh-stack/lib/girepository-1.0/$typelib.typelib" ]; then
        cp -a "/opt/thongssh-stack/lib/girepository-1.0/$typelib.typelib" "$dest"
    elif [ -f "$SYS_TYPELIB_DIR/$typelib.typelib" ]; then
        cp -a "$SYS_TYPELIB_DIR/$typelib.typelib" "$dest"
    else
        echo "  !! missing typelib: $typelib" >&2
    fi
done

echo "==> Resolving + bundling shared libraries with linuxdeploy (patchelf disabled)"
# patchelf rewriting RPATHs on a bundled Python interpreter/its C-extension
# modules is a well-known way to quietly corrupt it — a no-op patchelf is
# put first on PATH so linuxdeploy only copies files; AppRun sets
# LD_LIBRARY_PATH/GI_TYPELIB_PATH by hand instead (see /assets/AppRun).
NOOP_DIR="$(mktemp -d)"
printf '#!/bin/sh\nexit 0\n' > "$NOOP_DIR/patchelf"
chmod +x "$NOOP_DIR/patchelf"
LD_PATH="/opt/thongssh-stack/lib"
for lib in libgtk-4.so libadwaita-1.so libvte-2.91-gtk4.so; do
    found_any=0
    for f in "$LD_PATH/$lib"*; do
        [ -e "$f" ] || continue
        # ALL matches, not just one — "libadwaita-1.so*" matches both the
        # bare dev-symlink (libadwaita-1.so) and the real SONAME'd file
        # (libadwaita-1.so.0) the dynamic linker actually dlopen()s at
        # runtime. Picking only the alphabetically-first match (a bare
        # `sort | head -1`) silently bundled just the symlink and left the
        # real .so.0 out entirely — the typelib's dlopen("libadwaita-1.so.0")
        # then fell through to whatever the *host* happens to have under
        # that name (or, on the real Ubuntu 22.04 target, nothing at all).
        found_any=1
        BUNDLE_LIBS="${BUNDLE_LIBS:-} --library $f"
    done
    [ "$found_any" = 1 ] || { echo "!! $lib not found in $LD_PATH" >&2; exit 1; }
done

# librsvg backs the SVG gdk-pixbuf loader (bundled below) but is dlopen'd
# by gdk-pixbuf at runtime rather than being a link-time dependency of
# gtk4/libadwaita/vte — linuxdeploy's automatic ldd-walk from the
# --library entries above would never discover it on its own. It's also a
# plain apt package (see librsvg2-common above), not part of our
# custom-built $LD_PATH stack, so it lives in the system multiarch dir.
SYS_LIB_PATH="/usr/lib/x86_64-linux-gnu"
for lib in librsvg-2.so; do
    found_any=0
    for f in "$SYS_LIB_PATH/$lib"*; do
        [ -e "$f" ] || continue
        found_any=1
        BUNDLE_LIBS="${BUNDLE_LIBS:-} --library $f"
    done
    [ "$found_any" = 1 ] || { echo "!! $lib not found in $SYS_LIB_PATH" >&2; exit 1; }
done
EXTRACTED="$(mktemp -d)"
(cd "$EXTRACTED" && APPIMAGE_EXTRACT_AND_RUN=1 linuxdeploy --appimage-extract >/dev/null 2>&1)
cp "$NOOP_DIR/patchelf" "$EXTRACTED/squashfs-root/usr/bin/patchelf"
NO_STRIP=true "$EXTRACTED/squashfs-root/AppRun" \
    --appdir "$APPDIR" \
    --executable "$APPDIR/usr/bin/python3" \
    $BUNDLE_LIBS \
    --desktop-file "$APPDIR/usr/share/applications/$APP_ID.desktop" \
    --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
rm -rf "$NOOP_DIR" "$EXTRACTED"

echo "==> Bundling gdk-pixbuf loaders"
LOADER_DIR="$(dirname "$(find /usr/lib -path '*gdk-pixbuf-2.0*/loaders' -type d | head -1)")"
# Not on PATH — it's a private helper shipped inside libgdk-pixbuf-2.0-0's
# own lib dir (not the -bin/-dev packages, despite the name), meant to be
# invoked by full path, not run directly by users.
QUERY_LOADERS="$(find /usr/lib -name gdk-pixbuf-query-loaders | head -1)"
if [ -n "$LOADER_DIR" ] && [ -d "$LOADER_DIR/loaders" ] && [ -n "$QUERY_LOADERS" ]; then
    mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
    cp -a "$LOADER_DIR/loaders/"*.so "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders/"
    GDK_PIXBUF_MODULEDIR="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders" \
        "$QUERY_LOADERS" "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders/"*.so \
        > "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
    # Not "|| true" — an empty/missing cache here used to fail silently
    # (gdk-pixbuf-query-loaders wasn't even installed at one point) and
    # every loader, not just the SVG one, quietly stopped registering.
    # Most raster formats still rendered anyway via GTK4's own built-in
    # PNG/JPEG texture loading, which masked the breakage for everything
    # except SVG-only icons (symbolic icons with no PNG fallback) — caught
    # only by someone noticing blank toolbar buttons, not a build failure.
    [ -s "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ] || {
        echo "!! loaders.cache came out empty — gdk-pixbuf-query-loaders" >&2
        echo "   missing or failed; SVG/other icons would render blank" >&2
        exit 1
    }
    # gdk-pixbuf-query-loaders bakes each loader's path in as an ABSOLUTE
    # path — "$APPDIR" as it exists right now, inside this build container
    # (/build/AppDir). An AppImage mounts at a different, randomly-named
    # path every single run (/tmp/.mount_XXXXXX, or wherever it's been
    # extracted to) — with the real path baked in, gdk-pixbuf would try to
    # dlopen a path that doesn't exist at runtime and every loader (not
    # just SVG) would silently fail to load, which is *worse* than not
    # having a cache at all (GTK4's own built-in PNG/JPEG texture loading
    # at least covers raster formats when there's no cache; a cache full
    # of dead paths short-circuits even that fallback). Swap in a
    # placeholder token here; AppRun rewrites it to the real mount path —
    # which it, unlike this build script, actually knows — on every launch.
    sed -i "s|$APPDIR|@APPDIR@|g" "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
else
    echo "!! no gdk-pixbuf loader modules found — icons would not render" >&2
    exit 1
fi

echo "==> Packaging the AppImage"
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 appimagetool "$APPDIR" "/output/ThongSSH-$VERSION-x86_64.AppImage"
echo "==> Done: /output/ThongSSH-$VERSION-x86_64.AppImage"
STAGE_B_EOF

docker run --rm \
    -v "$SRC_ROOT:/src:ro" \
    -v "$ASSETS_DIR:/assets:ro" \
    -v "$BUILD_ROOT/stage_b.sh:/stage_b.sh:ro" \
    -v "$OUTDIR:/output" \
    -e APP="$APP" -e APP_ID="$APP_ID" -e VERSION="$VERSION" \
    "$IMAGE_TAG" sh /stage_b.sh

echo
echo "==> Built: $OUTDIR/ThongSSH-$VERSION-x86_64.AppImage"
echo "==> Test it on a real (or containerized) Ubuntu 22.04 box before trusting it —"
echo "    everything compiled/resolved happened inside the ubuntu:22.04 build"
echo "    container, but the final smoke test still needs to happen on 22.04 itself."
