"""国内三云 CDN 加速域名只读纳管（阿里云 DescribeUserDomains · 腾讯云 DescribeDomains · 火山引擎 ListCdnDomains）——离线、零云调用。

各云的 JSON 调用函数全桩喂假响应，断言 argv（全局服务不带 region、分页参数写法）与归一化行结构（字段集与 AWS CloudFront 行一致）；
另覆盖「未开通 / 出错 / 没装 CLI → 空列表 + stderr 提示一次、绝不抛」。示例值一律虚构（账号 123456789012 / 100000000001 / 2100000000，example.com）。
"""
import json
import subprocess

import pytest

from noc_ops import deploy_config
from noc_ops.providers import aliyun as al
from noc_ops.providers import tencent as tc
from noc_ops.providers import volcengine as ve
from noc_ops.providers.aliyun import AliyunProvider
from noc_ops.providers.tencent import TencentProvider
from noc_ops.providers.volcengine import VolcengineProvider

pytestmark = pytest.mark.unit

# 与 AWS CloudFront / Cloudflare 行同一套键，看板 CDN_COLS 直接复用
CDN_KEYS = {"id", "domain", "aliases", "origin", "status", "enabled", "cert", "price_class",
            "last_modified", "account", "cloud", "partition", "comment"}

ALI_ACCT = {"profile": "acme", "account": "123456789012", "regions": {"cn-hangzhou": ("Hangzhou", "🇨🇳")}}
TC_CFG = {"account": "100000000001", "regions": {"ap-nanjing": ("Nanjing", "🇨🇳")}}
VE_ACCT = {"account": "2100000000", "regions": {"cn-beijing": ("Beijing", "🇨🇳")}}


# ———————————————————————————— 阿里云 ————————————————————————————
@pytest.fixture
def ali_account(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [ALI_ACCT] if cloud == "aliyun" else [])


def _ali_domain(name, status="online", ssl="on", **extra):
    d = {"DomainName": name, "Cname": f"{name}.w.kunlunsl.com", "CdnType": "web", "DomainStatus": status,
         "SslProtocol": ssl, "GmtCreated": "2025-01-02T03:04:05Z", "GmtModified": "2026-09-30T10:11:12Z",
         "Description": "", "Sources": {"Source": [{"Content": "origin.example.com", "Type": "domain", "Port": 443},
                                                   {"Content": "10.0.0.1", "Type": "ipaddr", "Port": 80}]}}
    d.update(extra)
    return d


def test_aliyun_cdn_rows_paged_global(monkeypatch, ali_account):
    calls = []

    def fake(a, args, timeout=60, quiet=False):
        calls.append((list(args), quiet))
        if args[:2] == ["cdn", "DescribeUserDomains"]:
            page = int(args[args.index("--PageNumber") + 1])
            data = ([_ali_domain("cdn.example.com", Description="官网静态"), _ali_domain("dl.example.com", status="offline", ssl="off", CdnType="download")]
                    if page == 1 else [_ali_domain("v.example.com", CdnType="video", Sources={"Source": []})])
            return {"Domains": {"PageData": data}, "TotalCount": 3, "PageNumber": page, "PageSize": 50}
        if args[:2] == ["cdn", "DescribeDomainCertificateInfo"]:
            assert quiet is True                                           # 证书到期是锦上添花，失败不许刷屏
            return {"CertInfos": {"CertInfo": [{"CertName": "c1", "CertExpireTime": "2027-03-01T00:00:00Z"}]}} \
                if args[args.index("--DomainName") + 1] == "cdn.example.com" else None
        raise AssertionError(f"unexpected aliyun call: {args}")

    monkeypatch.setattr(al, "_ali_json", fake)
    rows = AliyunProvider().list_cdn()
    assert [r["domain"] for r in rows] == ["cdn.example.com", "dl.example.com", "v.example.com"]
    assert all(set(r) == CDN_KEYS for r in rows)
    # 全局服务：不带 --RegionId；分页 PageSize/PageNumber 翻到 TotalCount 为止
    lst = [c for c, _ in calls if c[:2] == ["cdn", "DescribeUserDomains"]]
    assert lst == [["cdn", "DescribeUserDomains", "--PageSize", "50", "--PageNumber", "1"],
                   ["cdn", "DescribeUserDomains", "--PageSize", "50", "--PageNumber", "2"]]
    assert not any("--RegionId" in c for c, _ in calls)
    r = rows[0]
    assert r["id"] == "cdn.example.com" and r["aliases"] == "" and r["comment"] == "官网静态"
    assert r["origin"] == "origin.example.com, 10.0.0.1"
    assert r["status"] == "online" and r["enabled"] is True
    assert r["cert"] == "HTTPS 开 · 到期 2027-03-01" and r["price_class"] == "web"
    assert r["last_modified"] == "2026-09-30"
    assert r["account"] == "123456789012" and r["cloud"] == "aliyun" and r["partition"] == "cn"
    assert rows[1]["enabled"] is False and rows[1]["cert"] == "无 HTTPS" and rows[1]["price_class"] == "download"
    assert rows[2]["cert"] == "HTTPS 开" and rows[2]["origin"] == ""       # 证书明细拿不到 → 不带到期；无源站 → 空串
    # SslProtocol=off 的域名不该去查证书
    assert [c[c.index("--DomainName") + 1] for c, _ in calls if c[1] == "DescribeDomainCertificateInfo"] == ["cdn.example.com", "v.example.com"]


