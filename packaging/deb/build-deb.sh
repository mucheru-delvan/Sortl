#!/usr/bin/env bash
# Build a .deb for Sortl from the current checkout.
#
#   packaging/deb/build-deb.sh [output-dir]      (default: ./dist)
#
# Needs: meson, ninja-build, dpkg-dev, appstream, desktop-file-utils.
set -euo pipefail

cd "$(dirname "$0")/../.."
OUT="$(realpath -m "${1:-dist}")"

VERSION="$(grep -oP "version: '\K[^']+" meson.build | head -1)"
REVISION=1
PKG=sortl
DEB_VERSION="${VERSION}-${REVISION}"
MAINTAINER="Delvan Mucheru <mkdelvan9@gmail.com>"
HOMEPAGE="https://github.com/mucheru-delvan/Sortl"

BUILD="$(mktemp -d)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$BUILD" "$STAGE"' EXIT

meson setup "$BUILD" --prefix=/usr --buildtype=plain >/dev/null
meson test -C "$BUILD" --print-errorlogs
meson install -C "$BUILD" --destdir "$STAGE" >/dev/null

# Bytecode is not shipped: Debian policy generates it at install time, and
# unprivileged runs can't write into /usr anyway (imports are fast enough).
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/sortl"

DOC="$STAGE/usr/share/doc/$PKG"
install -d "$DOC"

# Copyright file (machine-readable format).
{
  echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
  echo "Upstream-Name: Sortl"
  echo "Source: $HOMEPAGE"
  echo
  echo "Files: *"
  echo "Copyright: 2026 Delvan Mucheru"
  echo "License: MIT"
  echo
  echo "License: MIT"
  sed -n '/^Permission is hereby/,$p' LICENSE | sed 's/^$/./; s/^/ /'
} > "$DOC/copyright"

# Debian changelog.
EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
{
  echo "$PKG ($DEB_VERSION) unstable; urgency=low"
  echo
  echo "  * Release $VERSION."
  echo
  echo " -- $MAINTAINER  $(date -R -d "@$EPOCH")"
} | gzip -9n > "$DOC/changelog.Debian.gz"

mkdir -p "$STAGE/DEBIAN"
SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $DEB_VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.12), python3-gi, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.5)
Recommends: adwaita-icon-theme
Installed-Size: $SIZE
Maintainer: $MAINTAINER
Homepage: $HOMEPAGE
Description: Safely organize a folder into category folders
 Sortl sorts the files in a folder into category folders such as Images,
 Documents and Archives. It shows a preview of every move before anything
 changes, never overwrites existing files, verifies each move and records it
 in a journal that can be inspected afterwards.
CONTROL

# Compile bytecode on install and remove it on removal, so Python's runtime
# __pycache__ files never outlive the package (py3compile/py3clean ship with
# the python3 package, which we depend on).
cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ] && command -v py3compile >/dev/null 2>&1; then
    py3compile -p sortl
fi
exit 0
POSTINST
cat > "$STAGE/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p sortl
fi
exit 0
PRERM
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT/${PKG}_${DEB_VERSION}_all.deb" >/dev/null
echo "Built: $OUT/${PKG}_${DEB_VERSION}_all.deb"
