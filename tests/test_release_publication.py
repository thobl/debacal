"""Publication completes drafts while preserving already published assets."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish_release.sh'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not included in the source archive')
ASSETS = ('debacal-0.1.0.tar.gz', 'debacal-0.1.0-py3-none-any.whl',
          'debacal-0.1.0-arch.tar.gz', 'SHA256SUMS')


def publication(tmp_path, *, exists=True, draft=True, fail=None, assets=True, missing_asset=None, extra_assets=()):
    notes = tmp_path / 'docs/releases/v0.1.0.md'
    notes.parent.mkdir(parents=True)
    notes.write_text('Release notes\n')
    if assets:
        dist = tmp_path / 'dist'
        dist.mkdir()
        for name in (*ASSETS, *extra_assets):
            if name == missing_asset:
                continue
            (dist / name).write_text('Synthetic asset\n')
    commands = tmp_path / 'commands.jsonl'
    gh = tmp_path / 'gh'
    gh.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['release', 'view']:
    if os.environ['RELEASE_EXISTS'] != '1':
        sys.exit(1)
    print(os.environ['RELEASE_DRAFT'])
    sys.exit(0)
sys.exit(1 if sys.argv[2] == os.environ.get('FAIL_ACTION') else 0)
''')
    gh.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               RELEASE_TAG='v0.1.0', RELEASE_EXISTS='1' if exists else '0',
               RELEASE_DRAFT='true' if draft else 'false',
               COMMAND_LOG=str(commands), FAIL_ACTION=fail or '')
    result = subprocess.run(['bash', str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in commands.read_text().splitlines()] if commands.exists() else []
    return result, calls


def test_existing_draft_is_retried_and_published_after_upload(tmp_path):
    result, calls = publication(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == [
        ['release', 'view', 'v0.1.0', '--json', 'isDraft', '--jq', '.isDraft'],
        ['release', 'upload', 'v0.1.0', *['dist/' + name for name in ASSETS], '--clobber'],
        ['release', 'edit', 'v0.1.0', '--draft=false', '--latest', '--notes-file', 'docs/releases/v0.1.0.md'],
    ]


def test_published_release_cannot_be_hidden_or_have_its_assets_replaced(tmp_path):
    result, calls = publication(tmp_path, draft=False)
    assert result.returncode != 0
    assert 'already published' in result.stderr and 'new version' in result.stderr
    assert calls == [['release', 'view', 'v0.1.0', '--json', 'isDraft', '--jq', '.isDraft']]


def test_new_release_stays_draft_until_its_initial_upload_succeeds(tmp_path):
    result, calls = publication(tmp_path, exists=False)
    assert result.returncode == 0, result.stderr
    create = calls[1]
    assert create[:3] == ['release', 'create', 'v0.1.0']
    assert '--verify-tag' in create and '--draft' in create
    assert calls[-1][3] == '--draft=false'


@pytest.mark.parametrize('exists,fail', [(True, 'upload'), (False, 'create')])
def test_failed_preparation_or_upload_does_not_publish(tmp_path, exists, fail):
    result, calls = publication(tmp_path, exists=exists, fail=fail)
    assert result.returncode != 0
    assert not any('--draft=false' in call for call in calls)


def test_failed_final_publication_is_reported_as_a_failure(tmp_path):
    result, calls = publication(tmp_path, fail='edit')
    assert result.returncode != 0
    assert calls[-2][1] == 'upload'
    assert calls[-1][1] == 'edit' and '--draft=false' in calls[-1]


def test_missing_assets_are_rejected_before_changing_a_release(tmp_path):
    result, calls = publication(tmp_path, assets=False)
    assert result.returncode != 0
    assert not calls


@pytest.mark.parametrize('missing_asset', ASSETS)
def test_each_required_asset_must_exist_before_contacting_github(tmp_path, missing_asset):
    result, calls = publication(tmp_path, missing_asset=missing_asset)
    assert result.returncode != 0
    assert 'Missing release asset: dist/' + missing_asset in result.stderr
    assert not calls


@pytest.mark.parametrize('exists', [False, True])
def test_only_current_release_assets_are_uploaded(tmp_path, exists):
    extra = ('debacal-0.0.9.tar.gz', 'debacal-0.0.9-py3-none-any.whl',
             'debacal-0.0.9-arch.tar.gz', 'local-build.log')
    result, calls = publication(tmp_path, exists=exists, extra_assets=extra)
    assert result.returncode == 0, result.stderr
    upload = next(call for call in calls if call[1] in ('create', 'upload'))
    files = [argument for argument in upload if argument.startswith('dist/')]
    assert files == ['dist/' + name for name in ASSETS]
