import hashlib
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

pytestmark = pytest.mark.unit
spec = importlib.util.spec_from_file_location('published_artifacts', Path(__file__).parents[2] / 'scripts/check-published.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def artifacts(tmp_path):
    for name in ['noc_ops-0.3.0.whl', 'noc_ops-0.3.0.tar.gz']:
        (tmp_path / name).write_bytes(b'synthetic artifact')
    return tmp_path


def test_exact_artifacts_are_idempotent(monkeypatch, artifacts):
    payload = {'urls': [{'filename': p.name, 'digests': {'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}}
                        for p in artifacts.iterdir()]}
    monkeypatch.setattr(module, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    assert module.published('0.3.0', artifacts) is True
    next(artifacts.iterdir()).write_bytes(b'different')
    with pytest.raises(ValueError):
        module.published('0.3.0', artifacts)


@pytest.mark.parametrize('status,expected', [(404, False), (500, None)])
def test_only_missing_release_permits_upload(monkeypatch, artifacts, status, expected):
    def fail(*args, **kwargs):
        raise HTTPError('https://pypi.org', status, 'synthetic failure', {}, None)
    monkeypatch.setattr(module, 'urlopen', fail)
    if expected is False:
        assert module.published('0.3.0', artifacts) is False
    else:
        with pytest.raises(HTTPError):
            module.published('0.3.0', artifacts)
