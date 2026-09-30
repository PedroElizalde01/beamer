#!/usr/bin/env bash
# Builds linux_app/dist/beamer_<version>_amd64.deb and linux_app/dist/Beamer-<version>-x86_64.AppImage.
#
#   PYTHON=.venv/bin/python linux_app/build_linux.sh
#
# PYTHON needs requirements-linux.txt and pyinstaller installed. The AppImage step downloads
# appimagetool from its GitHub releases the first time, into linux_app/build.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
version="$(tr -d '[:space:]' < "$repo/VERSION")"
python="${PYTHON:-$repo/.venv/bin/python}"
# A path is made absolute before the cd below, so PYTHON=.venv/bin/python works from the repository.
# Not realpath: that follows the venv's symlink out to the system Python.
case "$python" in /*) ;; */*) python="$PWD/$python" ;; esac
maintainer="${MAINTAINER:-$(git -C "$repo" config user.name || true) <$(git -C "$repo" config user.email || true)>}"
[ "$maintainer" = " <>" ] && maintainer="Beamer for Linux <noreply@github.com>"
out="$here/dist"
stage="$here/build/stage"

cd "$here"
"$python" -m PyInstaller --noconfirm --clean --log-level WARN build.spec
rm -rf "$stage"
mkdir -p "$stage"

desktop_entry() {
    printf '[Desktop Entry]\nType=Application\nName=Beamer\nComment=One keyboard and mouse for this machine and your PC\n'
    printf 'Exec=%s\nIcon=beamer\nCategories=Utility;\nStartupWMClass=beamer\nTerminal=false\n' "$1"
}

# The .deb: the bundle under /opt, `beamer` on the PATH, a menu entry and the icon.
deb="$stage/deb"
mkdir -p "$deb/DEBIAN" "$deb/opt" "$deb/usr/bin" "$deb/usr/share/applications" "$deb/usr/share/icons/hicolor/256x256/apps"
cp -a "$out/beamer" "$deb/opt/beamer"
ln -s /opt/beamer/beamer "$deb/usr/bin/beamer"
desktop_entry beamer > "$deb/usr/share/applications/beamer.desktop"
cp "$repo/Beamer.png" "$deb/usr/share/icons/hicolor/256x256/apps/beamer.png"
# What apt reports as the space it takes, in KiB.
installed_kib="$(du -sk --exclude=DEBIAN "$deb" | cut -f1)"
cat > "$deb/DEBIAN/control" <<EOF
Package: beamer
Version: $version
Architecture: amd64
Installed-Size: $installed_kib
Maintainer: $maintainer
Depends: xclip
Section: utils
Priority: optional
Homepage: https://github.com/PedroElizalde01/beamer
Description: One keyboard and mouse for this Linux machine and a Windows PC
 Push the pointer off the edge of the screen and it carries on onto the PC running Beamer
 for Windows, or double-tap a key. The clipboard comes with it, in both directions.
 Needs an X11 session.
EOF
# 755 and 644 whatever the umask that built it: a group-writable package file is a policy error.
chmod -R go-w "$deb"
dpkg-deb --root-owner-group --build "$deb" "$out/beamer_${version}_amd64.deb"

# The AppImage: the same bundle behind AppRun. xclip is the one thing it takes from the system.
app="$stage/Beamer.AppDir"
mkdir -p "$app/usr/lib"
cp -a "$out/beamer" "$app/usr/lib/beamer"
cat > "$app/AppRun" <<'EOF'
#!/bin/sh
exec "$(dirname "$(readlink -f "$0")")/usr/lib/beamer/beamer" "$@"
EOF
chmod +x "$app/AppRun"
desktop_entry beamer > "$app/beamer.desktop"
cp "$repo/Beamer.png" "$app/beamer.png"
tool="$here/build/appimagetool"
if [ ! -x "$tool" ]; then
    curl -fsSL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$tool"
fi
ARCH=x86_64 "$tool" --appimage-extract-and-run "$app" "$out/Beamer-${version}-x86_64.AppImage"

ls -lh "$out"/*.deb "$out"/*.AppImage
