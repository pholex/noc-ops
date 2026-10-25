#!/usr/bin/env python3
"""Offline checks for public repository content. Findings never include source text."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

RULES = {
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'cloud-access-key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b(?:LTAI|AKID)[A-Za-z0-9]{20,}\b',
    'service-token': r'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|sbp_[a-f0-9]{30,}|sk-[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,})\b',
    'jwt': r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b',
    'personal-path': r'/(?:Users|home)/(?!user\b|example\b|tester\b|runner\b|testuser\b)[A-Za-z0-9_.-]+',
    'development-narrative': r'\u501f\u9274\u81ea|\u53c2\u8003\u5b9e\u73b0[\uff1a:]|\u7075\u611f\u6765\u81ea|(?:inspired|borrowed|adapted|ported|learned)\s+from|\u672c\u673a.{0,20}\u5b9e\u6d4b|\u4f9b\u4ee5\u540e\u63a5\u7740\u67e5|\u4e0b\u4e00\u6b65\u6700\u9760\u8c31|\u9879\u76ee\u8fed\u4ee3\u8ba1\u5212|\u53d1\u5c55\u8def\u7ebf|\u8fed\u4ee3\u8ba1\u5212|\u5185\u90e8\u8ba8\u8bba|\u5f00\u53d1\u8fc7\u7a0b|\b\u0072oadmap\b',
}
COMPILED = {key: re.compile(value, re.I if key == 'development-narrative' else 0)
            for key, value in RULES.items()}
ASSIGNMENT = re.compile(r'''(?i)\b(?:password|passwd|api[_-]?key|api[_-]?token|access[_-]?token|client[_-]?secret|secret[_-]?key|aws_secret_access_key)\b["']?\s*[:=]\s*["']([A-Za-z0-9_+/=.-]{16,})["']''')
BLOCKED_FILES = {'ops-config.toml', 'data.json', 'inventory.md', 'local-servers.toml',
                 'capabilities.toml', '.mcp.json'}
POLICY_FILES = {'AGENTS.md', 'CLAUDE.md', 'PUBLIC_CONTENT.md'}


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def local_terms() -> list[str]:
    path = Path(os.fsdecode(git('rev-parse', '--git-path', 'info/public-content-terms')).strip())
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding='utf-8').splitlines()
            if x.strip() and not x.lstrip().startswith('#')]


def is_blocked_path(path: str) -> bool:
    p = PurePosixPath(path)
    if path in BLOCKED_FILES or p.parts[0] in {'.state', '.aws', 'runbooks'}:
        return True
    return (p.name == '.env' or p.name.startswith('.env.') and p.name not in {'.env.example', '.env.sample'}
            or p.name in {'id_rsa', 'id_ed25519', 'credentials', 'public-content-terms'}
            or p.suffix in {'.p12', '.pfx', '.key'}
            or p.name.startswith('ops-config.toml.'))


def scan(path: str, data: bytes, terms: list[str], *, message: bool = False) -> list[tuple[int, str]]:
    findings = []
    if not message and is_blocked_path(path):
        findings.append((0, 'private-workspace-file'))
    # Search private terms in filenames as well; logs use an opaque label if matched.
    if any(t.casefold() in path.casefold() for t in terms):
        findings.append((0, 'private-identifier'))
    text = data.decode('utf-8', errors='replace')
    for n, line in enumerate(text.splitlines(), 1):
        for category, pattern in COMPILED.items():
            if category == 'development-narrative' and path in POLICY_FILES:
                continue
            if pattern.search(line):
                findings.append((n, category))
        for match in ASSIGNMENT.finditer(line):
            value = match.group(1)
            if not re.fullmatch(r'(?:example|placeholder|dummy|test|fake)[A-Za-z0-9_.-]*', value, re.I):
                findings.append((n, 'credential-literal'))
        if any(t.casefold() in line.casefold() for t in terms):
            findings.append((n, 'private-identifier'))
    return sorted(set(findings))


def safe_label(path: str, terms: list[str]) -> str:
    if any(t.casefold() in path.casefold() for t in terms) or any(ord(c) < 32 for c in path):
        return 'file-' + hashlib.sha256(os.fsencode(path)).hexdigest()[:12]
    return path


def tree_files(tree: str) -> list[tuple[str, str]]:
    rows = []
    for entry in git('ls-tree', '-rz', tree).split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        _, kind, oid = meta.split()
        if kind == b'blob':
            rows.append((os.fsdecode(path), oid.decode()))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged', action='store_true', help='scan the complete Git index')
    group.add_argument('--all', action='store_true', help='scan tracked working-tree files')
    group.add_argument('--commit-msg', type=Path, help='scan proposed commit message')
    group.add_argument('--commits', help='scan changed blobs and messages in a Git revision range')
    group.add_argument('--history', action='store_true', help='audit all reachable snapshots and messages')
    group.add_argument('--tree', help='scan a complete committed snapshot')
    args = parser.parse_args(argv)
    root = Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip())
    if args.commit_msg:
        args.commit_msg = args.commit_msg.resolve()
    os.chdir(root)
    terms = local_terms()
    failed = 0
    checked = 0
    seen: set[tuple[str, str]] = set()

    def check(path: str, data: bytes, prefix: str = '', message: bool = False):
        nonlocal failed, checked
        checked += 1
        findings = scan(path, data, terms, message=message)
        for line, category in findings:
            print(f'{prefix}{safe_label(path, terms)}:{line}: {category}', file=sys.stderr)
        failed += len(findings)

    if args.commit_msg:
        check('COMMIT_MESSAGE', args.commit_msg.read_bytes(), message=True)
    elif args.tree:
        for path, oid in tree_files(args.tree):
            check(path, git('cat-file', 'blob', oid))
    elif args.all:
        for raw in git('ls-files', '-z').split(b'\0'):
            if raw:
                path = os.fsdecode(raw)
                if Path(path).is_file() and not Path(path).is_symlink():
                    check(path, Path(path).read_bytes())
    elif args.staged:
        for raw in git('ls-files', '--stage', '-z').split(b'\0'):
            if not raw:
                continue
            meta, name = raw.split(b'\t', 1)
            mode, oid, stage = meta.split()
            if stage != b'0':
                raise ValueError('unmerged index')
            if mode != b'160000':
                check(os.fsdecode(name), git('cat-file', 'blob', oid.decode()))
    else:
        revisions = git('rev-list', '--all' if args.history else args.commits).decode().splitlines()
        for revision in revisions:
            check('COMMIT_MESSAGE', git('show', '-s', '--format=%B', revision), revision[:8] + ':', True)
            changed = None if args.history else set(git('diff-tree', '--root', '-m', '--no-commit-id', '--name-only', '-r', '-z', revision).split(b'\0'))
            for path, oid in tree_files(revision):
                if changed is not None and os.fsencode(path) not in changed:
                    continue
                if (path, oid) in seen:
                    continue
                seen.add((path, oid))
                check(path, git('cat-file', 'blob', oid), revision[:8] + ':')
    if failed:
        print(f'Public-content check blocked: {failed} finding(s); source text suppressed.', file=sys.stderr)
        return 1
    print(f'Public-content check passed ({checked} items).')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('Public-content check could not complete; inspect local Git/files and retry.', file=sys.stderr)
        raise SystemExit(2)
