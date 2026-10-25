#!/usr/bin/env python3
"""Check whether PyPI already hosts these exact release artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import urlopen


def published(version, dist):
    files = list(Path(dist).glob('*.whl')) + list(Path(dist).glob('*.tar.gz'))
    if len(files) != 2:
        raise ValueError('Expected one wheel and one sdist')
    try:
        with urlopen(f'https://pypi.org/pypi/noc-ops/{version}/json', timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    remote = {item['filename']: item['digests']['sha256'] for item in data['urls']}
    local = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if remote != local:
        raise ValueError('Existing release artifacts differ; refusing to overwrite or skip verification')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--dist', required=True)
    args = parser.parse_args()
    try:
        print('published=' + str(published(args.version, args.dist)).lower())
    except (OSError, ValueError, KeyError):
        print('Published artifact verification failed.', file=sys.stderr)
        raise SystemExit(1)
