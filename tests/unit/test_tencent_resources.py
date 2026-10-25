"""腾讯云四类只读资源（域名/DNSPod · 云数据库 MySQL/PostgreSQL · 云函数 SCF · CAM 子用户）—— 离线、零云调用。

_tc_json 全桩喂假 JSON（字段名照 tccli 自带 API schema），断言 argv 与归一化行结构；
另两条用例桩 subprocess.run，覆盖「exit 0 但 JSON 里带 Response.Error」与「没装 tccli」——两者都必须返回空列表而不是抛异常。
"""
import subprocess as sp

import pytest

from noc_ops.providers import PROVIDERS, tencent

pytestmark = pytest.mark.unit

CFG = {"account": "100000000001", "ssh_user": "ubuntu", "ssh_key": "tencent",
       "regions": {"ap-nanjing": ("Nanjing", "🇨🇳"), "ap-hongkong": ("Hong Kong", "🇭🇰")}}


def _setup(monkeypatch, responses):
    """responses：{(action, region): dict | callable(args) | None}，无 region 的全局产品 key 为 (action, "")。记录每次 argv。"""
    calls = []

    def fake(args, timeout=60, on_error=None):
        action = " ".join(args[:2])
        region = args[args.index("--region") + 1] if "--region" in args else ""
        calls.append(list(args))
        v = responses.get((action, region), responses.get(action, "MISSING"))
        if v == "MISSING":
            raise AssertionError(f"unexpected tccli call: {args}")
        if callable(v):
            v = v(args)
        if v is None and on_error is not None:
            on_error(action, "stubbed failure")
        return v
    monkeypatch.setattr(tencent, "_tc_json", fake)
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: CFG if name == "tencent" else {})
    return calls


@pytest.fixture
def today_2026_07_15(monkeypatch):
    class FakeDT(tencent.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 15, tzinfo=tz)
    monkeypatch.setattr(tencent, "datetime", FakeDT)


# —— 域名 ——
def test_domains_paged_and_merged_with_dnspod(monkeypatch, today_2026_07_15):
    def domain_pages(args):
        off = int(args[args.index("--Offset") + 1])
        assert args[args.index("--Limit") + 1] == "100"
        if off == 0:   # 第一页整整 100 条（2 条有料 + 98 条填充）→ 翻页；第二页 1 条收口
            filler = [{"DomainName": f"filler{i}.com", "Tld": "com", "AutoRenew": 1, "BuyStatus": "ok",
                       "CreationDate": "2024-01-01 00:00:00", "ExpirationDate": "2027-01-01 00:00:00"} for i in range(98)]
            return {"TotalCount": 101, "DomainSet": [
                {"DomainName": "Example.com", "Tld": "com", "AutoRenew": 1, "IsPremium": False, "BuyStatus": "ok",
                 "CreationDate": "2024-07-01 10:00:00", "ExpirationDate": "2026-08-14 10:00:00"},
                {"DomainName": "example.xyz", "Tld": "xyz", "AutoRenew": 0, "IsPremium": True, "BuyStatus": "AboutToExpire",
                 "CreationDate": "2025-07-20 10:00:00", "ExpirationDate": "2026-07-20 10:00:00"},
                *filler]}
        return {"TotalCount": 101, "DomainSet": [
            {"DomainName": "example.cn", "Tld": "cn", "AutoRenew": 1, "BuyStatus": "ok",
             "CreationDate": "2023-01-01 00:00:00", "ExpirationDate": "2027-01-01 00:00:00"}]}

    calls = _setup(monkeypatch, {
        "domain DescribeDomainNameList": domain_pages,
        "dnspod DescribeDomainList": {"DomainCountInfo": {"AllTotal": 2}, "DomainList": [
            {"Name": "example.com", "Status": "ENABLE", "DNSStatus": "", "RecordCount": 12, "IsVip": "no",
             "CreatedOn": "2024-07-01 10:00:00"},
            {"Name": "example.org", "Status": "PAUSE", "DNSStatus": "DNSERROR", "RecordCount": 3, "IsVip": "yes",
             "GradeTitle": "专业版", "CreatedOn": "2025-01-02 03:04:05"},
        ]},
    })
    rows = PROVIDERS["tencent"].list_domains()
    # 只读 argv：两类都是 Describe*，且分页参数在
    assert [c[:2] for c in calls] == [["domain", "DescribeDomainNameList"]] * 2 + [["dnspod", "DescribeDomainList"]]
    assert calls[1][calls[1].index("--Offset") + 1] == "100"
    assert calls[2][calls[2].index("--Limit") + 1] == "3000"
    by = {r["domain"]: r for r in rows}
    # 注册域名 101 条（Example.com 小写化）+ 只在 DNSPod 的 example.org 一条；example.com 两边都有只出注册行
    assert len(rows) == 102 and len(by) == 102
    assert sorted(k for k in by if not k.startswith("filler")) == ["example.cn", "example.com", "example.org", "example.xyz"]
    com = by["example.com"]
    assert com == {"domain": "example.com", "type": "gTLD", "registrar": "腾讯云", "account": "100000000001",
                   "reg_date": "2024-07-01", "expire_date": "2026-08-14", "days_left": 30,
                   "note": "自动续费 ✓ · 12 条记录"}
    xyz = by["example.xyz"]
    assert xyz["type"] == ".xyz" and xyz["days_left"] == 5 and xyz["note"] == "未自动续费 ⚠ · 即将到期 ⚠ · 溢价域名"
    assert by["example.cn"]["type"] == "ccTLD" and by["example.cn"]["days_left"] == 170
    org = by["example.org"]
    assert org["type"] == "DNS zone" and org["expire_date"] == "" and org["days_left"] is None
    assert org["reg_date"] == "2025-01-02" and org["note"] == "3 条记录 · 解析已暂停 ⚠ · NS 未指向 DNSPod ⚠ · 套餐 专业版"


