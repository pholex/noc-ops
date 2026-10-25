"""全新机器（没装任何云 CLI、出厂占位配置、没有 ~/.ssh）上 `ops sync` / `ops noc` 必须优雅工作——
2026-10-25 Docker（python:3.12-slim）走 INSTALL.md 实测暴露的四处崩溃的回归网。离线、零云调用。"""
import subprocess

import pytest

from noc_ops import deploy_config, schedule_ops, sync
from noc_ops.providers import PROVIDERS, aws, tencent

pytestmark = pytest.mark.unit


def _raise_missing(*a, **k):
    raise FileNotFoundError(2, "No such file or directory", "aws")


def test_tencent_unconfigured_returns_empty(monkeypatch):
    """[tencent] 整段注释掉（出厂模板）→ cloud('tencent') 为 {} → 以前 KeyError('regions') 崩整次 sync。"""
    monkeypatch.setattr(deploy_config, "cloud", lambda name: {})
    calls = []
    monkeypatch.setattr(tencent.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    assert PROVIDERS["tencent"].list_instances() == []
    assert calls == []                      # 没配就不该去碰 tccli


def test_sync_skips_providers_whose_cli_is_missing(monkeypatch, capsys):
    """本机没装某云 CLI：该云跳过；已配账号的提示一次，没配的静默；无 CLI 的云（Cloudflare/Supabase）不受影响。"""
    monkeypatch.setattr(sync.shutil, "which", lambda b: None if b == "aws" else f"/usr/bin/{b}")
    monkeypatch.setattr(deploy_config, "aws_accounts", lambda: [{"profile": "x", "partition": "global"}])
    sync._MISSING_CLI_WARNED.clear()
    provs = sync._providers()
    assert "aws" not in provs
    assert "cloudflare" in provs and "supabase" in provs and "aliyun" in provs
    sync._providers()                        # 第二次不再重复提示
    out = capsys.readouterr().out
    assert out.count("未找到 aws CLI") == 1
    # 只要这几朵
    assert set(sync._providers(["aliyun", "aws"])) == {"aliyun"}


def test_sync_silent_when_missing_cli_cloud_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(sync.shutil, "which", lambda b: None if b == "tccli" else f"/usr/bin/{b}")
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda name, network=None: [])
    sync._MISSING_CLI_WARNED.clear()
    assert "tencent" not in sync._providers()
    assert "未找到" not in capsys.readouterr().out


def test_aws_json_and_scheduler_tolerate_missing_binary(monkeypatch):
    monkeypatch.setattr(aws.subprocess, "run", _raise_missing)
    assert aws._aws_json("p", "us-east-1", ["ec2", "describe-instances"]) is None
    monkeypatch.setattr(schedule_ops.subprocess, "run", _raise_missing)
    ok, out = schedule_ops._aws(["list-schedules"])
    assert ok is False and "aws" in out
    assert schedule_ops._json(ok, out) is None


def test_write_ssh_config_creates_dotssh(monkeypatch, tmp_path):
    """新机器没有 ~/.ssh：以前 write_text 直接 FileNotFoundError。"""
    cfg = tmp_path / "home" / ".ssh" / "config"
    monkeypatch.setattr(sync, "SSH_CONFIG", cfg)
    sync.write_ssh_config("Host demo\n    HostName 1.2.3.4")
    assert cfg.exists() and sync.MARKER_BEGIN in cfg.read_text()
    assert (cfg.stat().st_mode & 0o777) == 0o600
