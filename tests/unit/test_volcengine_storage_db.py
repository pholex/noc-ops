"""火山引擎 TOS 桶（tosutil ls 解析、未装/未配置的提示）与 RDS MySQL v2 / PostgreSQL 实例行。离线。"""
import subprocess

import pytest

from noc_ops import deploy_config
from noc_ops.providers import volcengine as ve
from noc_ops.providers.volcengine import VolcengineProvider

pytestmark = pytest.mark.unit

ACCT = {"account": "2100000000", "regions": {"cn-beijing": ("Beijing", "🇨🇳")}}

LS_OUT = """Start at 2026-10-25 04:50:37 +0000 UTC

Bucket                   CreationDate                  Location                 BucketType
tos://noc-test-abc123    2026-10-25T04:50:37Z          cn-beijing               fns
tos://media-archive      2025-01-02T03:04:05Z          cn-shanghai              hns

Bucket number is: 2
"""


@pytest.fixture
def one_account(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [ACCT] if cloud == "volcengine" else [])


def test_tos_buckets_parsed_into_storage_rows(monkeypatch, one_account):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=LS_OUT, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = VolcengineProvider().list_storage()
    assert seen["cmd"] == ["tosutil", "ls"]
    assert [r["name"] for r in rows] == ["noc-test-abc123", "media-archive"]
    assert rows[0] == {"name": "noc-test-abc123", "cloud": "volcengine", "account": "2100000000",
                       "region": "cn-beijing", "region_display": "Beijing", "flag": "🇨🇳",
                       "domain": "noc-test-abc123.tos-cn-beijing.volces.com", "created": "2026-10-25", "bucket_type": "fns"}
    assert rows[1]["region_display"] == "cn-shanghai"            # 配置里没写的区域原样显示


def test_tosutil_missing_or_unconfigured_hint_no_crash(monkeypatch, one_account, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("tosutil")))
    assert VolcengineProvider().list_storage() == []
    assert "未安装 tosutil" in capsys.readouterr().err
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="Warn: Please set sk, region and endpoint in the configuration file!\n", stderr=""))
    assert VolcengineProvider().list_storage() == []
    assert "tosutil 未配置" in capsys.readouterr().err


def test_no_volcengine_account_means_no_tosutil_call(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert VolcengineProvider().list_storage() == []


def test_databases_mysql_v2_and_postgres(monkeypatch, one_account):
    calls = []

    def fake_ve(account, args, timeout=60, quiet=False):
        calls.append(args)
        import json as _j
        body = _j.loads(args[args.index("--body") + 1]); svc, page = args[0], str(body["PageNumber"])
        assert body["RegionId"] == "cn-beijing" and body["PageSize"] == 100
        if svc == "rdsmysqlv2":
            return {"Total": 1, "Instances": [{"InstanceId": "mysql-1", "InstanceName": "orders", "InstanceStatus": "Running",
                                              "DBEngineVersion": "MySQL_8_0", "NodeSpec": "rds.mysql.1c2g", "StorageSpace": 100,
                                              "StorageType": "LocalSSD", "CreateTime": "2026-03-12T02:30:43Z"}]} if page == "1" else {"Instances": []}
        return {"Total": 0, "Instances": []}

    monkeypatch.setattr(ve, "_ve_json", fake_ve)
    rows = VolcengineProvider().list_databases()
    assert len(rows) == 1
    r = rows[0]
    assert r["engine"] == "MySQL 8_0" and r["class"] == "rds.mysql.1c2g" and r["storage"] == "100 GB 本地 SSD"
    assert r["state"] == "running" and r["region_display"] == "Beijing" and r["created"] == "2026-03-12" and r["endpoint"] == "—"
    assert [c[0] for c in calls] == ["rdsmysqlv2", "rdspostgresql"]
    assert calls[0][:3] == ["rdsmysqlv2", "DescribeDBInstances", "--body"]


def test_databases_first_page_failure_is_empty_not_crash(monkeypatch, one_account):
    monkeypatch.setattr(ve, "_ve_json", lambda account, args, timeout=60, quiet=False: None)
    assert VolcengineProvider().list_databases() == []


def test_databases_region_failures_summarized_once_per_account(monkeypatch, capsys):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [
        {"account": "2100000000", "regions": {"cn-beijing": ("Beijing", "🇨🇳"), "ap-southeast-3": ("Jakarta", "🇮🇩")}}])
    seen = []

    def fake(account, args, timeout=60, quiet=False):
        seen.append(quiet)
        return {"Total": 0, "Instances": []} if "cn-beijing" in args[-1] else None

    monkeypatch.setattr(ve, "_ve_json", fake)
    assert VolcengineProvider().list_databases() == []
    err = capsys.readouterr().err
    assert err.count("RDS DescribeDBInstances 失败") == 1 and "rdsmysqlv2@ap-southeast-3" in err and "rdspostgresql@ap-southeast-3" in err
    assert all(seen)                                   # 列表调用一律 quiet，由本方法汇总
