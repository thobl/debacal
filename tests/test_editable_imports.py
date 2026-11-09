"""Console entry points must import the checkout ahead of a global install.

A Python -c probe includes its current directory and can hide this failure.
Build an actual editable wheel using the project's package-layout settings,
then run a console-style script outside the checkout with a competing package.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tomllib
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def package_wheels(tmp_path_factory):
    directory = tmp_path_factory.mktemp('editable-import')
    source, wheels = directory / 'checkout', directory / 'wheels'
    source.mkdir()
    wheels.mkdir()
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['setuptools']
    lines = ['[build-system]', 'requires = ["setuptools>=77"]',
             'build-backend = "setuptools.build_meta"', '[project]',
             'name = "debacal"', 'version = "0.0.0"']
    if config.get('package-dir'):
        lines.append('[tool.setuptools.package-dir]')
        lines.extend(json.dumps(key) + ' = ' + json.dumps(value)
                     for key, value in config['package-dir'].items())
    lines.append('[tool.setuptools.packages.find]')
    for key, value in config['packages']['find'].items():
        lines.append(key + ' = ' + json.dumps(value))
    (source / 'pyproject.toml').write_text('\n'.join(lines) + '\n')
    package = source / 'debacal'
    package.mkdir()
    (package / '__init__.py').write_text('ORIGIN = "checkout"\n')
    (package / 'nested').mkdir()
    (package / 'nested' / '__init__.py').write_text('ORIGIN = "checkout child"\n')
    # A flat editable root must not broaden the ordinary wheel's contents.
    (source / 'local-notes.txt').write_text('Synthetic local notes\n')
    (source / 'local-calendar.ics').write_text('Synthetic local calendar\n')
    (source / 'unrelated.py').write_text('ORIGIN = "unrelated"\n')
    (package / 'cli.py').write_text(
        'import json\nfrom pathlib import Path\nimport debacal\n'
        'from debacal import nested\n'
        'def main():\n'
        '    print(json.dumps({"origin": debacal.ORIGIN, "child": nested.ORIGIN, '
        '"module": str(Path(debacal.__file__).resolve())}))\n')
    for hook in ('build_editable', 'build_wheel'):
        completed = subprocess.run(
            [sys.executable, '-c',
             f'from setuptools.build_meta import {hook}; import sys; {hook}(sys.argv[1])', str(wheels)],
            cwd=source, text=True, capture_output=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    editable, = wheels.glob('*editable*.whl')
    ordinary, = [path for path in wheels.glob('*.whl') if 'editable' not in path.name]
    return directory, source, editable, ordinary


def test_console_import_prefers_editable_checkout_over_later_global_package(package_wheels):
    directory, source, wheel, _ordinary = package_wheels
    local_site, global_site, scripts, working = [directory / name for name in ('local-site', 'global-site', 'bin', 'outside')]
    for path in (local_site, global_site, scripts, working):
        path.mkdir()
    with ZipFile(wheel) as archive:
        archive.extractall(local_site)
    package = global_site / 'debacal'
    package.mkdir()
    (package / '__init__.py').write_text('ORIGIN = "global"\n')
    (package / 'nested').mkdir()
    (package / 'nested' / '__init__.py').write_text('ORIGIN = "global child"\n')
    (package / 'cli.py').write_text((source / 'debacal' / 'cli.py').read_text())
    script = scripts / 'debacal'
    # This is Python's local-venv-before-system-site ordering, with the cwd
    # deliberately absent from its import path, as for installed entry points.
    script.write_text('import site, sys\n'
                      'site.addsitedir(sys.argv[1])\nsite.addsitedir(sys.argv[2])\n'
                      'from debacal.cli import main\nmain()\n')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-S', str(script), str(local_site), str(global_site)],
                            cwd=working, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout)
    assert details['origin'] == 'checkout'
    assert details['child'] == 'checkout child'
    assert Path(details['module']) == source / 'debacal' / '__init__.py'


def test_regular_wheel_still_contains_only_the_selected_application_package(package_wheels):
    _directory, _source, _editable, ordinary = package_wheels
    with ZipFile(ordinary) as archive:
        entries = set(archive.namelist())
    assert {'debacal/__init__.py', 'debacal/cli.py', 'debacal/nested/__init__.py'} <= entries
    assert not {'local-notes.txt', 'local-calendar.ics', 'unrelated.py'} & entries
    assert not any(name.endswith('.pth') or '__editable__' in name for name in entries)
