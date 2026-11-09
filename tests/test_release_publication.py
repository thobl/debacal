"""Publication only exposes a complete set of freshly uploaded release assets."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish_release.sh'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not included in the source archive')


def publication(tmp_path, *, exists=True, fail=None, assets=True):
    notes = tmp_path / 'docs/releases/v0.1.0.md'
    notes.parent.mkdir(parents=True)
    notes.write_text('Release notes\n')
    if assets:
        dist = tmp_path / 'dist'
        dist.mkdir()
        for name in ('SHA256SUMS', 'debacal-0.1.0.tar.gz'):
            (dist / name).write_text('Synthetic asset\n')
    commands = tmp_path / 'commands.jsonl'
    gh = tmp_path / 'gh'
    gh.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['release', 'view']:
    sys.exit(0 if os.environ['RELEASE_EXISTS'] == '1' else 1)
sys.exit(1 if sys.argv[2] == os.environ.get('FAIL_ACTION') else 0)
''')
    gh.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               RELEASE_TAG='v0.1.0', RELEASE_EXISTS='1' if exists else '0',
               COMMAND_LOG=str(commands), FAIL_ACTION=fail or '')
    result = subprocess.run(['bash', str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in commands.read_text().splitlines()] if commands.exists() else []
    return result, calls


def test_existing_release_is_updated_in_place_and_published_after_upload(tmp_path):
    result, calls = publication(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == [
        ['release', 'view', 'v0.1.0'],
        ['release', 'edit', 'v0.1.0', '--draft=true'],
        ['release', 'upload', 'v0.1.0', 'dist/SHA256SUMS', 'dist/debacal-0.1.0.tar.gz', '--clobber'],
        ['release', 'edit', 'v0.1.0', '--draft=false', '--latest', '--notes-file', 'docs/releases/v0.1.0.md'],
    ]


def test_new_release_stays_draft_until_its_initial_upload_succeeds(tmp_path):
    result, calls = publication(tmp_path, exists=False)
    assert result.returncode == 0, result.stderr
    create = calls[1]
    assert create[:3] == ['release', 'create', 'v0.1.0']
    assert '--verify-tag' in create and '--draft' in create
    assert calls[-1][3] == '--draft=false'


@pytest.mark.parametrize('exists,fail', [(True, 'upload'), (True, 'edit'), (False, 'create')])
def test_failed_preparation_or_upload_does_not_publish(tmp_path, exists, fail):
    result, calls = publication(tmp_path, exists=exists, fail=fail)
    assert result.returncode != 0
    assert not any('--draft=false' in call for call in calls)


def test_missing_assets_are_rejected_before_changing_a_release(tmp_path):
    result, calls = publication(tmp_path, assets=False)
    assert result.returncode != 0
    assert not calls
