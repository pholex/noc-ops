"""VolcengineProvider：argv 拼装、读写分类、四类资源的归一化（_ve_json 全桩，不碰真 ve）。"""
import pytest

from noc_ops.providers import PROVIDERS, volcengine
from noc_ops import sync

pytestmark = pytest.mark.unit

ACCOUNTS = [{"account": "2100000001", "ssh_user": "root", "ssh_key": "volc",
             "regions": {"cn-beijing": ("Beijing", "🇨🇳"), "cn-hongkong": ("Hong Kong", "🇭🇰")}}]
INST = {"id": "i-abc", "region": "cn-beijing", "profile": "2100000001"}

ECS = {   # 按 volcengine-go-sdk InstanceForDescribeInstancesOutput 字段
    "InstanceId": "i-abc", "InstanceName": "web 01", "Status": "RUNNING", "ZoneId": "cn-beijing-a",
    "InstanceTypeId": "ecs.g3i.large", "Cpus": 2, "MemorySize": 8192, "KeyPairName": "kp-1",
    "EipAddress": {"AllocationId": "eip-1", "IpAddress": "1.2.3.4"},
    "NetworkInterfaces": [{"Type": "primary", "PrimaryIpAddress": "192.168.0.10"},
                          {"Type": "secondary", "PrimaryIpAddress": "192.168.0.11"}],
}


