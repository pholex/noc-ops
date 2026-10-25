"""阿里云 RAM 用户 / RDS+PolarDB 数据库（只读）—— 离线、零云调用，monkeypatch 掉 _ali_json。"""
from datetime import datetime, timedelta, timezone

import pytest

from noc_ops.aws_safety import is_read_only_aliyun
from noc_ops.providers import PROVIDERS, aliyun

pytestmark = pytest.mark.unit

ACCOUNTS = [{"profile": "acme", "account": "123456789012",
             "regions": {"cn-hangzhou": ("Hangzhou", "🇨🇳"), "cn-shenzhen": ("Shenzhen", "🇨🇳")}},
            {"profile": "acme-dns", "account": "210987654321"}]       # 只有域名的账号：无 regions → 不扫数据库


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _action(args):
    return " ".join(a for a in args[:2])


def _opt(args, name):
    return args[args.index(name) + 1] if name in args else None


@pytest.fixture
def ali(monkeypatch):
    monkeypatch.setattr(type(PROVIDERS["aliyun"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return PROVIDERS["aliyun"]


def test_list_iam_pagination_keys_and_last_login(ali, monkeypatch):
    calls = []

    def fake(account, args, timeout=60, quiet=False):
        calls.append((account["profile"], list(args), quiet))
        act = _action(args)
        if act == "ram ListUsers":
            assert _opt(args, "--MaxItems") == "100"
            if account["profile"] == "acme-dns":
                return None                                        # 该账号缺 ram:ListUsers → 整账号为空
            if _opt(args, "--Marker") is None:                     # 第一页：截断，给 Marker
                return {"IsTruncated": True, "Marker": "m2",
                        "Users": {"User": [{"UserName": "alice", "CreateDate": "2025-01-23T06:51:36Z"}]}}
            assert _opt(args, "--Marker") == "m2"
            return {"IsTruncated": False,
                    "Users": {"User": [{"UserName": "bot", "CreateDate": "2026-05-03T17:44:36Z"}]}}
        if act == "ram ListAccessKeys":
            assert quiet is True                                   # 扇出调用静默，由 list_iam 汇总
            if _opt(args, "--UserName") == "bot":
                return {"AccessKeys": {"AccessKey": [
                    {"AccessKeyId": "LTAI-redacted-1", "Status": "Active", "CreateDate": _iso(174)},
                    {"AccessKeyId": "LTAI-redacted-2", "Status": "Inactive", "CreateDate": _iso(30)}]}}
            return {"AccessKeys": {"AccessKey": []}}
        if act == "ram GetUser":
            assert quiet is True
            u = _opt(args, "--UserName")
            return {"User": {"UserName": u, "LastLoginDate": "2026-07-24T01:02:03Z" if u == "alice" else ""}}
        raise AssertionError(f"unexpected call {args}")
    monkeypatch.setattr(aliyun, "_ali_json", fake)

    rows = ali.list_iam()
    assert [r["name"] for r in rows] == ["alice", "bot"]            # 两页合并，按账号/名字排序
    alice, bot = rows
    assert alice == {"name": "alice", "cloud": "aliyun", "account": "123456789012", "created": "2025-01-23",
                     "last_used": "2026-07-24", "key_count": 0, "oldest_key_days": None}
    assert bot["last_used"] == "—" and bot["key_count"] == 2       # 从没登录 → —；Inactive 也计入密钥数（与 AWS 同款）
    assert bot["oldest_key_days"] in (174, 175)                    # 跨日边界允许 ±1
    assert set(bot) == set(alice) and all(r["cloud"] == "aliyun" for r in rows)
    # 每账号一次 ListUsers（acme 两页 + acme-dns 一次失败），每用户各一次 ListAccessKeys / GetUser
    acts = [(p, _action(a)) for p, a, _ in calls]
    assert acts.count(("acme", "ram ListUsers")) == 2 and acts.count(("acme-dns", "ram ListUsers")) == 1
    assert acts.count(("acme", "ram ListAccessKeys")) == 2 and acts.count(("acme", "ram GetUser")) == 2
    assert all("--profile" not in a for _, a, _ in calls)          # profile 由 _ali_json 统一追加，调用方不拼


def test_list_iam_detail_failures_summarized_once(ali, monkeypatch, capsys):
    def fake(account, args, timeout=60, quiet=False):
        if _action(args) == "ram ListUsers":
            if account["profile"] != "acme":
                return {"IsTruncated": False, "Users": {"User": []}}
            return {"IsTruncated": False, "Users": {"User": [{"UserName": f"u{i}", "CreateDate": "2026-01-01T00:00:00Z"}
                                                             for i in range(5)]}}
        return None                                                # ListAccessKeys / GetUser 全缺权限
    monkeypatch.setattr(aliyun, "_ali_json", fake)
    rows = ali.list_iam()
    assert len(rows) == 5 and all(r["key_count"] == 0 and r["oldest_key_days"] is None and r["last_used"] == "—" for r in rows)
    err = capsys.readouterr().err
    assert err.count("⚠️ 阿里云 123456789012 RAM") == 1 and "10 次" in err   # 5 用户 × 2 调用，只汇总一行


def test_list_databases_rds_and_polardb(ali, monkeypatch):
    calls = []
    gib = 2 ** 30

    def fake(account, args, timeout=60, quiet=False):
        calls.append((account["profile"], list(args), quiet))
        act, region = _action(args), _opt(args, "--RegionId")
        assert account["profile"] == "acme"                        # 无 regions 的账号根本不该被扫
        if act == "rds DescribeDBInstances":
            assert _opt(args, "--PageSize") == "100"
            if region != "cn-hangzhou":
                return {"TotalRecordCount": 0, "PageRecordCount": 0, "Items": {"DBInstance": []}}
            page = _opt(args, "--PageNumber")
            if page == "1":                                        # 两页：TotalRecordCount=2，每页 1 条
                return {"TotalRecordCount": 2, "PageRecordCount": 1, "Items": {"DBInstance": [
                    {"DBInstanceId": "rm-aaa", "DBInstanceDescription": "订单库", "Engine": "MySQL", "EngineVersion": "8.0",
                     "DBInstanceClass": "mysql.n2.medium.1", "DBInstanceStatus": "Running",
                     "CreateTime": "2025-03-01T08:00:00Z"}]}}
            assert page == "2"
            return {"TotalRecordCount": 2, "PageRecordCount": 1, "Items": {"DBInstance": [
                {"DBInstanceId": "rm-bbb", "Engine": "PostgreSQL", "EngineVersion": "14.0",
                 "DBInstanceStatus": "Stopped"}]}}
        if act == "rds DescribeDBInstanceAttribute":
            assert quiet is True and region == "cn-hangzhou"
            assert _opt(args, "--DBInstanceId") == "rm-aaa,rm-bbb"   # 批量：逗号拼
            return {"Items": {"DBInstanceAttribute": [
                {"DBInstanceId": "rm-aaa", "DBInstanceStorage": 100, "DBInstanceStorageType": "cloud_essd",
                 "ConnectionString": "rm-aaa.mysql.rds.aliyuncs.com", "Port": "3306", "DBInstanceClass": "mysql.n2.medium.1"},
                {"DBInstanceId": "rm-bbb", "DBInstanceStorage": 20, "DBInstanceStorageType": "cloud_essd",
                 "ConnectionString": "rm-bbb.pg.rds.aliyuncs.com", "Port": "5432", "DBInstanceClass": "pg.n2.small.1"}]}}
        if act == "polardb DescribeDBClusters":
            if region != "cn-shenzhen":
                return {"TotalRecordCount": 0, "PageRecordCount": 0, "Items": {"DBCluster": []}}
            return {"TotalRecordCount": 1, "PageRecordCount": 1, "Items": {"DBCluster": [
                {"DBClusterId": "pc-ccc", "DBClusterDescription": "分析库", "DBType": "MySQL", "DBVersion": "8.0",
                 "DBNodeClass": "polar.mysql.x4.medium", "DBNodeNumber": 2, "DBClusterStatus": "Running",
                 "StorageUsed": int(12.3 * gib), "CreateTime": "2025-06-01T00:00:00Z"}]}}
        if act == "polardb DescribeDBClusterEndpoints":
            assert quiet is True and _opt(args, "--DBClusterId") == "pc-ccc"
            return {"Items": [
                {"EndpointType": "Primary", "AddressItems": [{"NetType": "Private", "ConnectionString": "pc-ccc-primary.rwlb.rds.aliyuncs.com", "Port": "3306"}]},
                {"EndpointType": "Cluster", "AddressItems": [
                    {"NetType": "Public", "ConnectionString": "pc-ccc.rwlb.rds.aliyuncs.com", "Port": "3306"},
                    {"NetType": "Private", "ConnectionString": "pc-ccc.rwlb.rds.aliyuncs.com.internal", "Port": "3306"}]}]}
        raise AssertionError(f"unexpected call {args}")
    monkeypatch.setattr(aliyun, "_ali_json", fake)

    rows = ali.list_databases()
    assert [(r["region"], r["name"]) for r in rows] == [("cn-hangzhou", "rm-bbb"), ("cn-hangzhou", "订单库"), ("cn-shenzhen", "分析库")]
    a, b, c = rows[1], rows[0], rows[2]
    required = {"name", "cloud", "account", "region", "region_display", "flag", "engine", "class", "storage", "state", "endpoint"}
    assert all(required <= set(r) for r in rows) and all(r["cloud"] == "aliyun" and r["account"] == "123456789012" for r in rows)
    # RDS：引擎 + 版本、规格/存储/端口来自 DescribeDBInstanceAttribute，状态小写
    assert a["engine"] == "MySQL 8.0" and a["class"] == "mysql.n2.medium.1" and a["storage"] == "100 GB cloud_essd"
    assert a["state"] == "running" and a["endpoint"] == "rm-aaa.mysql.rds.aliyuncs.com:3306"
    assert a["region_display"] == "Hangzhou" and a["flag"] == "🇨🇳" and a["created"] == "2025-03-01"
    assert b["name"] == "rm-bbb" and b["state"] == "stopped" and b["class"] == "pg.n2.small.1"   # 无描述 → 用 ID
    # PolarDB：引擎前缀 PolarDB、节点数、已用存储、Cluster 内网地址优先
    assert c["engine"] == "PolarDB MySQL 8.0" and c["class"] == "polar.mysql.x4.medium ×2"
    assert c["storage"] == "12 GB 已用" and c["state"] == "running" and c["region_display"] == "Shenzhen"
    assert c["endpoint"] == "pc-ccc.rwlb.rds.aliyuncs.com.internal:3306"
    # 每区域各一次列表调用（hangzhou 的 RDS 翻两页），只有有实例的区域才打明细调用
    acts = [(_opt(a_, "--RegionId"), _action(a_)) for _, a_, _ in calls]
    assert acts.count(("cn-hangzhou", "rds DescribeDBInstances")) == 2
    assert acts.count(("cn-shenzhen", "rds DescribeDBInstances")) == 1
    assert acts.count(("cn-hangzhou", "polardb DescribeDBClusters")) == acts.count(("cn-shenzhen", "polardb DescribeDBClusters")) == 1
    assert acts.count(("cn-hangzhou", "rds DescribeDBInstanceAttribute")) == 1
    assert acts.count(("cn-shenzhen", "polardb DescribeDBClusterEndpoints")) == 1
    # 全部是只读动作（aws_safety 的阿里云分类器也认）
    assert all(is_read_only_aliyun(" ".join(a_)) for _, a_, _ in calls)


def test_list_databases_missing_cli_or_denied_is_empty_not_crash(ali, monkeypatch, capsys):
    def fake(account, args, timeout=60, quiet=False):
        if not quiet:
            print(f"  ⚠️ 阿里云 {account['account']} {_action(args)}：Forbidden.RAM", file=__import__('sys').stderr)
        return None
    monkeypatch.setattr(aliyun, "_ali_json", fake)
    assert ali.list_databases() == []
    err = capsys.readouterr().err
    assert err.count("⚠️ 阿里云 123456789012") == 2               # 一次原因（首个列表调用）+ 一次汇总，不按区域刷屏
    assert "3 次 RDS/PolarDB" in err                               # 2 区域 × 2 产品 = 4 次失败，其中 1 次已响亮报过

    def no_cli(account, args, timeout=60, quiet=False):
        return None                                                # 等价于 FileNotFoundError 路径（_ali_json 内部已吞掉）
    monkeypatch.setattr(aliyun, "_ali_json", no_cli)
    assert ali.list_iam() == [] and ali.list_databases() == []


def test_ali_json_quiet_suppresses_stderr(monkeypatch, capsys):
    import subprocess

    class R:
        returncode, stdout, stderr = 0, '{"schemaVersion":"v1","message":"no permission","error_code":"Forbidden.RAM","status_code":403}', ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    acct = {"profile": "acme", "account": "123456789012"}
    assert aliyun._ali_json(acct, ["ram", "ListUsers"]) is None
    assert "Forbidden.RAM" in capsys.readouterr().err
    assert aliyun._ali_json(acct, ["ram", "ListUsers"], quiet=True) is None
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("aliyun")))
    assert aliyun._ali_json(acct, ["ram", "ListUsers"]) is None    # CLI 没装：None，不抛
    assert "跑 aliyun 失败" in capsys.readouterr().err
