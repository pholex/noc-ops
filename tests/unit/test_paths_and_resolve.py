"""Tier 1 单元测试：工作区路径解析（NOC_HOME 覆盖）。"""
import json

import pytest

pytestmark = pytest.mark.unit


def test_noc_home_override(monkeypatch, tmp_path):
    from noc_ops import paths
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    assert paths.workspace_dir() == tmp_path
    assert paths.data_json_path() == tmp_path / "data.json"
    assert paths.inventory_path() == tmp_path / "inventory.md"
    assert paths.runbooks_dir() == tmp_path / "runbooks"
    assert paths.ops_log_path() == tmp_path / ".state" / "ops-log.jsonl"

