"""Tier 1 单元测试：网络域切分（lan vs tailscale）。

核心不变量（2026-07-24 沉淀，起因：`ops sync --local` 被一台离线的 Tailscale 群晖
拖住 30s，且在家跑 --local 时内网探针不通会连累 Tailscale 机器一起被跳过）：
- 一个端点属于哪个网络域可从地址自动推导（100.64/10、*.ts.net → tailscale）；
- `refresh_net(域)` **只替换本域**的记录，另一个域和云账号的记录原样保留；
- 门控分域：lan 看内网探针，tailscale 看 `tailscale status`；
- Tailscale 节点离线时 endpoint_alive 直接判死，不去吃 HTTP 超时。
"""
import json

import pytest

from noc_ops import netprobe, sync

pytestmark = pytest.mark.unit


# ─── 网络域推导 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("addr,expect", [
    ("https://100.64.0.8:5001", netprobe.NET_TAILSCALE),   # Tailscale CGNAT 段
    ("100.64.0.1",                 netprobe.NET_TAILSCALE),   # 段首
    ("100.127.255.254",            netprobe.NET_TAILSCALE),   # 段尾
    ("https://sample-resource-05.taile1013d.ts.net:5001", netprobe.NET_TAILSCALE),  # MagicDNS
    ("https://192.0.2.20",        netprobe.NET_LAN),
    ("10.0.0.107:4430",       netprobe.NET_LAN),
    ("100.200.0.1",                netprobe.NET_LAN),         # 100.x 但在 CGNAT 段外
    ("",                           netprobe.NET_LAN),
])
def test_network_of(addr, expect):
    assert netprobe.network_of(addr) == expect


def test_hostport_of():
    assert netprobe.hostport_of("https://100.64.0.8:5001") == ("100.64.0.8", 5001)
    assert netprobe.hostport_of("https://192.0.2.20") == ("192.0.2.20", 443)
    assert netprobe.hostport_of("192.168.1.5", default_port=22) == ("192.168.1.5", 22)


# ─── 离线 Tailscale 节点：0 网络等待判死 ──────────────────────────────────────
def test_offline_tailscale_peer_short_circuits(monkeypatch):
    """对端 Online=False → endpoint_alive 直接 False，不该再去 TCP 连接（那会吃满超时）。"""
    monkeypatch.setattr(netprobe, "_TS_QUERIED", True)
    monkeypatch.setattr(netprobe, "_TS_STATUS", {
        "BackendState": "Running",
        "Peer": {"n1": {"DNSName": "sample-resource-05.taile1013d.ts.net.",
                        "TailscaleIPs": ["100.64.0.8"], "Online": False,
                        "LastSeen": "2026-07-23T10:00:00Z"}},
    })
    monkeypatch.setattr(netprobe, "_tailscale_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(netprobe, "tcp_alive",
                        lambda *a, **k: pytest.fail("离线节点不该再发起 TCP 连接"))

    ok, why = netprobe.endpoint_alive("https://100.64.0.8:5001", default_port=5001)
    assert ok is False
    assert "离线" in why and "sample-resource-05" in why


def test_tailnet_down_blocks_whole_domain(monkeypatch):
    monkeypatch.setattr(netprobe, "_TS_QUERIED", True)
    monkeypatch.setattr(netprobe, "_TS_STATUS", {"BackendState": "Stopped"})
    monkeypatch.setattr(netprobe, "_tailscale_bin", lambda: "/usr/bin/true")
    ok, why = netprobe.tailnet_up()
    assert ok is False and "Stopped" in why


