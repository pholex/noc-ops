"""阿里云函数计算 FC 3.0：ListFunctions 翻页、行结构、区域失败汇总。离线。"""
import pytest

from noc_ops import deploy_config
from noc_ops.providers import aliyun as al
from noc_ops.providers.aliyun import AliyunProvider

pytestmark = pytest.mark.unit

ACCT = {"profile": "acme", "account": "123456789012", "regions": {"cn-hangzhou": ("Hangzhou", "🇨🇳"), "cn-shanghai": ("Shanghai", "🇨🇳")}}


@pytest.fixture
def one_account(monkeypatch):
    monkeypatch.setattr(deploy_config, "cloud_accounts", lambda cloud: [ACCT] if cloud == "aliyun" else [])


def test_fc_functions_paged_rows(monkeypatch, one_account):
    calls = []

    def fake(a, args, timeout=60, quiet=False):
        calls.append(args)
        region = args[args.index("--region") + 1]
        if region != "cn-hangzhou":
            return {"functions": []}
        if "--nextToken" not in args:
            return {"functions": [{"functionName": "img-resize", "functionId": "f-1", "runtime": "python3.10", "memorySize": 512,
                                   "timeout": 60, "codeSize": 2_400_000, "lastModifiedTime": "2026-09-01T10:00:00Z", "description": "缩图"}],
                    "nextToken": "t2"}
        return {"functions": [{"functionName": "cron-job", "runtime": "custom-container", "memorySize": 1024, "timeout": 300}]}

    monkeypatch.setattr(al, "_ali_json", fake)
    rows = AliyunProvider().list_serverless()
    assert [r["name"] for r in rows] == ["cron-job", "img-resize"]
    r = rows[1]
    assert r["cloud"] == "aliyun" and r["account"] == "123456789012" and r["region_display"] == "Hangzhou"
    assert r["runtime"] == "python3.10" and r["memory"] == 512 and r["timeout"] == 60 and r["code_mb"] == 2.4
    assert r["last_modified"] == "2026-09-01" and r["note"] == "缩图"
    assert rows[0]["code_mb"] == "" and rows[0]["id"] == ""
    hz = [c for c in calls if "cn-hangzhou" in c]
    assert hz[0][:2] == ["fc", "ListFunctions"] and "--nextToken" in hz[1] and "t2" in hz[1]


def test_fc_region_failures_summarized_once(monkeypatch, one_account, capsys):
    monkeypatch.setattr(al, "_ali_json", lambda a, args, timeout=60, quiet=False: None)
    assert AliyunProvider().list_serverless() == []
    err = capsys.readouterr().err
    assert err.count("FC ListFunctions 失败") == 1