def test_aliyun_cdn_failure_or_not_activated_is_empty(monkeypatch, ali_account, capsys):
    """CLI 层报错（如 CDN 未开通 / 缺权限）：_ali_json 打一行、list_cdn 得空列表，不抛。"""
    def fake_run(cmd, **kw):
        assert cmd[:3] == ["aliyun", "cdn", "DescribeUserDomains"] and "--profile" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
            {"schemaVersion": "v1", "error_code": "Forbidden.NoPermission", "message": "cdn:DescribeUserDomains forbidden"}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert AliyunProvider().list_cdn() == []
    err = capsys.readouterr().err
    assert err.count("⚠️") == 1 and "Forbidden.NoPermission" in err and "123456789012" in err


def test_aliyun_cdn_cli_missing_no_crash(monkeypatch, ali_account, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("aliyun")))
    assert AliyunProvider().list_cdn() == []
    assert "跑 aliyun 失败" in capsys.readouterr().err


# ———————————————————————————— 腾讯云 ————————————————————————————
def _tc_domain(name, status="online", rid="cdn-abc", stype="web", origins=("origin.example.com",)):
    return {"ResourceId": rid, "AppId": 1250000000, "Domain": name, "Cname": f"{name}.cdn.dnsv1.com", "Status": status,
            "ProjectId": 0, "ServiceType": stype, "CreateTime": "2025-01-02 03:04:05", "UpdateTime": "2026-08-15 12:00:00",
            "Origin": {"Origins": list(origins), "OriginType": "domain", "ServerName": name, "OriginPullProtocol": "https"},
            "Disable": "normal", "Area": "mainland", "Readonly": "normal", "Product": "cdn"}


def test_tencent_cdn_rows(monkeypatch):
    calls = []

    def fake(args, timeout=60, on_error=None):
        calls.append(list(args))
        assert args[:2] == ["cdn", "DescribeDomains"] and "--region" not in args      # 全局服务
        return {"Domains": [_tc_domain("cdn.example.com"),
                            _tc_domain("dl.example.com", status="offline", rid="cdn-def", stype="download", origins=("1.2.3.4:8080", "5.6.7.8"))],
                "TotalNumber": 2, "RequestId": "r"}

    monkeypatch.setattr(tc, "_tc_json", fake)
    monkeypatch.setattr(tc.deploy_config, "cloud", lambda name: TC_CFG if name == "tencent" else {})
    rows = TencentProvider().list_cdn()
    assert calls == [["cdn", "DescribeDomains", "--Offset", "0", "--Limit", "100"]]       # 不足一页即收口
    assert [r["domain"] for r in rows] == ["cdn.example.com", "dl.example.com"]
    assert all(set(r) == CDN_KEYS for r in rows)
    r = rows[0]
    assert r == {"id": "cdn-abc", "domain": "cdn.example.com", "aliases": "", "origin": "origin.example.com",
                 "status": "online", "enabled": True, "cert": "—", "price_class": "web", "last_modified": "2026-08-15",
                 "account": "100000000001", "cloud": "tencent", "partition": "cn", "comment": ""}
    assert rows[1]["enabled"] is False and rows[1]["origin"] == "1.2.3.4:8080, 5.6.7.8" and rows[1]["price_class"] == "download"