def test_domains_half_failure_keeps_other_half(monkeypatch, capsys):
    _setup(monkeypatch, {
        "domain DescribeDomainNameList": None,                      # 域名注册没开通 → 这半边空
        "dnspod DescribeDomainList": {"DomainList": [{"Name": "example.net", "Status": "ENABLE", "RecordCount": 1}]},
    })
    rows = PROVIDERS["tencent"].list_domains()
    assert [r["domain"] for r in rows] == ["example.net"] and rows[0]["type"] == "DNS zone"
    err = capsys.readouterr().err
    assert "100000000001 域名注册" in err and err.count("⚠️") == 1


# —— 数据库 ——
CDB = {"InstanceId": "cdb-abc123", "InstanceName": "app-mysql", "EngineVersion": "8.0", "EngineType": "InnoDB",
       "Cpu": 2, "Memory": 4000, "Volume": 100, "DiskType": "CLOUD_SSD", "DeviceType": "UNIVERSAL",
       "Status": 1, "InstanceType": 1, "Vip": "10.0.0.5", "Vport": 3306, "WanStatus": 1,
       "WanDomain": "example-cdb.example.com", "WanPort": 25555, "CreateTime": "2025-03-01 12:00:00"}
PG = {"DBInstanceId": "postgres-xyz789", "DBInstanceName": "app-pg", "DBVersion": "15.1", "DBMajorVersion": "15",
      "DBEngine": "postgresql", "DBInstanceCpu": 1, "DBInstanceMemory": 2, "DBInstanceStorage": 50,
      "DBInstanceStorageType": "CLOUD_PREMIUM", "DBInstanceClass": "cdb.pg.z1.2g", "DBInstanceStatus": "running",
      "DBInstanceType": "readonly", "CreateTime": "2025-04-05 06:07:08",
      "DBInstanceNetInfo": [{"Address": "", "Ip": "203.0.113.9", "Port": 5432, "NetType": "public"},
                            {"Address": "pg.example.internal", "Ip": "10.0.0.9", "Port": 5432, "NetType": "private"}]}


def test_databases_per_region(monkeypatch):
    calls = _setup(monkeypatch, {
        ("cdb DescribeDBInstances", "ap-nanjing"): {"TotalCount": 1, "Items": [CDB]},
        ("cdb DescribeDBInstances", "ap-hongkong"): {"TotalCount": 0, "Items": []},
        ("postgres DescribeDBInstances", "ap-nanjing"): {"TotalCount": 0, "DBInstanceSet": []},
        ("postgres DescribeDBInstances", "ap-hongkong"): {"TotalCount": 1, "DBInstanceSet": [PG]},
    })
    rows = PROVIDERS["tencent"].list_databases()
    assert sorted((c[0], c[c.index("--region") + 1]) for c in calls) == [
        ("cdb", "ap-hongkong"), ("cdb", "ap-nanjing"), ("postgres", "ap-hongkong"), ("postgres", "ap-nanjing")]
    assert all("--Offset" in c and "--Limit" in c for c in calls)
    assert len(rows) == 2
    pg, my = rows                                              # 排序：region → ap-hongkong 在 ap-nanjing 前
    assert my == {"name": "app-mysql", "id": "cdb-abc123", "cloud": "tencent", "account": "100000000001",
                  "region": "ap-nanjing", "region_display": "Nanjing", "flag": "🇨🇳",
                  "engine": "MySQL 8.0", "class": "2C4G UNIVERSAL", "storage": "100GB CLOUD_SSD",
                  "state": "running", "endpoint": "10.0.0.5:3306", "created": "2025-03-01",
                  "note": "外网 example-cdb.example.com:25555"}
    assert pg["region_display"] == "Hong Kong" and pg["flag"] == "🇭🇰"
    assert pg["engine"] == "PostgreSQL 15.1" and pg["class"] == "1C2G" and pg["storage"] == "50GB CLOUD_PREMIUM"
    assert pg["state"] == "running" and pg["endpoint"] == "pg.example.internal:5432"     # 优先内网地址
    assert pg["note"] == "只读实例" and pg["created"] == "2025-04-05"


