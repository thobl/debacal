# Maintainer release checklist

Releases remain private while the GitHub repository is private. Changing its
visibility is a separate owner decision; the workflow does not change it.

1. Update the project/Arch versions, README download URL, and release notes in
   `docs/releases/vVERSION.md`.
2. Review tracked files, full history, and author identities for private data.
   Never add profiles, credentials, local ICS exports, or personal notes.
3. Use Python 3.14 with `build==1.6.1` and `setuptools==84.2.1` for the release
   toolchain. Run `python scripts/build_release.py --source-only`, copy the
   source SHA-256 into `packaging/arch/PKGBUILD`, and regenerate `.SRCINFO`
   with `makepkg --printsrcinfo` in that directory.
4. Run `python scripts/build_release.py`. It verifies the source hash, builds
   the wheel from the released source, bundles the Arch recipe, and writes
   `dist/SHA256SUMS`. Check an extracted-source makepkg build and a clean wheel
   install. Run the test suite.
5. Commit, create `vVERSION`, and push the branch and tag. The pinned GitHub
   Actions workflow tests Python 3.11 and 3.14 before creating the release with
   all four assets. It uses the repository's own short-lived token; no PyPI or
   personal access token is needed. GitHub Actions must be enabled.

The source archive deliberately excludes the Arch recipe, screenshots, local
data, and development galleries. Excluding the recipe avoids a self-referential
archive checksum. File order, ownership and timestamps are normalized; the
source archive and wheel can be rebuilt with the documented toolchain.

To inspect publication from an SSH-authenticated checkout without an API token:

```sh
git fetch origin refs/notes/debacal-releases:refs/notes/debacal-releases
git notes --ref=debacal-releases show 'v0.1.0^{commit}'
```

That receipt records the release URL, published state and assets after a
successful publication. It is an ordinary Git note, not application data.
Never move a published version tag or replace published release files; fix
problems in a new version. A failed draft can be completed by rerunning the
workflow. The workflow refuses to overwrite an already published release.
