#!/usr/bin/env bash
#
# build-deb-rpm.sh — build .deb and .rpm packages for ThongSSH.
#
# Fully self-contained: can be run from inside a checkout of the repo, OR
# standalone from anywhere — in that case it clones the repo itself.
#
# Usage:
#   ./build-deb-rpm.sh [deb|rpm|all]        (default: all)
#
# Environment:
#   REPO_URL  — git repo to clone in standalone mode
#               (default: https://github.com/lknsfos/thongssh.git)
#   REF       — branch/tag/commit to build (default: remote default branch)
#   SRC       — use an existing source tree instead of cloning
#   VERSION   — override package version
#               (default: __version__ from thongssh_gtk/constants.py)
#   RELEASE   — package release/revision (default: 1)
#   OUTDIR    — where to put built packages (default: $PWD/dist)
#
# Requirements:
#   deb: dpkg-deb (any Debian/Ubuntu box, or `apt install dpkg` elsewhere)
#   rpm: rpmbuild (`dnf install rpm-build` / `apt install rpm`)
#   standalone mode: git
#
# The app is pure Python (noarch), so both packages can be built on any
# distro — you do NOT need a Fedora box to build the rpm.

set -euo pipefail

APP=thongssh
APP_ID=com.example.thongssh
REPO_URL="${REPO_URL:-https://github.com/lknsfos/thongssh.git}"

TARGET="${1:-all}"
RELEASE="${RELEASE:-1}"
OUTDIR="${OUTDIR:-$PWD/dist}"

CLEANUP_DIR=""
cleanup() { [[ -n "$CLEANUP_DIR" ]] && rm -rf "$CLEANUP_DIR"; }
trap cleanup EXIT

# --- locate or fetch sources --------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${SRC:-}" ]]; then
    SRC_ROOT="$(cd "$SRC" && pwd)"
    echo "==> Using existing source tree: $SRC_ROOT"
elif [[ -d "$SCRIPT_DIR/../thongssh_gtk" ]]; then
    SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "==> Running inside repo checkout: $SRC_ROOT"
else
    command -v git >/dev/null || { echo "!! git is required in standalone mode" >&2; exit 1; }
    CLEANUP_DIR="$(mktemp -d /tmp/${APP}-build.XXXXXX)"
    SRC_ROOT="$CLEANUP_DIR/src"
    echo "==> Standalone mode: cloning $REPO_URL${REF:+ (ref: $REF)}"
    git clone --quiet ${REF:+--branch "$REF"} --depth 1 "$REPO_URL" "$SRC_ROOT" || {
        # --branch only works for branches/tags; fall back to full clone + checkout for commits
        rm -rf "$SRC_ROOT"
        git clone --quiet "$REPO_URL" "$SRC_ROOT"
        [[ -n "${REF:-}" ]] && git -C "$SRC_ROOT" checkout --quiet "$REF"
    }
fi

[[ -f "$SRC_ROOT/thongssh_gtk/constants.py" ]] || {
    echo "!! $SRC_ROOT does not look like a thongssh source tree" >&2; exit 1; }

# --- version ----------------------------------------------------------------
# Priority: $VERSION env override > __version__ in constants.py > git tag
if [[ -z "${VERSION:-}" ]]; then
    VERSION="$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/p' \
        "$SRC_ROOT/thongssh_gtk/constants.py" | head -1)"
fi
if [[ -z "${VERSION:-}" ]] && git -C "$SRC_ROOT" describe --tags --abbrev=0 >/dev/null 2>&1; then
    VERSION="$(git -C "$SRC_ROOT" describe --tags --abbrev=0 | sed 's/^v//')"
fi
if [[ -z "${VERSION:-}" ]]; then
    echo "!! Cannot determine version: no \$VERSION, no __version__ in thongssh_gtk/constants.py, no git tag" >&2
    exit 1
fi

BUILD_ROOT="${CLEANUP_DIR:-$SRC_ROOT}/build"
echo "==> Building $APP $VERSION-$RELEASE (target: $TARGET)"
mkdir -p "$OUTDIR"