def _setup(monkeypatch, responses):
    calls = []

    def fake(account, args, timeout=60):
        key = " ".join(args[:2])
        region = args[args.index("--region") + 1] if "--region" in args else ""
        calls.append((key, region, tuple(args)))
        v = responses.get((key, region), responses.get(key))
        return v
    monkeypatch.setattr(volcengine, "_ve_json", fake)
    monkeypatch.setattr(type(PROVIDERS["volcengine"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    monkeypatch.setattr(volcengine, "resolve_keyfile", lambda acct, key: f"/k/{acct}/{key}.pem")
    return calls


def test_argv_and_classification():
    v = PROVIDERS["volcengine"]
    assert v.cli == "ve" and v.order == 3 and v.display == "火山引擎"
    assert v.reboot_argv(INST) == ["ve", "ecs", "RebootInstance", "--InstanceId", "i-abc", "--region", "cn-beijing"]
    assert v.status_argv(INST) == ["ve", "ecs", "DescribeInstances", "--InstanceIds.1", "i-abc", "--region", "cn-beijing"]
    assert v.is_read("ecs DescribeInstances --region cn-beijing")
    assert v.is_read("ve iam ListUsers --Limit 100")
    assert v.is_reboot("ecs RebootInstance --InstanceId i-abc --region cn-beijing")
    assert not v.is_read("ecs StopInstance --InstanceId i-abc --region cn-beijing")
    assert not v.is_reboot("ecs StopInstance --InstanceId i-abc --region cn-beijing")


def test_instances_paginated_and_normalized(monkeypatch):
    calls = _setup(monkeypatch, {
        ("ecs DescribeInstances", "cn-beijing"): {"Instances": [ECS], "NextToken": ""},
        ("ecs DescribeInstances", "cn-hongkong"): None,          # 该区失败 → 跳过，不影响其它区
    })
    rows = PROVIDERS["volcengine"].list_instances()
    assert len(rows) == 1
    r = rows[0]
    assert r["cloud"] == "volcengine" and r["profile"] == "2100000001" and r["region_display"] == "Beijing"
    assert r["name"] == "web 01" and r["id"] == "i-abc" and r["state"] == "running" and r["az"] == "cn-beijing-a"
    assert r["type"] == "ecs.g3i.large" and r["cpu_mem"] == "2C8G"
    assert r["private_ip"] == "192.168.0.10" and r["public_ip"] == "1.2.3.4"      # 主网卡 + EIP
    assert r["ssh_alias"] == "ve-web-01" and r["ssh_user"] == "root" and r["key_file"] == "/k/2100000001/volc.pem"
    assert r["connect"] == "direct" and r["verified"] is False
    assert [c[1] for c in calls] == ["cn-beijing", "cn-hongkong"]


def test_instances_follow_next_token(monkeypatch):
    seen = {"n": 0}

    def fake(account, args, timeout=60):
        if args[0] == "ecs" and args[2] == "--region" and args[3] == "cn-beijing":
            seen["n"] += 1
            if "--NextToken" in args:
                return {"Instances": [{**ECS, "InstanceId": "i-2", "InstanceName": "two"}], "NextToken": ""}
            return {"Instances": [ECS], "NextToken": "tok"}
        return None
    monkeypatch.setattr(volcengine, "_ve_json", fake)
    monkeypatch.setattr(type(PROVIDERS["volcengine"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    monkeypatch.setattr(volcengine, "resolve_keyfile", lambda a, k: "")
    assert [r["id"] for r in PROVIDERS["volcengine"].list_instances()] == ["i-abc", "i-2"] and seen["n"] == 2


def test_domains_serverless_iam(monkeypatch):
    _setup(monkeypatch, {
        "dns ListZones": {"Total": 1, "Zones": [{"ZID": 1, "ZoneName": "example.com", "RecordCount": 7,
                                                  "CreatedAt": 1710000000, "IsSubDomain": False}]},
        ("vefaas ListFunctions", "cn-beijing"): {"Items": [{"Id": "f-1", "Name": "hello", "Runtime": "python3.10/v1",
                                                            "MemoryMB": 512, "RequestTimeout": 30, "CodeSize": 2_500_000,
                                                            "LastUpdateTime": "2026-05-01T10:00:00Z"}], "Total": 1},
        ("vefaas ListFunctions", "cn-hongkong"): {"Items": [], "Total": 0},
        "iam ListUsers": {"Total": 1, "UserMetadata": [{"UserName": "000765", "DisplayName": "000765",
                                                         "Email": "u@example.com", "Status": "active",
                                                         "CreateDate": "20260312T023043Z"}]},
    })
    p = PROVIDERS["volcengine"]
    d = p.list_domains()
    assert d == [{"domain": "example.com", "type": "DNS zone", "registrar": "火山引擎", "account": "2100000001",
                  "reg_date": "2024-03-09", "expire_date": "", "days_left": None, "note": "7 条记录"}]
    f = p.list_serverless()
    assert len(f) == 1 and f[0]["name"] == "hello" and f[0]["runtime"] == "python3.10/v1"
    assert f[0]["memory"] == 512 and f[0]["timeout"] == 30 and f[0]["code_mb"] == 2.5 and f[0]["last_modified"] == "2026-05-01"
    u = p.list_iam()
    assert len(u) == 1 and u[0]["name"] == "000765" and u[0]["created"] == "2026-03-12"
    assert u[0]["key_count"] is None and u[0]["detail"] == "000765 · u@example.com · active"


def test_paged_body_apis(monkeypatch):
    """dns ListZones / vefaas ListFunctions 走 PageNumber 翻页，直到不足一页或达到 Total。"""
    pages = {}

    def fake(account, args, timeout=60):
        import json as _j
        body = _j.loads(args[args.index("--body") + 1])
        pages.setdefault(args[1], []).append(body["PageNumber"])
        if args[1] == "ListZones":
            n = body["PageNumber"]
            items = [{"ZoneName": f"z{n}-{i}.com", "RecordCount": 1} for i in range(100 if n < 3 else 5)]
            return {"Total": 205, "Zones": items}
        return {"Total": 0, "Items": []}
    monkeypatch.setattr(volcengine, "_ve_json", fake)
    monkeypatch.setattr(type(PROVIDERS["volcengine"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    assert len(PROVIDERS["volcengine"].list_domains()) == 205 and pages["ListZones"] == [1, 2, 3]
    assert PROVIDERS["volcengine"].list_serverless() == [] and pages["ListFunctions"] == [1, 1]   # 两个区各一页


def test_ve_json_nonzero_exit_is_failure(monkeypatch, capsys):
    """exit≠0 一律算失败返回 None（即使 stdout 是 JSON），否则 AK 过期会被当成「0 台实例」→ 误判删除。"""
    import subprocess as sp

    class R:
        returncode, stdout, stderr = 1, '{"Result": {"Instances": []}}', "error: expired credentials"
    monkeypatch.setattr(sp, "run", lambda *a, **k: R())
    assert volcengine._ve_json({"account": "x"}, ["ecs", "DescribeInstances", "--region", "cn-beijing"]) is None
    assert "exit 1" in capsys.readouterr().err


def test_billing_and_credits(monkeypatch):
    bodies = []

    def fake(account, args, timeout=60):
        import json as _j
        if args[1] == "ListBillOverviewByProd":
            body = _j.loads(args[args.index("--body") + 1]); bodies.append(body["BillPeriod"])
            if body["BillPeriod"].endswith("-09"):
                return {"Total": 2, "List": [
                    {"Product": "ark_bd", "ProductZh": "字节跳动大模型服务（豆包大模型）", "PayableAmount": "253.56",
                     "OriginalBillAmount": "253.638000", "Currency": "CNY"},
                    {"Product": "x", "ProductZh": "零元项", "PayableAmount": "0.00", "Currency": "CNY"}]}
            return {"Total": 0, "List": []}
        if args[1] == "QueryBalanceAcct":
            return {"AccountID": 2100000001, "ArrearsBalance": "0", "AvailableBalance": "651.36",
                    "CashBalance": "651.36", "CreditLimit": "0", "Currency": "CNY", "FreezeAmount": "0"}
        if args[1] == "ListBudget":
            return {"BudgetList": [], "Total": 0}
        return None
    monkeypatch.setattr(volcengine, "_ve_json", fake)
    monkeypatch.setattr(type(PROVIDERS["volcengine"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    p = PROVIDERS["volcengine"]
    rows = p.list_billing(months=3)
    assert len(bodies) == 3 and len(rows) == 1                     # 近 3 个月各查一次；0 元行剔除
    r = rows[0]
    assert r["cloud"] == "volcengine" and r["account"] == "2100000001" and r["service"].startswith("字节跳动大模型")
    assert r["amount"] == 253.56 and r["currency"] == "CNY" and r["month"].endswith("-09") and r["period_start"] == r["month"] + "-01"
    assert r["partial"] is False and r["fetched_at"]
    c = p.list_credits()
    assert len(c) == 1 and c[0]["credit"] == 651.36 and c[0]["credit_currency"] == "CNY"
    assert c[0]["state"] == "Normal" and c[0]["spending_limit"] == "On" and c[0]["budget"] == ""   # 没设预算→""（标「未设」）
    assert c[0]["free_tier"] is None and c[0]["plan"] == "Balance+PostPaid"


def test_credits_skipped_when_balance_unavailable(monkeypatch):
    monkeypatch.setattr(volcengine, "_ve_json", lambda *a, **k: None)
    monkeypatch.setattr(type(PROVIDERS["volcengine"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    assert PROVIDERS["volcengine"].list_credits() == [] and PROVIDERS["volcengine"].list_billing() == []


def test_sync_wiring():
    """火山引擎已接进排序表 / inventory 分区 / 按账号单刷。"""
    assert sync.CLOUD_RANK["volcengine"] == 3 and sync.CLOUD_RANK["gcp"] == 4
    assert sync.CLOUD_SECTION["volcengine"].startswith("火山引擎")
    assert sync.REGISTRAR_CLOUD["火山引擎"] == "volcengine"
    assert sync.ACCOUNT_SCOPED_KINDS["volcengine"] == ("instances", "domains", "databases", "storage", "serverless", "cdn", "iam", "billing", "credits")
