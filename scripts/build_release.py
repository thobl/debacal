"""Build reproducible source/wheel assets and a checksum-pinned Arch recipe.

Run with the release toolchain: build 1.6.1 and setuptools 84.2.1.
The source archive excludes its Arch recipe to avoid a self-referential hash.
Only declared source inputs enter the staging tree; user profiles and local
exports never enter it. No account or network access is required.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
EPOCH = 315532800  # ZIP's first supported date: 1 January 1980.


def canonical_archive(destination, files):
    """Write regular files in a portable order without local paths/ownership."""
    with Path(destination).open('wb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, data in sorted(files):
                    path = PurePosixPath(name)
                    if path.is_absolute() or '..' in path.parts or not path.parts:
                        raise ValueError('Unsafe archive path: ' + name)
                    info = tarfile.TarInfo(str(path))
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    archive.addfile(info, io.BytesIO(data))


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run_build(arguments, env):
    result = subprocess.run([sys.executable, '-m', 'build', *arguments], env=env,
                            capture_output=True, text=True)
    if result.returncode:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args)


def project_version(tag=None):
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', version):
        raise ValueError('Use a release version such as 0.1.0 in pyproject.toml.')
    if tag is not None and tag != 'v' + version:
        raise ValueError('The Git tag must match the project version: v' + version)
    return version


def stage_sources(destination, *, tag=None):
    """Copy the declared package inputs into a fresh, data-free source tree."""
    project_version(tag)
    stage = Path(destination).resolve()
    trees = ('debacal', 'tests', 'packaging/linux')
    if stage == ROOT or any(stage.is_relative_to(ROOT / name) for name in trees):
        raise ValueError('The staging directory must be outside the source inputs.')
    stage.mkdir(parents=True)
    for name in ('pyproject.toml', 'MANIFEST.in', 'README.md', 'LICENSE'):
        shutil.copyfile(ROOT / name, stage / name)
    for name in trees:
        shutil.copytree(ROOT / name, stage / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    for path in stage.rglob('*'):
        os.utime(path, (EPOCH, EPOCH))


def build(outdir, *, source_only=False, tag=None):
    version = project_version(tag)
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    assets = []
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH), PYTHONHASHSEED='0')
    with tempfile.TemporaryDirectory(prefix='debacal-release-') as temporary:
        work = Path(temporary)
        stage = work / 'source'
        stage_sources(stage, tag='v' + version)
        raw = work / 'raw'
        run_build(['--sdist', '--no-isolation', '--outdir', str(raw), str(stage)], env)
        source = outdir / f'debacal-{version}.tar.gz'
        with tarfile.open(raw / source.name, 'r:gz') as archive:
            files = [(member.name, archive.extractfile(member).read())
                     for member in archive if member.isfile()]
        canonical_archive(source, files)
        assets.append(source)
        if not source_only:
            recipe = ROOT / 'packaging/arch/PKGBUILD'
            expected = re.search(r"sha256sums=\('([a-f0-9]{64})'\)", recipe.read_text())
            if expected is None or expected[1] != digest(source):
                raise ValueError('Update the Arch source SHA-256 before releasing: ' + digest(source))
            extracted = work / 'unpacked'
            for name, data in files:
                relative = PurePosixPath(name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe source archive path.')
                target = extracted.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.utime(target, (EPOCH, EPOCH))
            run_build(['--wheel', '--no-isolation', '--outdir', str(outdir),
                       str(extracted / f'debacal-{version}')], env)
            assets.append(outdir / f'debacal-{version}-py3-none-any.whl')
            arch = outdir / f'debacal-{version}-arch.tar.gz'
            canonical_archive(arch, [(f'debacal-{version}-arch/{name}',
                (ROOT / 'packaging/arch' / name).read_bytes())
                for name in ('PKGBUILD', '.SRCINFO', '.gitignore', 'README.md')])
            assets.append(arch)
    (outdir / 'SHA256SUMS').write_text(''.join(f'{digest(path)}  {path.name}\n' for path in sorted(assets)))
    for path in assets:
        print(f'{digest(path)}  {path.name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outdir', type=Path, default=ROOT / 'dist')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--source-only', action='store_true', help='Build the source first to update its recipe hash')
    mode.add_argument('--stage-only', type=Path, help='Stage checkout inputs in a fresh directory without building an archive')
    parser.add_argument('--tag', help='Verify that the release tag matches the package version')
    args = parser.parse_args()
    if args.stage_only is not None:
        stage_sources(args.stage_only, tag=args.tag)
    else:
        build(args.outdir, source_only=args.source_only, tag=args.tag)


if __name__ == '__main__':
    main()