def test_tencent_cdn_pages_follow_offset(monkeypatch):
    def fake(args, timeout=60, on_error=None):
        off = int(args[args.index("--Offset") + 1])
        n = 100 if off == 0 else 7
        return {"Domains": [_tc_domain(f"d{off + i}.example.com", rid=f"cdn-{off + i}") for i in range(n)], "TotalNumber": 107}
    monkeypatch.setattr(tc, "_tc_json", fake)
    monkeypatch.setattr(tc.deploy_config, "cloud", lambda name: TC_CFG if name == "tencent" else {})
    assert len(TencentProvider().list_cdn()) == 107


def test_tencent_cdn_not_activated_warns_once(monkeypatch, capsys):
    """exit 0 但 JSON 里带 Error（产品未开通/无权限）→ 空 + 一行提示；没装 tccli 同样不抛。"""
    monkeypatch.setattr(tc.deploy_config, "cloud", lambda name: TC_CFG if name == "tencent" else {})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
        {"Response": {"Error": {"Code": "UnauthorizedOperation.CdnUserNoWhitelist", "Message": "user not in whitelist"}, "RequestId": "r"}}), stderr=""))
    assert TencentProvider().list_cdn() == []
    err = capsys.readouterr().err
    assert err.count("⚠️") == 1 and "CDN 加速域名" in err and "UnauthorizedOperation" in err and "100000000001" in err
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("tccli")))
    assert TencentProvider().list_cdn() == []
    assert "未安装 tccli" in capsys.readouterr().err


