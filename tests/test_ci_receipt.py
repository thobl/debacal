"""CI receipt diagnostics follow job-log redirects without leaking API credentials."""
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/collect_ci_receipt.py'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not included in the source archive')


def collector():
    spec = importlib.util.spec_from_file_location('debacal_ci_receipt', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Opener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)


@pytest.fixture
def environment():
    return {'RELEASE_RESULT': 'skipped', 'CHECK_RESULT': 'failure',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': 'abc123',
            'GITHUB_REPOSITORY': 'example/debacal', 'GITHUB_RUN_ID': '123',
            'GH_TOKEN': 'private-api-token', 'GITHUB_WORKSPACE': '/home/runner/work/debacal',
            'RUNNER_TEMP': '/tmp/private-runner', 'RUNNER_TOOL_CACHE': '/opt/private-tools'}


def receipt(tmp_path, **values):
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(values))
    return path


def test_job_snapshot_and_redirected_failure_logs_keep_credentials_on_api_only(tmp_path, environment):
    module = collector()
    location = 'https://storage.example/log?signature=private-signature'
    snapshot = {'total_count': 2, 'jobs': [
        {'id': 10, 'name': 'test (3.11)', 'conclusion': 'failure',
         'steps': [{'name': 'pytest', 'conclusion': 'failure'}]},
        {'id': 11, 'name': 'test (3.14)', 'conclusion': 'success'}]}
    api = Opener(json.dumps(snapshot).encode(), HTTPError('https://api.github.com/logs', 302,
                 'Found', {'Location': location}, None))
    storage = Opener((f"/home/runner/work/debacal/tests: failure\n{environment['GH_TOKEN']}\n"
                      f"{location}\nnext line").encode())
    path = receipt(tmp_path, tagName='v0.1.0')
    module.update_receipt(path, environment, api=api, storage=storage)
    result = json.loads(path.read_text())
    assert result['tagName'] == 'v0.1.0'
    assert result['jobs'] == snapshot
    assert result['checks'] == 'failure' and result['result'] == 'skipped'
    assert result['reference'] == 'refs/heads/main'
    assert result['workflow_url'] == 'https://github.com/example/debacal/actions/runs/123'
    assert '<checkout>/tests: failure' in result['failed_checks']
    assert 'next line' in result['failed_checks']
    assert 'private-api-token' not in path.read_text() and location not in path.read_text()
    assert api.requests[0].full_url.endswith('/jobs?filter=all&per_page=100')
    assert all(request.get_header('Authorization') == 'Bearer private-api-token' for request in api.requests)
    assert len(storage.requests) == 1 and storage.requests[0].full_url == location
    assert storage.requests[0].get_header('Authorization') is None
    assert not storage.requests[0].headers


def test_failed_download_retains_other_logs_and_safe_diagnostic(tmp_path, environment):
    module = collector()
    location = 'https://storage.example/log?signature=secret'
    snapshot = {'total_count': 2, 'jobs': [
        {'id': 10, 'name': 'first', 'conclusion': 'failure'},
        {'id': 11, 'name': 'second', 'conclusion': 'failure'}]}
    api = Opener(json.dumps(snapshot).encode(), HTTPError('api', 302, 'Found', {'Location': location}, None),
                 b'second job traceback')
    storage = Opener(URLError(location + ' private-api-token'))
    path = receipt(tmp_path)
    module.update_receipt(path, environment, api=api, storage=storage)
    result = json.loads(path.read_text())
    assert result['jobs'] == snapshot
    assert 'second job traceback' in result['failed_checks']
    assert result['diagnostic_errors'] == ['Could not retrieve logs for job 10 (URLError).']
    assert 'secret' not in path.read_text() and 'private-api-token' not in path.read_text()


def test_jobs_api_error_still_records_result_without_exception_url_or_token(tmp_path, environment):
    module = collector()
    api = Opener(HTTPError('https://secret.example/?token=private-api-token', 403,
                           'Forbidden', {}, None))
    storage = Opener()
    path = receipt(tmp_path)
    module.update_receipt(path, environment, api=api, storage=storage)
    result = json.loads(path.read_text())
    assert result['checks'] == 'failure'
    assert result['diagnostic_errors'] == ['Could not retrieve jobs (HTTP 403).']
    assert not storage.requests
    assert 'secret.example' not in path.read_text() and 'private-api-token' not in path.read_text()


@pytest.mark.parametrize('location', ['http://storage.example/log', 'https://user:pass@storage.example/log'])
def test_invalid_storage_location_is_not_requested_or_persisted(tmp_path, environment, location):
    module = collector()
    api = Opener(b'{"jobs": [{"id": 10, "conclusion": "failure"}]}',
                 HTTPError('api', 302, 'Found', {'Location': location}, None))
    storage = Opener()
    path = receipt(tmp_path)
    module.update_receipt(path, environment, api=api, storage=storage)
    result = json.loads(path.read_text())
    assert result['diagnostic_errors'] == ['Could not retrieve logs for job 10 (ValueError).']
    assert not storage.requests and location not in path.read_text()
