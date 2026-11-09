"""Release archives are reproducible, private-path-free, and safe to unpack."""
import gzip
import importlib.util
from pathlib import Path
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_release.py'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not included in the source archive')


def builder():
    spec = importlib.util.spec_from_file_location('debacal_build_release', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_order_and_metadata_do_not_depend_on_the_build_machine(tmp_path):
    module = builder()
    one, two = tmp_path / 'one.tar.gz', tmp_path / 'two.tar.gz'
    files = [('debacal-0.1.0/LICENSE', b'license'), ('debacal-0.1.0/debacal/app.py', b'print(1)\n')]
    module.canonical_archive(one, files)
    module.canonical_archive(two, list(reversed(files)))
    assert one.read_bytes() == two.read_bytes()
    assert gzip.decompress(one.read_bytes())
    with tarfile.open(one) as archive:
        assert archive.getnames() == sorted(name for name, _ in files)
        for member in archive:
            assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, '', '', 0)
            assert member.mode == 0o644 and member.isfile()
            assert archive.extractfile(member).read() == dict(files)[member.name]


@pytest.mark.parametrize('path', ['/etc/passwd', '../calendar.ics', 'source/../../outside'])
def test_archive_rejects_paths_outside_its_root(tmp_path, path):
    with pytest.raises(ValueError, match='Unsafe archive path'):
        builder().canonical_archive(tmp_path / 'bad.tar.gz', [(path, b'no')])


def test_release_tag_must_match_package_version_before_building(tmp_path):
    with pytest.raises(ValueError, match='tag must match'):
        builder().build(tmp_path, tag='v999.0.0')
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    files = {
        'pyproject.toml': b'[project]\nname = "debacal"\nversion = "0.1.0"\n',
        'MANIFEST.in': b'include LICENSE\n', 'README.md': b'Debacal\n', 'LICENSE': b'0BSD\n',
        'debacal/__init__.py': b'', 'tests/test_example.py': b'assert True\n',
        'packaging/linux/debacal.desktop': b'[Desktop Entry]\n',
        'packaging/linux/debacal.svg': b'<svg/>\n',
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    module = builder()
    monkeypatch.setattr(module, 'ROOT', root)
    return module, root, files


def test_checkout_staging_copies_package_inputs_without_local_data_or_caches(checkout, tmp_path):
    module, root, files = checkout
    for name in ('.env', 'profile.sqlite3', 'private.ics', '.venv/local.py', 'docs/local-notes.md',
                 'debacal/__pycache__/local.pyc', 'tests/local.pyc'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Local-only synthetic data')
    destination = tmp_path / 'staged'
    module.stage_sources(destination, tag='v0.1.0')
    staged = {path.relative_to(destination).as_posix(): path.read_bytes()
              for path in destination.rglob('*') if path.is_file()}
    assert staged == files
    assert all(path.stat().st_mtime == module.EPOCH for path in destination.rglob('*'))


def test_checkout_version_mismatch_does_not_create_a_staging_directory(checkout, tmp_path):
    module, _, _ = checkout
    destination = tmp_path / 'staged'
    with pytest.raises(ValueError, match='tag must match'):
        module.stage_sources(destination, tag='v9.0.0')
    assert not destination.exists()


def test_checkout_staging_does_not_merge_stale_or_unrelated_files(checkout, tmp_path):
    module, _, _ = checkout
    destination = tmp_path / 'existing'
    destination.mkdir()
    marker = destination / 'keep.txt'
    marker.write_bytes(b'Keep existing files')
    with pytest.raises(FileExistsError):
        module.stage_sources(destination)
    assert list(destination.iterdir()) == [marker]
    assert marker.read_bytes() == b'Keep existing files'


@pytest.mark.parametrize('relative', ['', 'debacal/nested', 'tests/nested', 'packaging/linux/nested'])
def test_checkout_staging_rejects_self_copying(checkout, relative):
    module, root, _ = checkout
    with pytest.raises(ValueError, match='outside the source inputs'):
        module.stage_sources(root / relative)
