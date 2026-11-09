#!/usr/bin/env bash
# Create or refresh a release without publishing a partial set of assets.
set -euo pipefail
: "${RELEASE_TAG:?Set RELEASE_TAG to the version tag being released}"
notes="docs/releases/${RELEASE_TAG}.md"
test -f "$notes"
shopt -s nullglob
assets=(dist/*)
test "${#assets[@]}" -gt 0

if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
    # Refreshing a private pre-publication release retains its identity and
    # version. A failed replacement leaves it draft, ready for a safe retry.
    gh release edit "$RELEASE_TAG" --draft=true
    gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber
else
    gh release create "$RELEASE_TAG" "${assets[@]}" --verify-tag --draft \
        --title "debacal ${RELEASE_TAG#v}" --notes-file "$notes"
fi
gh release edit "$RELEASE_TAG" --draft=false --latest --notes-file "$notes"
