"""腾讯云 COS 桶：coscli ls 表格解析、未装/未配置提示、stdin 关闭。离线。"""
import subprocess

import pytest

from noc_ops import deploy_config
from noc_ops.providers.tencent import TencentProvider

pytestmark = pytest.mark.unit

CFG = {"account": "100000000001", "regions": {"ap-nanjing": ("Nanjing", "🇨🇳")}}
LS_OUT = """         BUCKET NAME         |     REGION      |     CREATE DATE
-----------------------------+-----------------+-----------------------
  noc-test-abc123-1250000000 | ap-nanjing      | 2026-10-25T05:14:57Z
  media-archive-1250000000   | ap-guangzhou    | 2025-01-02T03:04:05Z
-----------------------------+-----------------+-----------------------
                               TOTAL BUCKETS:  |          2
                             ------------------+-----------------------
"""


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud", lambda c: CFG if c == "tencent" else {})


def test_cos_buckets_rows(monkeypatch, configured):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["stdin"] = cmd, kw.get("stdin")
        return subprocess.CompletedProcess(cmd, 0, stdout=LS_OUT, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = TencentProvider().list_storage()
    assert seen["cmd"] == ["coscli", "ls"] and seen["stdin"] is subprocess.DEVNULL
    assert [r["name"] for r in rows] == ["media-archive-1250000000", "noc-test-abc123-1250000000"]
    r = rows[1]
    assert r == {"name": "noc-test-abc123-1250000000", "cloud": "tencent", "account": "100000000001",
                 "region": "ap-nanjing", "region_display": "Nanjing", "flag": "🇨🇳",
                 "domain": "noc-test-abc123-1250000000.cos.ap-nanjing.myqcloud.com", "created": "2026-10-25"}
    assert rows[0]["region_display"] == "ap-guangzhou"


def test_coscli_missing_or_unconfigured(monkeypatch, configured, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("coscli")))
    assert TencentProvider().list_storage() == []
    assert "未安装 coscli" in capsys.readouterr().err
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="Welcome to coscli!\n...generate the default configuration file of coscli.\nInput Your Mode:\n", stderr=""))
    assert TencentProvider().list_storage() == []
    assert "coscli 未配置" in capsys.readouterr().err


def test_unconfigured_tencent_never_runs_coscli(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud", lambda c: {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert TencentProvider().list_storage() == []
