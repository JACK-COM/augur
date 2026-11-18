#!/usr/bin/env bash
# Release Augur: test, stamp the version, tag, push, and move the Homebrew
# formula to the new tarball. Run from anywhere:  scripts/release.sh 0.1.3
#
# The tap is a sibling checkout (../homebrew-panoply) unless PANOPLY_TAP names it.
# Every step stops the release on failure; a tarball that cannot be fetched, or a
# hash that is not 64 hex characters, never reaches the formula.
# A failure after the tag is pushed leaves the release tagged and the formula
# unmoved: fix the cause, then set the formula's url and sha256 by hand.
set -euo pipefail

version="${1:-}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 X.Y.Z" >&2; exit 2; }

repo="$(cd "$(dirname "$0")/.." && pwd)"
tap="${PANOPLY_TAP:-$repo/../homebrew-panoply}"
formula="$tap/Formula/augur.rb"
src="$repo/src/augur"
[[ -f "$formula" ]] || { echo "no formula at $formula; set PANOPLY_TAP" >&2; exit 1; }

for r in "$repo" "$tap"; do
  [[ -z "$(git -C "$r" status --porcelain)" ]] || { echo "$r has uncommitted changes" >&2; exit 1; }
  [[ "$(git -C "$r" branch --show-current)" == main ]] || { echo "$r is not on main" >&2; exit 1; }
  git -C "$r" pull -q --ff-only
done
git -C "$repo" rev-parse -q --verify "refs/tags/v$version" >/dev/null && { echo "v$version is already tagged" >&2; exit 1; }

# A module past this many lines is a component that has not been named yet; split it
# before releasing. Every check runs before the stamp, so a refused release leaves the tree clean.
ceiling=800
over="$(find "$src" -name "*.py" -not -path "*/__pycache__/*" -exec wc -l {} + | awk -v c="$ceiling" '$2 != "total" && $1 > c {print "  " $2 ": " $1}')"
[[ -z "$over" ]] || { printf 'modules over %s lines:\n%s\n' "$ceiling" "$over" >&2; exit 1; }

# the panoply-lib copies, tests/ and the install smoke check
make -C "$repo" test

sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$version\"/" "$src/augur.py" && rm "$src/augur.py.bak"
grep -q "^__version__ = \"$version\"" "$src/augur.py" || { echo "could not stamp the version" >&2; exit 1; }

git -C "$repo" commit -qam "$version"
git -C "$repo" tag -a "v$version" -m "$version"
git -C "$repo" push -q origin main "v$version"

# AUGUR_RELEASE_BASE lets a dry run fetch from a local directory instead of GitHub
url="${AUGUR_RELEASE_BASE:-https://github.com/JACK-COM/augur/archive/refs/tags}/v$version.tar.gz"
tarball="$(mktemp)"; trap 'rm -f "$tarball"' EXIT
curl -fsSL -o "$tarball" "$url"
sha="$(shasum -a 256 "$tarball" | cut -d' ' -f1)"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || { echo "bad sha256 for $url" >&2; exit 1; }

sed -i.bak -E "s|^  url \".*\"|  url \"$url\"|; s|^  sha256 \".*\"|  sha256 \"$sha\"|" "$formula" && rm "$formula.bak"
grep -q "v$version.tar.gz" "$formula" && grep -q "$sha" "$formula" || { echo "formula not updated" >&2; exit 1; }
git -C "$tap" commit -qam "feat: updates Augur to $version

- Points the formula at the v$version tarball"
git -C "$tap" push -q origin main

echo "released $version ($sha)"
echo "upgrade:  brew update && brew upgrade jack-com/panoply/augur"
# Names each guide page this release moved past; it warns and never blocks
[[ -x "$tap/scripts/guides-behind.sh" ]] && "$tap/scripts/guides-behind.sh" augur "$version" || true
