"""Offline integration checks for Git index and commit-message privacy checks."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).parents[2] / 'scripts/check-public-content.py'
spec = importlib.util.spec_from_file_location('public_content', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_secrets_are_detected_without_disclosing_values():
    values = ['AK' + 'IA' + 'A' * 16, 'gh' + 'p_' + 'x' * 30,
              '-----BEGIN ' + 'PRIVATE KEY-----', 'password = "' + 'aB7X9yK2mN6pQ4rS' + '"']
    for value in values:
        found = checker.scan('example.py', value.encode(), [])
        assert found
        assert value not in repr(found)


def test_narrative_and_private_terms():
    assert checker.scan('notes.md', ('借鉴' + '自 another repo').encode(), [])
    assert checker.scan('notes.md', ('# Road' + 'map').encode(), [])
    assert checker.scan('sample.py', b'# SYNTHETIC-PRIVATE-ORG', ['synthetic-private-org'])
    assert checker.scan('synthetic-private-org.txt', b'', ['synthetic-private-org'])
    assert checker.safe_label('synthetic-private-org.txt', ['synthetic-private-org']).startswith('file-')
    assert checker.scan('sample.py', ('/Users/' + 'private-person/project').encode(), [])


def test_product_plans_and_license_notices_are_allowed():
    for content in ['ops reboot --dry-run shows an operation plan', 'Copyright Example Authors',
                    'Licensed under the Apache License, Version 2.0', '参考官方 API 文档',
                    'token = os.environ["API_TOKEN"]', 'password = "example-password-value"']:
        assert checker.scan('sample.md', content.encode(), []) == []


def test_private_workspace_and_backup_paths():
    for path in ['ops-config.toml', 'ops-config.toml.20261025.bak', 'data.json', '.state/a',
                 'runbooks/task.md', '.env', '.env.production', 'id_rsa']:
        assert checker.scan(path, b'', [])
    assert not checker.scan('tests/fixtures/workspace/ops-config.toml', b'', [])
    assert not checker.scan('src/noc_ops/assets/runbooks/README.md', b'', [])


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'config', 'user.name', 'Example'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'config', 'user.email', 'example@example.com'], check=True)
    return tmp_path


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


def run(repo, *args):
    return subprocess.run(['python3', str(SCRIPT), *args], cwd=repo, capture_output=True, text=True)


def test_staged_content_is_checked_even_if_worktree_is_clean(repo):
    secret = 'gh' + 'p_' + 'z' * 30
    path = repo / 'example.py'
    path.write_text(secret)
    git(repo, 'add', 'example.py')
    path.write_text('safe worktree')
    result = run(repo, '--staged')
    assert result.returncode == 1
    assert 'service-token' in result.stderr
    assert secret not in result.stderr + result.stdout
    assert run(repo, '--all').returncode == 0


def test_commit_message_and_local_terms(repo):
    terms = repo / '.git/info/public-content-terms'
    terms.write_text('synthetic-private-org\n')
    msg = repo / '.git/COMMIT_EDITMSG'
    msg.write_text('fix: change\n\nsynthetic-private-org\n')
    result = run(repo, '--commit-msg', str(msg))
    assert result.returncode == 1
    assert 'private-identifier' in result.stderr
    assert 'synthetic-private-org' not in result.stderr


def test_history_detects_deleted_content_and_commit_body(repo):
    path = repo / 'example.txt'
    path.write_text('safe')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'init')
    path.write_text('gh' + 'p_' + 'q' * 30)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'add example')
    path.write_text('safe')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fix example')
    assert run(repo, '--all').returncode == 0
    assert run(repo, '--history').returncode == 1
    assert run(repo, '--commits', 'HEAD~2..HEAD').returncode == 1
    assert run(repo, '--commits', 'HEAD^!').returncode == 0


def install_hooks(repo):
    import shutil
    source = SCRIPT.parents[1]
    for relative in ['scripts/check-public-content.py', 'scripts/install-git-hooks.sh',
                     '.githooks/pre-commit', '.githooks/commit-msg', '.githooks/pre-push']:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    return subprocess.run(['sh', 'scripts/install-git-hooks.sh'], cwd=repo, capture_output=True, text=True)


def test_installed_hooks_block_files_messages_and_outgoing_history(repo):
    secret = 'gh' + 'p_' + 'w' * 30
    path = repo / 'example.txt'
    path.write_text('safe')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'init')
    base = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    path.write_text(secret)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'change')
    path.write_text('safe')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fix')
    head = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    assert install_hooks(repo).returncode == 0
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'chore: enable checks')
    path.write_text(secret)
    git(repo, 'add', '.')
    blocked = subprocess.run(['git', 'commit', '-qm', 'change'], cwd=repo, capture_output=True, text=True)
    assert blocked.returncode != 0 and 'service-token' in blocked.stderr
    assert secret not in blocked.stderr
    path.write_text('safe again')
    git(repo, 'add', '.')
    msg = repo / '.git/test-message'
    msg.write_text('fix: change\n\n' + '借鉴' + '自 sample\n')
    blocked = subprocess.run(['git', 'commit', '-qF', str(msg)], cwd=repo, capture_output=True, text=True)
    assert blocked.returncode != 0 and 'development-narrative' in blocked.stderr
    result = subprocess.run(['sh', '.githooks/pre-push'], cwd=repo, capture_output=True, text=True,
                            input=f'refs/heads/main {head} refs/heads/main {base}\n')
    assert result.returncode != 0 and 'service-token' in result.stderr
    assert secret not in result.stderr
    deletion = subprocess.run(['sh', '.githooks/pre-push'], cwd=repo, capture_output=True, text=True,
                              input=f'(delete) {"0" * 40} refs/heads/main {head}\n')
    assert deletion.returncode == 0


def test_installer_preserves_existing_hook(repo):
    hook = repo / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\nexit 0\n')
    result = install_hooks(repo)
    assert result.returncode != 0
    assert hook.read_text() == '#!/bin/sh\nexit 0\n'


def test_new_tag_checks_snapshot_and_only_unpublished_commits(repo):
    path = repo / 'example.txt'
    path.write_text('gh' + 'p_' + 'v' * 30)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'init')
    old = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    path.write_text('safe')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fix')
    clean = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    git(repo, 'update-ref', 'refs/remotes/origin/main', clean)
    assert install_hooks(repo).returncode == 0
    def push(oid):
        return subprocess.run(['sh', '.githooks/pre-push', 'origin'], cwd=repo,
                              capture_output=True, text=True,
                              input=f'refs/tags/v1 {oid} refs/tags/v1 {"0" * 40}\n')
    assert push(clean).returncode == 0
    assert push(old).returncode == 1
    path.write_text('gh' + 'p_' + 'u' * 30)
    git(repo, 'add', 'example.txt')
    # Tree scanning is independent of staged or working-tree changes.
    assert run(repo, '--tree', clean).returncode == 0