def test_databases_cdb_numeric_state_and_isolated(monkeypatch):
    _setup(monkeypatch, {
        "cdb DescribeDBInstances": {"TotalCount": 1, "Items": [{**CDB, "Status": 5, "InstanceType": 3, "WanStatus": 0, "Vip": None}]},
        "postgres DescribeDBInstances": {"TotalCount": 0, "DBInstanceSet": []},
    })
    rows = PROVIDERS["tencent"].list_databases()
    assert len(rows) == 2 and all(r["state"] == "isolated" and r["endpoint"] == "—" and r["note"] == "只读实例" for r in rows)


def test_databases_product_failure_warns_once_per_kind(monkeypatch, capsys):
    _setup(monkeypatch, {"cdb DescribeDBInstances": None, "postgres DescribeDBInstances": None})
    assert PROVIDERS["tencent"].list_databases() == []
    err = capsys.readouterr().err
    assert err.count("⚠️") == 2 and "云数据库 MySQL" in err and "云数据库 PostgreSQL" in err      # 两个区域各失败 → 每类只提示一次


# —— 云函数 ——
def test_serverless_namespaces_functions_and_detail(monkeypatch):
    calls = _setup(monkeypatch, {
        ("scf ListNamespaces", "ap-nanjing"): {"TotalCount": 2, "Namespaces": [{"Name": "default"}, {"Name": "prod"}]},
        ("scf ListNamespaces", "ap-hongkong"): None,                                   # 失败 → 退回 default
        ("scf ListFunctions", "ap-nanjing"): lambda a: {"TotalCount": 1, "Functions": [
            {"FunctionName": f"fn-{a[a.index('--Namespace') + 1]}", "FunctionId": f"lam-{a[a.index('--Namespace') + 1]}",
             "Runtime": "Python3.10", "Status": "Active", "Type": "Event", "ModTime": "2026-06-01 09:00:00"}]},
        ("scf ListFunctions", "ap-hongkong"): {"TotalCount": 0, "Functions": []},
        ("scf GetFunction", "ap-nanjing"): lambda a: (
            None if a[a.index("--Namespace") + 1] == "prod"                          # 详情拿不到 → None，不影响出行
            else {"MemorySize": 256, "Timeout": 30, "CodeSize": 2 * 1048576 + 1}),
    })
    rows = PROVIDERS["tencent"].list_serverless()
    assert [(r["namespace"], r["name"]) for r in rows] == [("default", "fn-default"), ("prod", "fn-prod")]
    d = rows[0]
    assert d == {"name": "fn-default", "id": "lam-default", "namespace": "default", "cloud": "tencent",
                 "account": "100000000001", "region": "ap-nanjing", "region_display": "Nanjing", "flag": "🇨🇳",
                 "runtime": "Python3.10", "memory": 256, "timeout": 30, "code_mb": 2.0,
                 "last_modified": "2026-06-01", "state": "Active"}
    assert rows[1]["memory"] is None and rows[1]["timeout"] is None and rows[1]["code_mb"] is None
    gets = [c for c in calls if c[1] == "GetFunction"]
    assert len(gets) == 2 and all("--FunctionName" in c and "--Namespace" in c for c in gets)
    hk = [c for c in calls if c[1] == "ListFunctions" and "ap-hongkong" in c]
    assert len(hk) == 1 and hk[0][hk[0].index("--Namespace") + 1] == "default"


# —— CAM ——
def test_iam_users_and_keys(monkeypatch, today_2026_07_15):
    calls = _setup(monkeypatch, {
        "cam ListUsers": {"Data": [
            {"Uin": 100000000002, "Name": "deployer", "Uid": 1, "Remark": "CI 发布", "ConsoleLogin": 0,
             "CreateTime": "2024-01-10 08:00:00"},
            {"Uin": 100000000003, "Name": "alice", "Uid": 2, "Remark": "", "ConsoleLogin": 1,
             "CreateTime": "2026-07-01 08:00:00"},
        ]},
        "cam ListAccessKeys": lambda a: {"AccessKeys": [
            {"AccessKeyId": "AKIDexample1", "Status": "Active", "CreateTime": "2025-07-15 00:00:00"},
            {"AccessKeyId": "AKIDexample2", "Status": "Inactive", "CreateTime": "2026-07-01 00:00:00"},
        ]} if a[a.index("--TargetUin") + 1] == "100000000002" else {"AccessKeys": []},
    })
    rows = PROVIDERS["tencent"].list_iam()
    assert [c[:2] for c in calls] == [["cam", "ListUsers"], ["cam", "ListAccessKeys"], ["cam", "ListAccessKeys"]]
    assert sorted(c[c.index("--TargetUin") + 1] for c in calls[1:]) == ["100000000002", "100000000003"]
    assert [r["name"] for r in rows] == ["alice", "deployer"]
    dep = rows[1]
    assert dep == {"name": "deployer", "id": "100000000002", "cloud": "tencent", "account": "100000000001",
                   "created": "2024-01-10", "last_used": "—", "key_count": 2, "oldest_key_days": 365,
                   "detail": "CI 发布 · 1 把密钥已停用"}
    assert rows[0]["key_count"] == 0 and rows[0]["oldest_key_days"] is None and rows[0]["detail"] == "可登录控制台"


