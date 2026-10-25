"""Tier 1 单元测试：refresh_net 的本地网络机器「账号级沿用」逻辑（_scan_local_net）+ 网络域切分。

核心不变量：本地网络云（synology/vmware/sangfor…）多账号部分连通时，
- 连不上的账号 → 沿用上次 data.json 里该账号的记录（不被在线账号挤掉）；
- 连得上的账号里缺失的机器 → 视为真删除，正常消失；
- 整个 provider 抛异常 → 整朵云沿用上次记录。
回归防护：2026-07-01 synology 两台 NAS 一台超时一台在线，超时那台被静默丢弃的 bug。
"""
import pytest

from noc_ops import sync

pytestmark = pytest.mark.unit


class _FakeProvider:
    def __init__(self, fresh, unreachable):
        self._fresh = fresh
        self._unreachable = list(unreachable)

    def list_instances(self, network=None):
        self.seen_network = network
        return self._fresh

    def unreachable_accounts(self):
        return list(self._unreachable)


def test_unreachable_account_carried_reachable_deletion_propagates(monkeypatch):
    prev = [
        {"cloud": "synology", "_acct": "sample-resource-13",  "id": "syno-192.0.2.12",  "name": "sample-resource-13"},
        {"cloud": "synology", "_acct": "sample-resource-05", "id": "syno-100.64.0.8", "name": "DS218+ old"},
        {"cloud": "sangfor",  "_acct": "sangfor",   "id": "vm-gone",           "name": "被销毁的VM"},
    ]
    # synology：sample-resource-05 在线返回新记录；sample-resource-13 超时（unreachable）
    syn = _FakeProvider(
        fresh=[{"cloud": "synology", "_acct": "sample-resource-05", "id": "syno-100.64.0.8", "name": "DS218+"}],
        unreachable=["sample-resource-13"],
    )
    # sangfor：平台在线，vm-gone 真被删（fresh 空，无 unreachable）
    san = _FakeProvider(fresh=[], unreachable=[])
    monkeypatch.setattr(sync, "PROVIDERS", {"synology": syn, "sangfor": san})
    monkeypatch.setattr(sync, "LOCAL_NET_CLOUDS", {"synology", "sangfor"})

    out = sync._scan_local_net(prev)
    ids = {i["id"] for i in out}

    # 超时账号 sample-resource-13 被沿用
    assert "syno-192.0.2.12" in ids
    # 在线账号返回的是最新记录（DS218+，不是旧的 "DS218+ old"）
    assert any(i["name"] == "DS218+" for i in out)
    assert not any(i["name"] == "DS218+ old" for i in out)
    # 在线平台上真被删的 VM 正常消失
    assert "vm-gone" not in ids


def test_partial_pagination_dedups_by_id(monkeypatch):
    # sangfor 分页中断：page0 拿到 vm-a，但账号被标记 unreachable；prev 有 vm-a + vm-b
    prev = [
        {"cloud": "sangfor", "_acct": "sangfor", "id": "vm-a", "name": "A-old"},
        {"cloud": "sangfor", "_acct": "sangfor", "id": "vm-b", "name": "B"},
    ]
    san = _FakeProvider(
        fresh=[{"cloud": "sangfor", "_acct": "sangfor", "id": "vm-a", "name": "A-new"}],
        unreachable=["sangfor"],
    )
    monkeypatch.setattr(sync, "PROVIDERS", {"sangfor": san})
    monkeypatch.setattr(sync, "LOCAL_NET_CLOUDS", {"sangfor"})

    out = sync._scan_local_net(prev)
    by_id = {i["id"]: i for i in out}
    # vm-a 只出现一次，且是 fresh 版本（不与沿用记录重复）
    assert sum(1 for i in out if i["id"] == "vm-a") == 1
    assert by_id["vm-a"]["name"] == "A-new"
    # vm-b（本轮没拿到）作为 unreachable 账号的旧记录被沿用
    assert by_id["vm-b"]["name"] == "B"


def test_whole_provider_exception_carries_cloud(monkeypatch):
    prev = [{"cloud": "vmware", "_acct": "vmware", "id": "vm-x", "name": "X"}]

    class _Boom:
        def list_instances(self, network=None):
            raise RuntimeError("boom")

        def unreachable_accounts(self):
            return []

    monkeypatch.setattr(sync, "PROVIDERS", {"vmware": _Boom()})
    monkeypatch.setattr(sync, "LOCAL_NET_CLOUDS", {"vmware"})

    out = sync._scan_local_net(prev)
    assert {i["id"] for i in out} == {"vm-x"}  # 整朵云沿用


def test_reachable_empty_account_no_carry(monkeypatch):
    # 账号在线且确实无实例（unreachable 空）→ prev 记录不沿用（真的空）
    prev = [{"cloud": "vmware", "_acct": "vmware", "id": "vm-stale", "name": "stale"}]
    vm = _FakeProvider(fresh=[], unreachable=[])
    monkeypatch.setattr(sync, "PROVIDERS", {"vmware": vm})
    monkeypatch.setattr(sync, "LOCAL_NET_CLOUDS", {"vmware"})

    out = sync._scan_local_net(prev)
    assert out == []
