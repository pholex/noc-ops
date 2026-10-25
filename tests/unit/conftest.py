"""Offline tests use an anonymous workspace and isolate all generated state."""
from pathlib import Path

import pytest

from noc_ops import deploy_config, paths

FIXTURE_WORKSPACE = Path(__file__).resolve().parent.parent / "fixtures" / "workspace"


@pytest.fixture(autouse=True)
def _fixture_workspace(monkeypatch):
    monkeypatch.setenv("NOC_HOME", str(FIXTURE_WORKSPACE))
    deploy_config.reload()
    yield
    deploy_config.reload()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Redirect default fixture state to tmp_path; preserve explicit test workspaces."""
    real, safe = paths.state_dir, tmp_path / "state"
    repo_state = real()                        # NOC_HOME 已被上一条 fixture 钉到夹具工作区 → 夹具 .state

    def _guard():
        d = real()
        if d != repo_state:
            return d
        safe.mkdir(parents=True, exist_ok=True)
        return safe

    monkeypatch.setattr(paths, "state_dir", _guard)