# —— 失败模式：都得返回空列表 + 一条 stderr 提示，绝不抛 ——
class _R:
    def __init__(self, code, out, err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_exit0_with_error_json_is_failure(monkeypatch, capsys):
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: CFG)
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        assert argv[0] == "tccli" and argv[-2:] == ["--output", "json"] and argv[2].startswith(("Describe", "List", "Get"))
        return _R(0, '{"Response": {"Error": {"Code": "UnauthorizedOperation", "Message": "no permission"}, "RequestId": "r-1"}}')
    monkeypatch.setattr(sp, "run", run)
    p = PROVIDERS["tencent"]
    assert p.list_domains() == [] and p.list_databases() == [] and p.list_serverless() == [] and p.list_iam() == []
    err = capsys.readouterr().err
    assert "UnauthorizedOperation" in err and "未开通/无权限" in err
    # 域名注册 + DNSPod + MySQL + PostgreSQL + SCF + CAM：每类一条，不随区域数翻倍
    assert err.count("⚠️") == 6
    assert len(seen) >= 8                                      # 2 全局 + 2 区域 × (cdb + pg + scf ListNamespaces) + cam
    # 直接喂裸 Error（无 Response 外层）同样算失败
    monkeypatch.setattr(sp, "run", lambda *a, **k: _R(0, '{"Error": {"Code": "FailedOperation.ServiceNotActivated", "Message": ""}}'))
    assert tencent._tc_json(["scf", "ListFunctions"]) is None


def test_cli_missing_returns_empty(monkeypatch, capsys):
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: CFG)

    def run(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "tccli")
    monkeypatch.setattr(sp, "run", run)
    p = PROVIDERS["tencent"]
    assert p.list_domains() == [] and p.list_databases() == [] and p.list_serverless() == [] and p.list_iam() == []
    err = capsys.readouterr().err
    assert "tccli" in err and "未安装 tccli" in err and err.count("⚠️") == 6


def test_unconfigured_cloud_makes_no_calls(monkeypatch):
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: {})
    monkeypatch.setattr(sp, "run", lambda *a, **k: pytest.fail("tccli must not be called when [tencent] is absent"))
    p = PROVIDERS["tencent"]
    assert p.list_domains() == [] and p.list_databases() == [] and p.list_serverless() == [] and p.list_iam() == []


def test_paged_dedupes_when_offset_treated_as_page_number(monkeypatch):
    """某些产品把 Offset 当页码：第二页重复返回同一批 → 按 id 去重并收口，不会死循环/重复出行。"""
    pages = {"0": [{"Id": str(i)} for i in range(2)], "2": [{"Id": "0"}, {"Id": "1"}]}

    def fake(args, timeout=60, on_error=None):
        return {"TotalCount": 5, "Items": pages[args[args.index("--Offset") + 1]]}
    monkeypatch.setattr(tencent, "_tc_json", fake)
    assert [x["Id"] for x in tencent._paged(["x", "DescribeY"], "Items", page_size=2, id_key="Id")] == ["0", "1"]


def test_tc_json_unwraps_response_and_reports_sdk_exception(monkeypatch, capsys):
    monkeypatch.setattr(sp, "run", lambda *a, **k: _R(0, '{"Response": {"Data": [], "RequestId": "r"}}'))
    assert tencent._tc_json(["cam", "ListUsers"]) == {"Data": [], "RequestId": "r"}
    monkeypatch.setattr(sp, "run", lambda *a, **k: _R(255, "", "[TencentCloudSDKException] code:InvalidParameterValue message:bad region\n"))
    once = tencent._WarnOnce("100000000001", "云函数 SCF")
    assert tencent._tc_json(["scf", "ListFunctions", "--region", "ap-nowhere"], on_error=once) is None
    assert tencent._tc_json(["scf", "ListFunctions", "--region", "ap-nowhere"], on_error=once) is None
    err = capsys.readouterr().err
    assert err.count("⚠️") == 1 and "InvalidParameterValue" in err and "云函数 SCF" in err
