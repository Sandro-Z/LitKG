"""Resolve the official image digest and create local search settings without printing secrets."""
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.request import Request, urlopen


def prepare():
    root = Path(__file__).resolve().parents[1]
    target = root / '.web-research.env'
    if target.exists():
        print('Configuration already exists; preserved without reading or displaying it.')
        return
    # Only registry metadata is fetched. The image is executed later using its pinned digest.
    with urlopen('https://auth.docker.io/token?service=registry.docker.io&scope=repository:searxng/searxng:pull', timeout=20) as response:
        auth_value = json.load(response)['token']
    request = Request('https://registry-1.docker.io/v2/searxng/searxng/manifests/latest', headers={
        'Authorization': 'Bearer ' + auth_value,
        'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'})
    with urlopen(request, timeout=20) as response:
        digest = response.headers.get('Docker-Content-Digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('INVALID_IMAGE_DIGEST')
    content = ('SEARXNG_IMAGE=docker.io/searxng/searxng@' + digest + '\n'
               'SEARXNG_SECRET=' + secrets.token_hex(32) + '\n'
               'WEB_RESEARCH_REVISION=1\n')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(content)
    print('Created .web-research.env with an immutable official image and a local secret; values are not displayed.')


if __name__ == '__main__':
    try:
        prepare()
    except Exception:
        print('Preparation failed. Check outbound HTTPS to the official Docker registry; no configuration was overwritten.', file=sys.stderr)
        sys.exit(1)
