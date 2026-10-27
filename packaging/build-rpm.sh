#!/bin/sh
# Build the noarch RPM into temp/rpmbuild/RPMS/noarch/.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/temp/rpmbuild"
VERSION=1.1.1

cd "$ROOT"
rm -rf "$OUT"
# Every scratch dir stays inside temp/: the system /var/tmp may be read-only
# (sandbox, or a box with a separate small /var), and rpmbuild dies on it.
mkdir -p "$OUT/SOURCES" "$OUT/SPECS" "$OUT/BUILD" "$OUT/RPMS" "$OUT/SRPMS" "$OUT/tmp"

tar czf "$OUT/SOURCES/switcher-$VERSION.tar.gz" \
    --transform "s,^,switcher-$VERSION/," \
    switcher packaging

cp packaging/switcher.spec "$OUT/SPECS/"

rpmbuild --define "_topdir $OUT" --define "_tmppath $OUT/tmp" \
         -bb "$OUT/SPECS/switcher.spec"

echo
echo "Built:"
ls -l "$OUT/RPMS/noarch/"