# --- icons --------------------------------------------------------------------
# hicolor's index.theme only declares sizes up to 512x512 — an icon installed
# into 1024x1024/ is invisible to the theme lookup (shows as a generic gear).
# Resize the 1024px source into standard sizes; fall back to shipping the
# full-size PNG under 512x512/ if no resizer is available (GTK scales it).
ICON_SIZES="512 256 128 64 48"
ICON_DIR=""
# Two icon variants are shipped: the Safe icon as the default themed icon
# ($APP_ID) and the Original as a second named icon ($APP_ID.orig), so the
# in-app icon switcher can point a user-local .desktop override at it.
prepare_icons() {
    ICON_DIR="$BUILD_ROOT/icons"
    rm -rf "$ICON_DIR"; install -d "$ICON_DIR"
    local resizer=""
    if command -v magick >/dev/null 2>&1; then resizer="magick"
    elif command -v convert >/dev/null 2>&1; then resizer="convert"
    elif python3 -c 'import PIL' >/dev/null 2>&1; then resizer="pil"; fi
    [[ -z "$resizer" ]] && {
        echo "==> No image resizer found (imagemagick/PIL) — shipping 1024px icons as 512x512 only"
        ICON_SIZES="512"
    }

    local variant src stem s
    for variant in "thongssh:default" "thongssh_orig:orig"; do
        src="$SRC_ROOT/thongssh_gtk/icons/${variant%%:*}.png"
        stem="${variant##*:}"
        for s in $ICON_SIZES; do
            case "$resizer" in
                pil) python3 -c "from PIL import Image; Image.open('$src').resize(($s,$s), Image.LANCZOS).save('$ICON_DIR/$stem-$s.png')" ;;
                "")  cp "$src" "$ICON_DIR/$stem-$s.png" ;;
                *)   "$resizer" "$src" -resize "${s}x${s}" "$ICON_DIR/$stem-$s.png" ;;
            esac
        done
    done
    echo "==> Icons prepared (default + orig): $ICON_SIZES"
}

# --- stage a common rootfs ----------------------------------------------------
# /usr/lib/thongssh/                 — the app itself
# /usr/bin/thongssh                  — launcher
# /usr/share/applications/           — .desktop
# /usr/share/icons/hicolor/...       — icon
stage_rootfs() {
    local ROOT="$1"
    rm -rf "$ROOT"
    install -d "$ROOT/usr/lib/$APP" \
               "$ROOT/usr/bin" \
               "$ROOT/usr/share/applications" \
               "$ROOT/usr/share/doc/$APP"

    cp "$SRC_ROOT/thongssh.py" "$ROOT/usr/lib/$APP/"
    cp -r "$SRC_ROOT/thongssh_gtk" "$ROOT/usr/lib/$APP/"
    find "$ROOT/usr/lib/$APP" -name '__pycache__' -type d -exec rm -rf {} + || true
    find "$ROOT/usr/lib/$APP" -name '*.pyc' -delete || true
    chmod 0755 "$ROOT/usr/lib/$APP/thongssh.py"

    # Launcher. thongssh.py adds its own dir to sys.path, so thongssh_gtk
    # resolves without PYTHONPATH tricks.
    cat > "$ROOT/usr/bin/$APP" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/thongssh/thongssh.py "$@"
EOF
    chmod 0755 "$ROOT/usr/bin/$APP"

    # Desktop entry (the one in the repo has a hardcoded dev path — generate a
    # proper one instead).
    cat > "$ROOT/usr/share/applications/$APP_ID.desktop" <<EOF
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

    local s
    for s in $ICON_SIZES; do
        install -D -m0644 "$ICON_DIR/default-$s.png" \
            "$ROOT/usr/share/icons/hicolor/${s}x${s}/apps/$APP_ID.png"
        install -D -m0644 "$ICON_DIR/orig-$s.png" \
            "$ROOT/usr/share/icons/hicolor/${s}x${s}/apps/$APP_ID.orig.png"
    done

    cp "$SRC_ROOT/README.md" "$ROOT/usr/share/doc/$APP/"
}

# --- deb ---------------------------------------------------------------------
build_deb() {
    command -v dpkg-deb >/dev/null || { echo "!! dpkg-deb not found, skipping deb"; return 1; }
    local WORK="$BUILD_ROOT/deb"
    stage_rootfs "$WORK"

    install -d "$WORK/DEBIAN"
    local SIZE
    SIZE=$(du -sk "$WORK/usr" | cut -f1)

    cat > "$WORK/DEBIAN/control" <<EOF
Package: $APP
Version: $VERSION-$RELEASE
Section: net
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-vte-3.91, python3-paramiko, python3-keyring, python3-cryptography, openssh-client
Recommends: gnome-keyring, sshpass, telnet
Maintainer: lknsfos <lknsfos@users.noreply.github.com>
Homepage: https://github.com/lknsfos/thongssh
Description: Minimalist SSH/Telnet/SFTP client (GTK4)
 ThongSSH is a lightweight SSH, Telnet and SFTP client built with
 GTK4/Libadwaita and VTE. Features include a host tree, tabbed and
 split-view terminals, batch commands, an SFTP browser and native
 password vault integration via the keyring library.
 .
 Pre-alpha / experimental software.
EOF

    cat > "$WORK/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
exit 0
EOF
    cp "$WORK/DEBIAN/postinst" "$WORK/DEBIAN/postrm"
    chmod 0755 "$WORK/DEBIAN/postinst" "$WORK/DEBIAN/postrm"

    fakeroot dpkg-deb --build --root-owner-group -Zxz "$WORK" \
        "$OUTDIR/${APP}_${VERSION}-${RELEASE}_all.deb" 2>/dev/null \
      || dpkg-deb --build --root-owner-group -Zxz "$WORK" \
        "$OUTDIR/${APP}_${VERSION}-${RELEASE}_all.deb"
    echo "==> deb: $OUTDIR/${APP}_${VERSION}-${RELEASE}_all.deb"
}

