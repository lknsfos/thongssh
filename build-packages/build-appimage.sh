#!/usr/bin/env bash
#
# build-appimage.sh — build a self-contained .AppImage for ThongSSH that
# bundles GLib 2.76.6 + GTK4 4.12.5 + libadwaita 1.4.3 + VTE 0.72.2 (gtk4)
# — plus every transitive shared library any of those actually needs — all
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
# compiled artifact — building GLib/GTK4/libadwaita/VTE, resolving their
# shared library dependencies, bundling gdk-pixbuf loaders, even the Python
# interpreter and its compiled pip dependencies (cryptography/bcrypt/
# pynacl/PyGObject/pycairo — see the PyGObject comment further down for why
# those two are built from source too now, not copied off this container's
# own apt packages) — runs inside the container. Only icon resizing and
# final .desktop-file text generation (pure file/text manipulation, no
# compiled code involved) happen on the host.
#
# Package/version choices, checked against real upstream meson.build
# requirements and real Ubuntu 22.04 (jammy) package versions:
#   GLib 2.76.6      needs meson>=0.60.0 libffi>=3.0.0 pcre2>=10.32
#                     (jammy: libffi8 3.4.2, libpcre2-8 10.39 — meson
#                     itself already gets upgraded past 0.60 for the
#                     PyGObject build below, so no extra bump needed here)
#   GTK4 4.12.5      needs glib>=2.76.0 pango>=1.50.0 cairo>=1.14
#                     gdk-pixbuf>=2.30 graphene>=1.10 harfbuzz>=2.6
#                     fribidi>=1.0.6 epoxy>=1.4 — identical to 4.10.5's own
#                     requirements except glib (was >=2.72.0), which is
#                     exactly why GLib is the only *additional* thing that
#                     needed building from source to move onto this GTK4
#                     series at all. Also needs gobject-introspection
#                     >=1.76.0 (jammy: 1.72.0), but meson handles that one
#                     itself, silently falling back to building its own
#                     bundled subproject copy instead of failing outright
#                     — no extra build step needed from this script for
#                     it, just python3-dev + flex/bison for that
#                     subproject's own build (its scanner is generated
#                     from a lexer/parser grammar)
#   libadwaita 1.4.3 needs gtk4>=4.11.3 glib>=2.76.0 — bumped from 1.3.4
#                     specifically for Adw.SwitchRow/Adw.SpinRow (added in
#                     1.4), which the app's own Settings dialog uses
#                     extensively; 1.3.4 doesn't have them at all, which
#                     surfaced as a real, live-reproduced AttributeError
#                     the moment Settings was opened in the AppImage —
#                     never caught earlier since that specific dialog
#                     hadn't been exercised inside the actual AppImage
#                     runtime until then
#   VTE 0.72.2       needs gtk4>=4.0.1 glib>=2.52 pcre2>=10.21
#                     (-Dgtk4=true -Dgtk3=false — same source repo builds
#                     either GTK version; 22.04 only ships the gtk3 one)
# jammy ships pango 1.50.6, gdk-pixbuf 2.42.8, graphene 1.10.8, harfbuzz
# 2.7.4 — every one of those requirements is satisfied with room to spare
# regardless of the GLib bump, which is why pango/cairo/gdk-pixbuf/
# graphene/harfbuzz/fribidi/epoxy still don't need building from source —
# only GLib joins GTK4/libadwaita/VTE in that list now. GLib's own ABI
# stability guarantee (a binary built against older 2.x headers always
# runs fine against a newer 2.x runtime) is what makes this safe: pango/
# cairo/etc. stay linked against their *headers'* 2.72-era expectations
# at compile time, but at actual runtime every one of them, plus GTK4/
# libadwaita/VTE, resolves the exact same loaded libglib-2.0.so.0 — our
# newer 2.76.6 build, found first via LD_LIBRARY_PATH — since a process
# can only ever have one instance of a given soname loaded at once.
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
    build-essential git ca-certificates curl pkg-config meson ninja-build cmake \
    flex bison gperf gettext itstool bash-completion \
    python3 python3-pip python3-dev python3-gi python3-gi-cairo gir1.2-glib-2.0 \
    gobject-introspection libgirepository1.0-dev \
    libglib2.0-dev libpango1.0-dev libcairo2-dev libgdk-pixbuf-2.0-dev \
    libgraphene-1.0-dev libharfbuzz-dev libfribidi-dev libepoxy-dev \
    libx11-dev libxext-dev libxi-dev libxrandr-dev libxcursor-dev libxdamage-dev \
    libxfixes-dev libxinerama-dev libxkbcommon-dev libxkbcommon-x11-dev \
    libwayland-dev wayland-protocols libegl1-mesa-dev libgl1-mesa-dev \
    libpcre2-dev libgnutls28-dev libxml2-dev libxml2-utils libcurl4-gnutls-dev \
    libzstd-dev \
    libffi-dev zlib1g-dev \
    desktop-file-utils \
 && rm -rf /var/lib/apt/lists/*

ENV PREFIX=/opt/thongssh-stack
ENV PKG_CONFIG_PATH=$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig
ENV LD_LIBRARY_PATH=$PREFIX/lib
WORKDIR /src

# jammy's own apt meson (0.61.2) is too old for GTK4 4.12 (needs >=0.63.0)
# and, later, for PyGObject's own build backend meson-python (needs
# >=0.63.3) — upgraded once, up front, before anything that needs it
# rather than separately/later, since every build below needs at least
# one of these two floors. meson-python itself is unrelated to any of
# GLib/GTK4/libadwaita/VTE — installed here too only because it's cheap
# and keeps every meson-related version bump in one place.
RUN pip3 install --disable-pip-version-check --upgrade 'meson>=0.63.3' meson-python

# GLib itself, built first (into the same $PREFIX, ahead of everything
# that needs it) — GTK 4.12/libadwaita 1.4 both need glib>=2.76.0, jammy's
# own system package is 2.72.4. Every *other* requirement both still have
# (pango/cairo/gdk-pixbuf/graphene/harfbuzz/fribidi/epoxy) is unchanged
# from the 4.10.5/1.3.4 pairing this used to build, still satisfied by
# jammy's own packages — GLib is the only additional thing that needs
# building from source. GLib has no -Dintrospection switch of its own
# (unlike GTK4/libadwaita below) — it just builds its typelib whenever
# gobject-introspection is available, which it is here. gtk_doc/tests/
# selinux/libmount all disabled: none of them matter for a runtime
# bundle, and selinux/libmount specifically would otherwise pull in dev
# packages this image has no other reason to carry. (man pages already
# default to off.)
RUN git clone --branch 2.76.6 --depth 1 https://gitlab.gnome.org/GNOME/glib.git glib \
 && meson setup glib/build glib --prefix="$PREFIX" --libdir=lib \
        -Dgtk_doc=false -Dtests=false -Dinstalled_tests=false \
        -Dselinux=disabled -Dlibmount=disabled \
 && ninja -C glib/build install

# --libdir=lib (not the multiarch default) so everything ends up in one
# predictable $PREFIX/lib regardless of how meson would otherwise detect
# the install layout for this distro.
RUN git clone --branch 4.12.5 --depth 1 https://gitlab.gnome.org/GNOME/gtk.git gtk4 \
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

# libadwaita 1.4+ unconditionally depends on appstream (no meson option
# to turn it off — it's a hard entry in libadwaita_deps, presumably for
# AdwAboutWindow's release-notes-from-appdata feature) — jammy has no
# appstream pkg-config file either, so meson falls back to building ITS
# OWN bundled subproject (github.com/ximion/appstream, not on GNOME's own
# gitlab, per its own .wrap file), which brings its own small dependency
# chain along: libcurl (libcurl4-gnutls-dev above — matching this stack's
# existing GnuTLS choice for VTE, rather than also pulling in OpenSSL as
# a second TLS stack), libfyaml (its YAML-format metadata parser — no
# option to disable this one either), libzstd (compression support, on
# by default, jammy's libzstd-dev is new enough), gperf (a plain
# code-generator binary, used unconditionally at meson.build:244 to
# build a string->enum lookup table — no dev package needed, just the
# tool itself), gettext (its po/meson.build calls i18n.gettext()
# unconditionally for translations; without msgfmt present meson's i18n
# module returns void from that call instead of the expected dict,
# which this meson version then fails to assign — installing gettext
# avoids hitting that path at all rather than working around it) and
# itstool (data/meson.build unconditionally uses it too, to translate
# its .desktop/.metainfo.xml data files via the same i18n machinery) and
# bash-completion (contrib/meson.build wants its pkg-config file to find
# the completions install directory — its own bash-completion option
# defaults to on, and libadwaita doesn't override it). After that, its
# default-on `man` option wanted xsltproc + Docbook XSL stylesheets too
# (docs/meson.build) — rather than chase yet another apt package for a
# man page we'll never install from this bundle anyway, appstream's own
# `man`/`docs` options are disabled directly via meson subproject
# overrides (`-D<subproject>:<option>=value`, passed on libadwaita's own
# meson setup line below since libadwaita itself never exposes them).
#
# libfyaml specifically can't just be `apt install libfyaml-dev` though:
# jammy/universe only has 0.7.12, appstream 1.1.7's meson.build requires
# >=0.8 and hard-fails the version check (no fallback wrap of its own for
# this one dependency, unlike appstream's own status as libadwaita's
# fallback). So it's built from source too, straight into the same
# $PREFIX, via its CMake build (simpler than its autotools path, which
# has no committed ./configure and would need autoreconf). BUILD_TESTING
# off skips its network-touching test suite; everything else defaults
# are fine for a runtime bundle.
RUN git clone --branch v0.9.6 --depth 1 https://github.com/pantoniou/libfyaml.git libfyaml \
 && cmake -S libfyaml -B libfyaml/build -G Ninja \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
 && cmake --build libfyaml/build \
 && cmake --install libfyaml/build

RUN git clone --branch 1.4.3 --depth 1 https://gitlab.gnome.org/GNOME/libadwaita.git libadwaita \
 && meson setup libadwaita/build libadwaita --prefix="$PREFIX" --libdir=lib \
        -Dintrospection=enabled -Dtests=false -Dexamples=false -Dgtk_doc=false -Dvapi=false \
        -Dappstream:man=false -Dappstream:docs=false \
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

# PyGObject/pycairo, built from source here too — NOT copied from this
# container's own python3-gi/python3-gi-cairo apt packages (Ubuntu 22.04
# ships PyGObject 3.42.1, contemporary with roughly GTK 4.0-4.2, paired
# against our custom-built, much newer GTK 4.10.5). That mismatch is a
# real, live-reproduced bug, not a theoretical one: right-clicking
# *anywhere* a Gtk.GestureClick is attached (host tree, tabs, terminal)
# calls gesture.get_last_event(), and 3.42.1's marshaling of the returned
# Gdk.Event (a boxed type, not a GObject) corrupts memory badly enough to
# later segfault deep inside CPython's own dict lookup — confirmed via a
# real gdb backtrace, and confirmed absent once PyGObject is rebuilt here
# against our own stack instead. A normal (non-AppImage) install never
# hits this, since there GTK4 and PyGObject are always whatever matched
# pair the OS itself ships — it's specifically this AppImage's "newer
# GTK4 than the 22.04 base" approach that can pull them out of sync.
# python3-dev (Python.h) is needed to compile PyGObject's own C extension
# too, but it's in the main apt install list up top now, not a separate
# layer here — GTK4 4.12's own build turned out to need it already, for
# an unrelated reason: it needs gobject-introspection>=1.76.0 (jammy's
# system package is only 1.72.0), so meson silently falls back to
# building its own bundled copy of gobject-introspection as a subproject,
# and *that* build needs python3-dev regardless of anything PyGObject-
# related.
# PyGObject's build backend (meson-python) shells out to a "meson"
# command on PATH rather than importing it as a Python module — pip's
# usual build-isolation (a private venv for build-time deps, declared in
# PyGObject's own pyproject.toml) doesn't rescue this, since that
# isolation only covers Python imports, not external command resolution.
# Meson itself was already upgraded system-wide up front (see the very
# first RUN after WORKDIR /src) — but that upgrade alone isn't enough
# here: pip build-isolates by default, building each package's declared
# pyproject.toml [build-system] requirements in a private, throwaway venv
# that can't see our upgraded system meson at all — that isolated venv
# gets its OWN meson per PyGObject's own declared (looser/older) pin,
# which is exactly the same "too-old meson" problem all over again just
# one level removed. --no-build-isolation opts out, using the system
# environment directly — meson-python (also installed up front, next to
# meson itself) needs to already be present system-wide for that same
# reason; ninja (also required) is already present system-wide from the
# GTK4/libadwaita/VTE builds above.
#
# PyGObject pinned to 3.50.2, not left unpinned/latest: 3.52.0 onward
# needs girepository-2.0 (a newer, separate GObject-Introspection ABI
# that replaced 1.0), which jammy doesn't have at all — only the
# libgirepository1.0-dev/gobject-introspection-1.0 apt package installed
# above. 3.50.2 is the last release still on girepository-1.0, and
# already comfortably newer than jammy's own stock 3.42.1 — the actual
# bug this whole detour exists for.
# pycairo installed FIRST, on its own: PyGObject's own meson.build probes
# for it at build time via a plain "import cairo" + cairo.get_include(),
# which — with --no-build-isolation running against the system Python —
# otherwise finds jammy's system python3-gi-cairo apt package first (it's
# still importable from /usr/lib/python3/dist-packages regardless of our
# --target), and that one ships no headers at all (a runtime-only
# binding, not meant for anything else to build against), failing with
# "Include dir .../cairo/include does not exist." PYTHONPATH here points
# the *build-time* import at our --target install instead, which (being
# the real pip package) has real headers.
#
# pycairo is ALSO meson-python-based, so it needs --no-build-isolation
# too, for the identical reason PyGObject does above — otherwise pip's
# own isolated build venv reintroduces the same 0.61.2-vs-0.63.3+ mismatch
# one level removed, since that isolation doesn't see our system upgrade.
RUN pip3 install --disable-pip-version-check --no-compile --no-build-isolation \
        --target=/opt/thongssh-pydeps pycairo
RUN PYTHONPATH=/opt/thongssh-pydeps pip3 install --disable-pip-version-check --no-compile --no-build-isolation \
        --target=/opt/thongssh-pydeps 'PyGObject==3.50.2'

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
# gi/cairo come from /opt/thongssh-pydeps (built from source against our
# own GTK4 stack, see the Dockerfile) — NOT this container's own
# python3-gi/python3-gi-cairo apt packages (Ubuntu 22.04 ships PyGObject
# 3.42.1, contemporary with roughly GTK 4.0-4.2) any more. See the
# Dockerfile comment above the PyGObject pip install for the actual bug
# that forced this: mismatched against our newer, custom-built GTK
# 4.10.5, it corrupted memory on nearly every Gtk.GestureClick right-click
# anywhere in the app (get_last_event() returning a Gdk.Event — a boxed
# type, not a GObject — confused its marshaling), reproduced as a real
# segfault, not just a warning.
cp -a /opt/thongssh-pydeps/. "$TARGET_SITE/"
find "$TARGET_SITE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Typelibs (Gtk-4.0/Adw-1/Vte-3.91/Gdk-4.0/Gsk-4.0 from the built stack, everything else from this container's own system libs)"
SYS_TYPELIB_DIR="$(dirname "$(find /usr/lib -name 'GLib-2.0.typelib' | head -1)")"
for typelib in Gtk-4.0 Adw-1 Vte-3.91 Gio-2.0 GLib-2.0 GObject-2.0 GModule-2.0 GdkPixbuf-2.0 \
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
