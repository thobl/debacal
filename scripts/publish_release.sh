#!/usr/bin/env bash
# Publish a new release or complete a draft without replacing published assets.
set -euo pipefail
: "${RELEASE_TAG:?Set RELEASE_TAG to the version tag being released}"
notes="docs/releases/${RELEASE_TAG}.md"
test -f "$notes"
version="${RELEASE_TAG#v}"
assets=(
    "dist/debacal-${version}.tar.gz"
    "dist/debacal-${version}-py3-none-any.whl"
    "dist/debacal-${version}-arch.tar.gz"
    dist/SHA256SUMS
)
for asset in "${assets[@]}"; do
    if [[ ! -f "$asset" ]]; then
        printf 'Missing release asset: %s\n' "$asset" >&2
        exit 1
    fi
done

if is_draft=$(gh release view "$RELEASE_TAG" --json isDraft --jq .isDraft 2>/dev/null); then
    if [[ "$is_draft" != true ]]; then
        printf 'Release %s is already published; use a new version instead of replacing its assets.\n' "$RELEASE_TAG" >&2
        exit 1
    fi
    # Only unpublished drafts can be retried after an incomplete upload.
    gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber
else
    gh release create "$RELEASE_TAG" "${assets[@]}" --verify-tag --draft \
        --title "debacal ${RELEASE_TAG#v}" --notes-file "$notes"
fi
gh release edit "$RELEASE_TAG" --draft=false --latest --notes-file "$notes"