# --- rpm ---------------------------------------------------------------------
build_rpm() {
    command -v rpmbuild >/dev/null || { echo "!! rpmbuild not found, skipping rpm"; return 1; }
    local TOP="$BUILD_ROOT/rpm"
    rm -rf "$TOP"
    install -d "$TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

    # Source tarball
    local TARDIR="$TOP/SOURCES/$APP-$VERSION"
    install -d "$TARDIR"
    cp -r "$SRC_ROOT/thongssh.py" "$SRC_ROOT/thongssh_gtk" "$SRC_ROOT/README.md" "$TARDIR/"
    install -d "$TARDIR/hicolor-icons"
    local s
    for s in $ICON_SIZES; do
        cp "$ICON_DIR/default-$s.png" "$TARDIR/hicolor-icons/default-$s.png"
        cp "$ICON_DIR/orig-$s.png"    "$TARDIR/hicolor-icons/orig-$s.png"
    done
    find "$TARDIR" -name '__pycache__' -type d -exec rm -rf {} + || true
    tar -C "$TOP/SOURCES" -czf "$TOP/SOURCES/$APP-$VERSION.tar.gz" "$APP-$VERSION"
    rm -rf "$TARDIR"

    # Spec is embedded so this script works standalone.
    # typelib(Vte) = 3.91 resolves to vte291-gtk4 on Fedora and is more
    # robust than hardcoding the package name across Fedora/RHEL/openSUSE.
    cat > "$TOP/SPECS/$APP.spec" <<SPEC_EOF
%global app_id $APP_ID
%global icon_sizes $ICON_SIZES

Name:           $APP
Version:        $VERSION
Release:        $RELEASE%{?dist}
Summary:        Minimalist SSH/Telnet/SFTP client (GTK4)
License:        MIT
URL:            https://github.com/lknsfos/thongssh
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       (typelib(Vte) = 3.91 or vte291-gtk4)
Requires:       python3-paramiko
Requires:       python3-keyring
Requires:       python3-cryptography
Requires:       openssh-clients
Recommends:     gnome-keyring
Recommends:     sshpass
Recommends:     telnet

%description
ThongSSH is a lightweight SSH, Telnet and SFTP client built with
GTK4/Libadwaita and VTE. Features include a host tree, tabbed and
split-view terminals, batch commands, an SFTP browser and native
password vault integration via the keyring library.

Pre-alpha / experimental software.

%prep
%autosetup

%build
# nothing to build — pure Python

%install
install -d %{buildroot}%{_prefix}/lib/%{name}
cp -r thongssh.py thongssh_gtk %{buildroot}%{_prefix}/lib/%{name}/
chmod 0755 %{buildroot}%{_prefix}/lib/%{name}/thongssh.py

install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/thongssh/thongssh.py "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/%{name}

install -d %{buildroot}%{_datadir}/applications
cat > %{buildroot}%{_datadir}/applications/%{app_id}.desktop <<EOF
[Desktop Entry]
Name=ThongSSH
GenericName=SSH Connection Manager
Comment=Minimalist SSH/Telnet/SFTP client
Exec=%{name}
Icon=%{app_id}
Type=Application
Terminal=false
Categories=GTK;GNOME;Network;RemoteAccess;
Keywords=ssh;sftp;telnet;terminal;
StartupWMClass=%{app_id}
EOF

for s in %{icon_sizes}; do
    install -D -m0644 "hicolor-icons/default-\$s.png" \\
        "%{buildroot}%{_datadir}/icons/hicolor/\${s}x\${s}/apps/%{app_id}.png"
    install -D -m0644 "hicolor-icons/orig-\$s.png" \\
        "%{buildroot}%{_datadir}/icons/hicolor/\${s}x\${s}/apps/%{app_id}.orig.png"
done

%files
%doc README.md
%{_prefix}/lib/%{name}/
%{_bindir}/%{name}
%{_datadir}/applications/%{app_id}.desktop
%{_datadir}/icons/hicolor/*/apps/%{app_id}*.png

%changelog
* $(LC_ALL=C date '+%a %b %d %Y') lknsfos <lknsfos@users.noreply.github.com> - $VERSION-$RELEASE
- Automated package build
SPEC_EOF

    rpmbuild -bb "$TOP/SPECS/$APP.spec" --define "_topdir $TOP" --quiet
    cp "$TOP/RPMS/noarch/$APP-$VERSION-$RELEASE"*.noarch.rpm "$OUTDIR/"
    echo "==> rpm: $(ls "$OUTDIR/$APP-$VERSION-$RELEASE"*.noarch.rpm)"
}

prepare_icons

case "$TARGET" in
    deb) build_deb ;;
    rpm) build_rpm ;;
    all) build_deb; build_rpm ;;
    *) echo "usage: $0 [deb|rpm|all]"; exit 2 ;;
esac

echo "==> Done."
