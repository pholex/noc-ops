"""Tier 1：Polar 订阅 gate——开发模式放行、在线校验、订阅失效拦截、离线宽限。

不触网：monkeypatch license._post 模拟 Polar 响应；用临时 HOME 隔离 license.json。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from noc_ops import license as lic

pytestmark = pytest.mark.unit


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(lic._DEV_ENV, raising=False)           # 隔离本机 dev 旁路，否则 check() 被短路
    monkeypatch.setattr(lic, "ORGANIZATION_ID", "org_test")   # 视为已配置
    return tmp_path


def _seed(rec):
    f = lic._license_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec), encoding="utf-8")


def test_dev_mode_passes_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(lic._DEV_ENV, raising=False)        # 隔离本机 dev 旁路，测的是"未配置 org"这条放行
    monkeypatch.setattr(lic, "ORGANIZATION_ID", "<占位>")   # 未配置 → 开发模式放行
    ok, reason = lic.check()
    assert ok and "开发模式" in reason


def test_unactivated_blocks(home):
    ok, reason = lic.check()
    assert not ok and "未激活" in reason


def test_online_granted_passes_and_caches(home, monkeypatch):
    _seed({"key": "k", "activation_id": "a", "last_ok": _ago(2)})
    monkeypatch.setattr(lic, "_post", lambda p, b: {"status": "granted", "expires_at": None})
    ok, reason = lic.check()
    assert ok and reason == "ok"
    assert json.loads(lic._license_file().read_text())["status"] == "granted"


def test_online_revoked_blocks(home, monkeypatch):
    _seed({"key": "k", "last_ok": _ago(2)})
    monkeypatch.setattr(lic, "_post", lambda p, b: {"status": "revoked"})
    ok, reason = lic.check()
    assert not ok and "revoked" in reason


def test_online_expired_blocks(home, monkeypatch):
    _seed({"key": "k", "last_ok": _ago(2)})
    monkeypatch.setattr(lic, "_post", lambda p, b: {"status": "granted", "expires_at": _ago(1)})
    ok, reason = lic.check()
    assert not ok and "过期" in reason


def test_offline_within_grace_passes(home, monkeypatch):
    import urllib.error
    _seed({"key": "k", "last_ok": _ago(3)})       # 3 天前成功过，宽限 14 天
    def boom(p, b): raise urllib.error.URLError("offline")
    monkeypatch.setattr(lic, "_post", boom)
    ok, reason = lic.check()
    assert ok and "宽限" in reason


def test_offline_beyond_grace_blocks(home, monkeypatch):
    import urllib.error
    _seed({"key": "k", "last_ok": _ago(30)})      # 超过 14 天宽限
    def boom(p, b): raise urllib.error.URLError("offline")
    monkeypatch.setattr(lic, "_post", boom)
    ok, reason = lic.check()
    assert not ok and "宽限期" in reason


def test_http_403_blocks(home, monkeypatch):
    import urllib.error
    _seed({"key": "k", "last_ok": _ago(2)})
    def http403(p, b):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    monkeypatch.setattr(lic, "_post", http403)
    ok, reason = lic.check()
    assert not ok and ("无效" in reason or "授权" in reason)


def test_activate_stores_record(home, monkeypatch):
    monkeypatch.setattr(lic, "_post", lambda p, b: {"id": "act-1", "license_key": {"status": "granted", "expires_at": None}})
    ok, msg = lic.activate("mykey")
    assert ok
    rec = json.loads(lic._license_file().read_text())
    assert rec["key"] == "mykey" and rec["activation_id"] == "act-1" and rec["status"] == "granted"
