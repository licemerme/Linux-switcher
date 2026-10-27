#!/bin/sh
# Build the noarch RPM into temp/rpmbuild/RPMS/noarch/.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/temp/rpmbuild"
VERSION=0.3.0

cd "$ROOT"
rm -rf "$OUT"
mkdir -p "$OUT/SOURCES" "$OUT/SPECS" "$OUT/BUILD" "$OUT/RPMS" "$OUT/SRPMS"

tar czf "$OUT/SOURCES/switcher-$VERSION.tar.gz" \
    --transform "s,^,switcher-$VERSION/," \
    switcher packaging

cp packaging/switcher.spec "$OUT/SPECS/"

rpmbuild --define "_topdir $OUT" -bb "$OUT/SPECS/switcher.spec"

echo
echo "Built:"
ls -l "$OUT/RPMS/noarch/"
