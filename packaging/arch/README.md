# Arch Linux

This recipe builds the published Debacal source release using Arch's Python and
Qt packages. It does not run pip or download Python dependencies during the
build. All runtime dependencies are available in Arch's official repositories.

Download the `debacal-0.1.0-arch.tar.gz` recipe asset from the
[release](https://github.com/thobl/debacal/releases/tag/v0.1.0), extract it, review
`PKGBUILD`, and run these commands in that directory:

```sh
makepkg -si
debacal
```

Alternatively, use `packaging/arch` from the matching Git tag. `makepkg -s`
installs missing build/runtime dependencies with pacman; `-i` installs the built
package. The desktop launcher and `text/calendar` file association are installed
alongside the command. No existing calendar application is replaced as the
default. To choose Debacal explicitly:

```sh
xdg-mime default debacal.desktop text/calendar
```

Secure password storage needs an unlocked Secret Service provider (such as
GNOME Keyring or KeePassXC), or KWallet with `python-dbus`. These are optional;
without one, Debacal can use account passwords for the current session.

The default package check is a short, offline smoke test: it validates the
desktop entry, checks command-line help, and opens a disposable demonstration
calendar with Qt's offscreen backend to verify a workspace screenshot. It does
not run the full development test suite or require pytest. Test data and runtime
files are isolated under makepkg's source tree.

Maintainers can additionally run the full test suite, including local IPC tests,
on a host that permits local sockets. Install `python-pytest` version 9 or newer
first, then enable it explicitly:

```sh
sudo pacman -S python-pytest
DEBACAL_FULL_CHECK=1 makepkg -s
```

Use `makepkg -si --nocheck` to skip package checks.

For an offline build, put the matching `debacal-0.1.0.tar.gz` source asset beside
`PKGBUILD`; makepkg will verify and use it without downloading it again. The
source archive intentionally excludes this directory, so its checksum does not
depend on a recipe containing that same checksum. The release recipe records
the archive's SHA-256 digest.

Maintainers can refresh package metadata after changing the recipe with:

```sh
makepkg --printsrcinfo > .SRCINFO
```

The recipe is maintained in this repository. It has not been submitted to the
AUR or added to an Arch binary repository.
