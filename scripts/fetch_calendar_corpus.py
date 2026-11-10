"""Fetch only checksum-pinned upstream calendar fixtures into an explicit folder.

Public fixtures can contain personal-looking sample data and are deliberately
kept outside this repository. Mutable public feeds in the manifest are not
downloaded. No app packages or network access are needed to test this utility.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / 'tests' / 'interop_sources.json'
MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 20


class CorpusFetchError(ValueError):
    """A controlled diagnostic that never contains remote calendar contents."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # urllib normally forwards request headers on redirects. Pinned blob
        # endpoints need no redirects, so never forward an optional API token.
        raise CorpusFetchError('GitHub redirected a pinned blob request; review its repository URL.')


def _source(source):
    if not isinstance(source, dict):
        raise CorpusFetchError('Each source must contain pinned blob metadata.')
    repository, url, path = (source.get(key) for key in ('repository', 'url', 'path'))
    sha1, sha256, size = (source.get(key) for key in ('git_blob_sha1', 'sha256', 'bytes'))
    if (not isinstance(sha1, str) or re.fullmatch('[0-9a-f]{40}', sha1) is None
            or not isinstance(sha256, str) or re.fullmatch('[0-9a-f]{64}', sha256) is None
            or type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES):
        raise CorpusFetchError('A source needs valid SHA-1, SHA-256 and a bounded byte count.')
    if (not isinstance(repository, str) or not isinstance(url, str)
            or any(ord(char) <= 32 for char in repository + url)):
        raise CorpusFetchError('A source needs HTTPS repository and API blob URLs.')
    try:
        repo, api = urllib.parse.urlsplit(repository), urllib.parse.urlsplit(url)
    except ValueError:
        raise CorpusFetchError('A source has an invalid repository or API URL.') from None
    owner_repo = repo.path.removeprefix('/').split('/')
    if (repo.scheme != 'https' or repo.netloc != 'github.com' or repo.query or repo.fragment
            or len(owner_repo) != 2 or any(part in {'.', '..'} or
                re.fullmatch('[A-Za-z0-9_.-]+', part) is None for part in owner_repo)):
        raise CorpusFetchError('A source repository must be an exact https://github.com/owner/repository URL.')
    expected = '/repos/' + '/'.join(owner_repo) + '/git/blobs/' + sha1
    if (api.scheme != 'https' or api.netloc != 'api.github.com' or api.path != expected
            or api.query or api.fragment):
        raise CorpusFetchError('A source URL must identify its immutable blob on https://api.github.com.')
    if (not isinstance(path, str) or len(path.encode('utf-8')) > 4096
            or '\\' in path or ':' in path or any(ord(char) < 32 for char in path)
            or any(part in {'', '.', '..'} for part in path.split('/'))):
        raise CorpusFetchError('A source path must be a safe relative repository path.')
    return Path(*owner_repo, *path.split('/'))


def _verify(data, source):
    git_hash = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data,
                            usedforsecurity=False).hexdigest()
    if (len(data) != source['bytes'] or hashlib.sha256(data).hexdigest() != source['sha256']
            or git_hash != source['git_blob_sha1']):
        raise CorpusFetchError('Fixture bytes do not match their pinned size and checksums.')


def _target(directory, relative):
    target = directory / relative
    if not target.resolve().is_relative_to(directory):
        raise CorpusFetchError('A source path escapes the output directory.')
    current = directory
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CorpusFetchError('Fixture output paths must not traverse symbolic links.')
    return target


def _download(source, opener, token):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'debacal-corpus-check'}
    if token:
        if '\r' in token or '\n' in token:
            raise CorpusFetchError('GITHUB_TOKEN must not contain line breaks.')
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(source['url'], headers=headers)
    try:
        with opener(request, timeout=REQUEST_TIMEOUT) as response:
            if response.geturl() != source['url']:
                raise CorpusFetchError('The blob response came from an unexpected URL.')
            limit = source['bytes'] * 2 + 8192
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise CorpusFetchError(f'GitHub API returned HTTP {error.code}.') from None
    except (urllib.error.URLError, OSError):
        raise CorpusFetchError('The GitHub API request failed. Check network access and authentication.') from None
    if len(raw) > limit:
        raise CorpusFetchError('The GitHub blob response exceeds its size limit.')
    try:
        blob = json.loads(raw)
        if (not isinstance(blob, dict) or blob.get('sha') != source['git_blob_sha1']
                or type(blob.get('size')) is not int or blob['size'] != source['bytes'] or blob.get('encoding') != 'base64'
                or not isinstance(blob.get('content'), str)):
            raise ValueError
        data = base64.b64decode(''.join(blob['content'].split()), validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise CorpusFetchError('GitHub returned invalid or mismatched blob metadata.') from None
    _verify(data, source)
    return data


def fetch_corpus(manifest, output, *, opener=None, token=None):
    """Verify existing fixtures or fetch immutable sources without overwriting."""
    with Path(manifest).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CorpusFetchError('The corpus manifest exceeds its size limit.')
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise CorpusFetchError('The corpus manifest is not valid JSON.') from None
    sources = value.get('sources') if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(sources, list) or len(sources) > 1024:
        raise CorpusFetchError('Expected a version 1 corpus manifest with at most 1024 sources.')
    relatives = [_source(source) for source in sources]
    if len(set(relatives)) != len(relatives):
        raise CorpusFetchError('Multiple sources map to the same output file.')
    directory = Path(output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    opener = opener or urllib.request.build_opener(_NoRedirects()).open
    token = os.environ.get('GITHUB_TOKEN', '') if token is None else token
    paths = []
    for source, relative in zip(sources, relatives):
        target = _target(directory, relative)
        if target.exists():
            if not target.is_file():
                raise CorpusFetchError('An existing fixture path is not a regular file.')
            with target.open('rb') as stream:
                data = stream.read(source['bytes'] + 1)
            _verify(data, source)
        else:
            data = _download(source, opener, token)
            target.parent.mkdir(parents=True, exist_ok=True)
            _target(directory, relative)
            with target.open('xb') as stream:
                stream.write(data)
        paths.append(target)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='Explicit destination, preferably outside the checkout')
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        paths = fetch_corpus(args.manifest, args.output)
    except (CorpusFetchError, OSError) as error:
        parser.exit(1, f'Corpus fetch failed: {error}\n')
    print(f'{len(paths)} verified fixtures in {args.output.resolve()}')


if __name__ == '__main__':
    main()
