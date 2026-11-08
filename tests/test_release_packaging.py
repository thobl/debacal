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
