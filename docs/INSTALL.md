# Installation

Debacal targets Linux desktops and requires Python 3.11 or newer. Releases
provide an Arch build recipe, a Python wheel, a source archive, and SHA-256 sums.
There is no PyPI or AUR publication yet.

On Python 3.11 the dependency metadata excludes PySide6 6.12.0, whose
[upstream reference-counting bug](https://qt-project.atlassian.net/browse/PYSIDE-3474)
can abort ordinary GUI calls. Pip and uv select a compatible release
automatically; Python 3.12 and newer keep their normal Qt version range.

## Arch Linux

From a checkout, run `makepkg -si` in `packaging/arch`. Alternatively, download
`debacal-0.1.0-arch.tar.gz` from the
[release](https://github.com/thobl/debacal/releases/tag/v0.1.0), extract it,
review `PKGBUILD`, then run `makepkg -si` in the extracted directory.
The recipe downloads and verifies the matching source archive. Missing
dependencies are installed using pacman; the build itself uses system Python
packages. See [the recipe's documentation](../packaging/arch/README.md) for
offline builds and test options.

For a private repository, first download `debacal-0.1.0.tar.gz` from the release
page while signed in and place it beside `PKGBUILD`. Makepkg cannot use your
browser login or SSH authentication to fetch that HTTPS asset. An authenticated
GitHub CLI download is another option; see the recipe documentation above.

## Other Linux distributions

Install pipx using your distribution's package manager, then:

```sh
pipx install https://github.com/thobl/debacal/releases/download/v0.1.0/debacal-0.1.0-py3-none-any.whl
debacal
```

`uv tool install` can be used instead of `pipx install`. Both keep dependencies
separate from system Python. These commands download the Python dependencies;
they do not install the app's files system-wide. Qt also needs your desktop's
OpenGL/EGL libraries. If Qt reports a missing `xcb-cursor` library on X11,
install your distribution's libxcb-cursor package (`libxcb-cursor0` on Debian
and Ubuntu).

For a desktop launcher with a pipx/source installation, use the two files in
`packaging/linux` in a checkout or extracted source archive:

```sh
install -Dm644 packaging/linux/debacal.desktop "${XDG_DATA_HOME:-$HOME/.local/share}/applications/debacal.desktop"
install -Dm644 packaging/linux/debacal.svg "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/debacal.svg"
```

Make sure `debacal` is available in your desktop session's `PATH` (usually
`~/.local/bin`; pipx's `ensurepath` command helps). To make it the default ICS
handler, optionally run `xdg-mime default debacal.desktop text/calendar`.
Arch packages install the launcher automatically and retain your existing
default calendar application.

A source-only launch such as `uv run debacal --test` may print a Qt portal
warning that app info for `debacal` was not found. The app still runs; the
desktop portal cannot identify it until the launcher above is installed and
its `debacal` command is available to the desktop session. This warning is
separate from Python exceptions or calendar synchronization errors.

## From source

```sh
git clone https://github.com/thobl/debacal.git
cd debacal
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/debacal --demo
```

For development, install `-e '.[dev]'` instead. Run tests with
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest`. Local-socket tests need
an environment that permits local IPC; no personal calendar account is needed.

## Verify and remove

Download `SHA256SUMS` alongside the release files and run `sha256sum --check
--ignore-missing SHA256SUMS` in that directory. For Arch, makepkg also checks the
source hash embedded in the recipe.

Use `pacman -R debacal`, `pipx uninstall debacal`, or remove the source virtual
environment, according to the installation method. User profiles are preserved;
see [data and backups](USER_GUIDE.md) before deleting them.