def test_tencent_cdn_unconfigured_is_silent(monkeypatch):
    monkeypatch.setattr(tc.deploy_config, "cloud", lambda name: {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert TencentProvider().list_cdn() == []


# ———————————————————————————— 火山引擎 ————————————————————————————
@pytest.fixture
def ve_account(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [VE_ACCT] if cloud == "volcengine" else [])


def _ve_domain(name, status="online", https=True, stype="web", **extra):
    d = {"Domain": name, "Cname": f"{name}.volcgslb.com", "Status": status, "ServiceType": stype, "ServiceRegion": "chinese_mainland",
         "HTTPS": https, "IPv6": False, "OriginProtocol": "https", "PrimaryOrigin": ["origin.example.com"], "BackupOrigin": [],
         "Project": "default", "CreateTime": 1735786245, "UpdateTime": 1760000000, "LockStatus": "off"}
    d.update(extra)
    return d


def test_volcengine_cdn_rows_body_paging(monkeypatch, ve_account):
    bodies = []

    def fake(account, args, timeout=60, quiet=False):
        assert args[:2] == ["cdn", "ListCdnDomains"] and "--region" not in args and args[2] == "--body"   # 分页只走 --body JSON
        assert not any(a.startswith("--Page") for a in args)                                         # 旗标写法会被当字符串拒掉
        body = json.loads(args[3])
        bodies.append(body)
        assert set(body) == {"PageNum", "PageSize"} and body["PageSize"] == 100                       # 页码字段叫 PageNum
        if body["PageNum"] == 1:
            return {"Total": 102, "PageNum": 1, "PageSize": 100,
                    "Data": [_ve_domain(f"d{i}.example.com") for i in range(100)]}
        return {"Total": 102, "PageNum": 2, "PageSize": 100,
                "Data": [_ve_domain("cdn.example.com", BackupOrigin=["backup.example.com"], UpdateTime=1759000000),
                         _ve_domain("dl.example.com", status="offline", https=False, stype="download", PrimaryOrigin="1.2.3.4")]}

    monkeypatch.setattr(ve, "_ve_json", fake)
    rows = VolcengineProvider().list_cdn()
    assert [b["PageNum"] for b in bodies] == [1, 2]
    assert len(rows) == 102 and all(set(r) == CDN_KEYS for r in rows)
    byname = {r["domain"]: r for r in rows}
    r = byname["cdn.example.com"]
    assert r["id"] == "cdn.example.com" and r["aliases"] == "" and r["comment"] == ""
    assert r["origin"] == "origin.example.com, backup.example.com"
    assert r["status"] == "online" and r["enabled"] is True and r["cert"] == "HTTPS 开" and r["price_class"] == "web"
    assert r["last_modified"] == "2025-09-27"                                # 秒级时间戳 → 日期
    assert r["account"] == "2100000000" and r["cloud"] == "volcengine" and r["partition"] == "cn"
    d = byname["dl.example.com"]
    assert d["enabled"] is False and d["cert"] == "无 HTTPS" and d["origin"] == "1.2.3.4" and d["price_class"] == "download"
    assert rows[0]["domain"] == "cdn.example.com"                            # 按域名排序


def test_volcengine_cdn_service_stopped_is_empty_with_hint(monkeypatch, ve_account, capsys):
    """未开通 CDN：ve 实测 exit 1 + stdout 带 ResponseMetadata.Error OperationDenied.ServiceStopped →
    空列表 + 一行「按未纳管处理」提示（错误码要露出来，不能被埋成笼统的 exit 1）。"""
    stopped = {"ResponseMetadata": {"Action": "ListCdnDomains", "Service": "cdn", "Region": "cn-beijing", "Version": "2021-03-01",
                                    "Error": {"Code": "OperationDenied.ServiceStopped", "Message": "服务处于停用状态，不支持该操作。"}}}
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(stopped, ensure_ascii=False) + "\n",
                                           stderr="OperationDenied.ServiceStopped: 服务处于停用状态，不支持该操作。\n\tstatus code: 200")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert VolcengineProvider().list_cdn() == []
    assert calls == [["ve", "cdn", "ListCdnDomains", "--body", json.dumps({"PageSize": 100, "PageNum": 1})]]
    err = capsys.readouterr().err
    assert err.count("⚠️") == 1 and "OperationDenied.ServiceStopped" in err and "按未纳管处理" in err and "2100000000" in err
    assert "exit 1" not in err


def test_volcengine_cdn_cli_missing_no_crash(monkeypatch, ve_account, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ve")))
    assert VolcengineProvider().list_cdn() == []
    assert "跑 ve 失败" in capsys.readouterr().err


def test_ve_json_nonzero_exit_without_api_error_still_reports_exit(monkeypatch, capsys):
    """回归：没有 ResponseMetadata.Error 的 exit≠0（AK 过期等）仍走原来的「exit N」报法。"""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, stdout='{"Result": {"Data": []}}', stderr="error: expired credentials"))
    assert ve._ve_json({"account": "2100000000"}, ["cdn", "ListCdnDomains"]) is None
    assert "exit 1" in capsys.readouterr().err


def test_cdn_rows_share_cloudfront_schema():
    """三云行键集与 AWS CloudFront 行完全一致（看板 CDN_COLS 单一列定义）。"""
    from noc_ops.providers import PROVIDERS
    for cloud in ("aliyun", "tencent", "volcengine"):
        assert hasattr(PROVIDERS[cloud], "list_cdn")
    aws_keys = {"id", "domain", "aliases", "origin", "status", "enabled", "cert", "partition", "price_class",
                "last_modified", "cloud", "account", "comment"}
    assert aws_keys == CDN_KEYS