def test_online_tailscale_peer_falls_through_to_tcp(monkeypatch):
    monkeypatch.setattr(netprobe, "_TS_QUERIED", True)
    monkeypatch.setattr(netprobe, "_TS_STATUS", {
        "BackendState": "Running",
        "Peer": {"n1": {"DNSName": "nas.taile1013d.ts.net.",
                        "TailscaleIPs": ["100.64.0.8"], "Online": True}},
    })
    monkeypatch.setattr(netprobe, "_tailscale_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(netprobe, "tcp_alive", lambda *a, **k: True)
    assert netprobe.endpoint_alive("https://100.64.0.8:5001", default_port=5001) == (True, "")


# ─── refresh_net 只动本域 ─────────────────────────────────────────────────────
def _prev():
    return [
        {"cloud": "aws", "profile": "111", "region": "ap-northeast-1", "name": "ec2-a"},
        {"cloud": "vmware", "profile": "VMware ESX", "region": "本地网络", "name": "vm-a",
         "_acct": "vmware", "network": "lan", "private_ip": "192.0.2.31"},
        {"cloud": "synology", "profile": "Synology NAS", "region": "Tailscale", "name": "sample-resource-05",
         "_acct": "sample-resource-05", "network": "tailscale", "private_ip": "100.64.0.8"},
    ]


def _run_refresh(monkeypatch, tmp_path, network, scanned, gate=(True, "ok")):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"instances": _prev(), "totals": {"instances": 3}}))
    monkeypatch.setattr(sync, "NOC_DATA", data_file)
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync, "_account_networks", lambda: {})
    monkeypatch.setattr(sync, "_net_gate", lambda n: gate)
    monkeypatch.setattr(sync, "write_local_ssh_config", lambda recs: None)
    seen = {}

    def _scan(prev_scope, network=None):
        seen["prev"] = prev_scope
        seen["network"] = network
        return scanned

    monkeypatch.setattr(sync, "_scan_local_net", _scan)
    rc = sync.refresh_net(network, quiet=True)
    return rc, json.loads(data_file.read_text())["instances"], seen


def test_refresh_lan_leaves_tailscale_untouched(monkeypatch, tmp_path):
    fresh_lan = [{"cloud": "vmware", "profile": "VMware ESX", "region": "本地网络",
                  "name": "vm-a-new", "_acct": "vmware", "network": "lan",
                  "private_ip": "192.0.2.31"}]
    rc, insts, seen = _run_refresh(monkeypatch, tmp_path, "lan", fresh_lan)
    names = {i["name"] for i in insts}

    assert rc == 0
    assert seen["network"] == "lan"
    assert {i["name"] for i in seen["prev"]} == {"vm-a"}      # 只把本域旧记录喂给扫描
    assert "vm-a-new" in names and "vm-a" not in names        # 本域被替换
    assert "sample-resource-05" in names                               # Tailscale 域原样保留
    assert "ec2-a" in names                                   # 云账号原样保留


def test_refresh_tailscale_leaves_lan_untouched(monkeypatch, tmp_path):
    fresh_ts = [{"cloud": "synology", "profile": "Synology NAS", "region": "Tailscale",
                 "name": "sample-resource-05-new", "_acct": "sample-resource-05", "network": "tailscale",
                 "private_ip": "100.64.0.8"}]
    rc, insts, seen = _run_refresh(monkeypatch, tmp_path, "tailscale", fresh_ts)
    names = {i["name"] for i in insts}

    assert rc == 0
    assert seen["network"] == "tailscale"
    assert {i["name"] for i in seen["prev"]} == {"sample-resource-05"}
    assert "sample-resource-05-new" in names and "sample-resource-05" not in names
    assert "vm-a" in names                                    # 内网域原样保留
    assert "ec2-a" in names


def test_gate_failure_carries_only_that_domain(monkeypatch, tmp_path):
    """门控没过（不在内网 / tailscaled 没跑）→ 本域整体沿用，另一域和云账号不受影响。"""
    rc, insts, seen = _run_refresh(monkeypatch, tmp_path, "lan", [], gate=(False, "不在内网"))
    assert rc == 0
    assert "prev" not in seen                                 # 门控没过就不该发起扫描
    assert {i["name"] for i in insts} == {"ec2-a", "vm-a", "sample-resource-05"}


def test_inst_network_backfills_legacy_records():
    """老 data.json 没有 network 字段：先查账号表，再退到按 IP 推导。"""
    acct_map = {("synology", "sample-resource-05"): "tailscale"}
    assert sync._inst_network({"cloud": "synology", "_acct": "sample-resource-05"}, acct_map) == "tailscale"
    assert sync._inst_network(
        {"cloud": "local", "_acct": None, "private_ip": "100.64.0.8"}, {}) == "tailscale"
    assert sync._inst_network(
        {"cloud": "local", "_acct": None, "private_ip": "192.0.2.31"}, {}) == "lan"
    assert sync._inst_network({"cloud": "vmware", "network": "lan"}, acct_map) == "lan"
