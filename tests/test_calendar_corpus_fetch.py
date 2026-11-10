"""Immutable corpus fetching is bounded, authenticated only to GitHub, and offline-tested."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'fetch_calendar_corpus.py'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tools are not in the source archive')


@pytest.fixture
def fetcher():
    spec = importlib.util.spec_from_file_location('fetch_calendar_corpus', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(data=b'Synthetic calendar fixture\r\n', *, repository='https://github.com/synthetic/calendar-tests', path='samples/event.ics'):
    sha1 = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data, usedforsecurity=False).hexdigest()
    source = {'project': 'Synthetic', 'repository': repository, 'path': path,
              'git_blob_sha1': sha1, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
              'url': repository.replace('https://github.com/', 'https://api.github.com/repos/') + '/git/blobs/' + sha1}
    blob = {'sha': sha1, 'size': len(data), 'encoding': 'base64', 'content': base64.encodebytes(data).decode()}
    return source, blob


def manifest(tmp_path, sources, **extra):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'schema_version': 1, 'sources': sources, **extra}))
    return path


class Response(io.BytesIO):
    def __init__(self, blob, url):
        super().__init__(json.dumps(blob).encode())
        self.url = url
        self.limits = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        assert size >= 0, 'Never read an unbounded API response'
        self.limits.append(size)
        return super().read(size)


def test_only_immutable_sources_are_downloaded_and_verified_existing_files_need_no_network(fetcher, tmp_path):
    data = b'Synthetic \xc3\xa9vent\r\n'
    source, blob = fixture(data)
    path = manifest(tmp_path, [source], public_feeds=[{'url': 'https://example.invalid/mutable.ics', 'mutable': True}])
    calls, responses = [], []
    def open_request(request, *, timeout):
        calls.append((request, timeout))
        responses.append(Response(blob, request.full_url))
        return responses[-1]
    destination = tmp_path / 'corpus'
    downloaded = fetcher.fetch_corpus(path, destination, opener=open_request, token='synthetic-token')
    assert downloaded == [destination / 'synthetic/calendar-tests/samples/event.ics']
    assert downloaded[0].read_bytes() == data
    request, timeout = calls[0]
    assert request.full_url == source['url']
    assert request.get_header('Authorization') == 'Bearer synthetic-token'
    assert timeout == fetcher.REQUEST_TIMEOUT and len(responses[0].limits) == 1
    assert fetcher.fetch_corpus(path, destination, opener=lambda *a, **kw: pytest.fail('Unexpected network')) == downloaded


@pytest.mark.parametrize('field,value', [
    ('url', 'https://api.github.com.evil.invalid/repos/synthetic/calendar-tests/git/blobs/' + 'a' * 40),
    ('url', 'http://api.github.com/repos/synthetic/calendar-tests/git/blobs/' + 'a' * 40),
    ('url', 'https://token@api.github.com/repos/synthetic/calendar-tests/git/blobs/' + 'a' * 40),
    ('url', 'https://api.github.com/repos/synthetic/calendar-tests/contents/samples/event.ics'),
    ('url', 'https://api.github.com/repos/synthetic/calendar-tests/git/blobs/main'),
    ('repository', 'https://github.com/../calendar-tests'),
    ('path', '../escape.ics'), ('path', '/escape.ics'), ('path', 'samples/../escape.ics'),
    ('path', 'samples\\escape.ics'), ('path', 'C:/escape.ics'), ('path', 'samples//event.ics'),
    ('git_blob_sha1', 'main'), ('sha256', 'bad'), ('bytes', True), ('bytes', 3_000_000),
])
def test_unpinned_or_unsafe_sources_are_rejected_before_network(fetcher, tmp_path, field, value):
    source, _blob = fixture()
    source[field] = value
    with pytest.raises(fetcher.CorpusFetchError):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus',
                             opener=lambda *a, **kw: pytest.fail('Invalid source must never contact network'), token='private-token')


@pytest.mark.parametrize('field,value', [('sha', 'a' * 40), ('size', 0), ('encoding', 'raw'), ('content', '!!not-base64!!')])
def test_bad_blob_metadata_never_creates_a_fixture(fetcher, tmp_path, field, value):
    source, blob = fixture()
    blob[field] = value
    with pytest.raises(fetcher.CorpusFetchError):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus',
                             opener=lambda request, **kw: Response(blob, request.full_url))
    assert not list((tmp_path / 'corpus').rglob('*.ics'))


def test_correct_blob_metadata_with_wrong_content_is_rejected(fetcher, tmp_path):
    source, blob = fixture(b'expected')
    blob['content'] = base64.b64encode(b'altered!').decode()
    with pytest.raises(fetcher.CorpusFetchError, match='checksums'):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus',
                             opener=lambda request, **kw: Response(blob, request.full_url))


@pytest.mark.parametrize('field', ['git_blob_sha1', 'sha256', 'bytes'])
def test_each_content_pin_is_verified_independently_of_api_metadata(fetcher, tmp_path, field):
    source, blob = fixture()
    if field == 'git_blob_sha1':
        source[field] = blob['sha'] = '0' * 40
        source['url'] = source['url'].rsplit('/', 1)[0] + '/' + source[field]
    elif field == 'sha256':
        source[field] = '0' * 64
    else:
        source[field] += 1
        blob['size'] = source[field]
    with pytest.raises(fetcher.CorpusFetchError, match='checksums'):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus',
                             opener=lambda request, **kw: Response(blob, request.full_url))


def test_existing_different_file_is_never_overwritten_or_downloaded(fetcher, tmp_path):
    source, _blob = fixture()
    target = tmp_path / 'corpus/synthetic/calendar-tests/samples/event.ics'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'Preserve existing data')
    with pytest.raises(fetcher.CorpusFetchError, match='checksums'):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus',
                             opener=lambda *a, **kw: pytest.fail('Existing different files must not download'))
    assert target.read_bytes() == b'Preserve existing data'


def test_output_symlinks_cannot_redirect_fixture_writes(fetcher, tmp_path):
    source, _blob = fixture()
    destination, outside = tmp_path / 'corpus', tmp_path / 'outside'
    destination.mkdir()
    outside.mkdir()
    (destination / 'synthetic').symlink_to(outside, target_is_directory=True)
    with pytest.raises(fetcher.CorpusFetchError):
        fetcher.fetch_corpus(manifest(tmp_path, [source]), destination,
                             opener=lambda *a, **kw: pytest.fail('Symlink must be rejected first'))
    assert not list(outside.iterdir())


def test_redirects_do_not_forward_authentication(fetcher):
    with pytest.raises(fetcher.CorpusFetchError, match='redirected'):
        fetcher._NoRedirects().redirect_request(None, None, 302, '', {}, 'https://example.invalid/')


def test_network_errors_do_not_echo_credentials_and_environment_token_is_optional(fetcher, tmp_path, monkeypatch):
    import urllib.error
    source, _blob = fixture()
    monkeypatch.setenv('GITHUB_TOKEN', 'private-test-token')
    def fail(request, **kwargs):
        assert request.get_header('Authorization') == 'Bearer private-test-token'
        raise urllib.error.URLError('A remote error containing private-test-token and source text')
    with pytest.raises(fetcher.CorpusFetchError) as error:
        fetcher.fetch_corpus(manifest(tmp_path, [source]), tmp_path / 'corpus', opener=fail)
    assert 'private-test-token' not in str(error.value) and 'source text' not in str(error.value)


def test_duplicate_destination_paths_are_rejected_before_download(fetcher, tmp_path):
    source, _blob = fixture()
    with pytest.raises(fetcher.CorpusFetchError, match='same output file'):
        fetcher.fetch_corpus(manifest(tmp_path, [source, source]), tmp_path / 'corpus',
                             opener=lambda *a, **kw: pytest.fail('Duplicate destination must not download'))


def test_redirected_response_and_oversized_response_are_rejected(fetcher, tmp_path):
    source, blob = fixture()
    path = manifest(tmp_path, [source])
    with pytest.raises(fetcher.CorpusFetchError, match='unexpected URL'):
        fetcher.fetch_corpus(path, tmp_path / 'corpus', opener=lambda request, **kw: Response(blob, 'https://example.invalid/'))
    blob['content'] = 'A' * 20_000
    with pytest.raises(fetcher.CorpusFetchError, match='size limit'):
        fetcher.fetch_corpus(path, tmp_path / 'corpus', opener=lambda request, **kw: Response(blob, request.full_url))


def test_cli_requires_explicit_output_and_default_manifest_is_fully_pinned(fetcher):
    with pytest.raises(SystemExit) as error:
        fetcher.main([])
    assert error.value.code == 2
    sources = json.loads(fetcher.DEFAULT_MANIFEST.read_text())['sources']
    assert len(sources) == 16
    assert len({fetcher._source(source) for source in sources}) == len(sources)
