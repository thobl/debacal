"""Attach CI jobs and failure logs to a publication receipt without printing secrets.

GitHub's authenticated job-log endpoint redirects to a short-lived storage URL.
Only the API request receives the token; the storage request uses a fresh opener.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def error_summary(error):
    # Exception messages can contain signed URLs. Record only safe diagnostics.
    return f'HTTP {error.code}' if isinstance(error, HTTPError) else type(error).__name__


def update_receipt(path, env, *, api=None, storage=None):
    path = Path(path)
    receipt = json.loads(path.read_text())
    receipt.update(result=env['RELEASE_RESULT'], checks=env['CHECK_RESULT'],
                   reference=env['GITHUB_REF'], commit=env['GITHUB_SHA'],
                   workflow_url=f"https://github.com/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}")
    api = api if api is not None else build_opener(NoRedirect())
    storage = storage if storage is not None else build_opener()
    headers = {'Authorization': 'Bearer ' + env['GH_TOKEN'],
               'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'debacal-ci-receipt'}
    base = f"https://api.github.com/repos/{env['GITHUB_REPOSITORY']}/actions"
    errors, logs, signed_urls = [], [], []
    try:
        request = Request(f"{base}/runs/{env['GITHUB_RUN_ID']}/jobs?filter=all&per_page=100", headers=headers)
        with api.open(request, timeout=30) as response:
            snapshot = json.load(response)
        receipt['jobs'] = snapshot
    except Exception as error:
        snapshot = {'jobs': []}
        errors.append(f'Could not retrieve jobs ({error_summary(error)}).')
    for job in snapshot.get('jobs', []):
        if job.get('conclusion') != 'failure':
            continue
        job_id = job['id']
        try:
            request = Request(f'{base}/jobs/{int(job_id)}/logs', headers=headers)
            try:
                with api.open(request, timeout=30) as response:
                    content = response.read()
            except HTTPError as redirect:
                try:
                    if redirect.code != 302:
                        raise
                    location = redirect.headers.get('Location', '')
                    signed_urls.append(location)
                    target = urlsplit(location)
                    if target.scheme != 'https' or not target.netloc or target.username or target.password:
                        raise ValueError('Invalid log download location')
                finally:
                    redirect.close()
                # Never forward the authenticated API request or its headers.
                with storage.open(Request(location), timeout=30) as response:
                    content = response.read()
            logs.append(f"\n=== {job.get('name', 'Job')} ({job_id}) ===\n" + content.decode('utf-8', errors='replace'))
        except Exception as error:
            errors.append(f'Could not retrieve logs for job {job_id} ({error_summary(error)}).')

    def sanitize(value):
        for secret in [env['GH_TOKEN'], *signed_urls]:
            if secret:
                value = value.replace(secret, '<redacted>')
        for key, label in [('GITHUB_WORKSPACE', '<checkout>'), ('RUNNER_TEMP', '<runner-temp>'),
                           ('RUNNER_TOOL_CACHE', '<tool-cache>')]:
            if env.get(key):
                value = value.replace(env[key], label)
        return re.sub(r'https?://[^\s<>"\']+\?[^\s<>"\']+', '<redacted-url>', value)

    if logs:
        receipt['failed_checks'] = sanitize(''.join(logs))[-100000:]
    if errors:
        receipt['diagnostic_errors'] = errors

    def sanitize_data(value):
        if isinstance(value, str):
            return sanitize(value)
        if isinstance(value, list):
            return [sanitize_data(item) for item in value]
        if isinstance(value, dict):
            return {key: sanitize_data(item) for key, item in value.items()}
        return value

    path.write_text(json.dumps(sanitize_data(receipt), indent=2) + '\n')


if __name__ == '__main__':
    update_receipt(sys.argv[1], os.environ)
