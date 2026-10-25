"""AwsProvider.list_apikeys：Bedrock / Claude Platform 长期 API key 元数据（_aws_json 全桩，不碰真 aws）。"""
import pytest

from noc_ops.providers import PROVIDERS, aws

pytestmark = pytest.mark.unit

ACCOUNTS = [{"profile": "111", "partition": "global"},
            {"profile": "222", "partition": "china"}]

BEDROCK_KEY = {   # 照搬 2026-10-24 实测形状
    "UserName": "BedrockAPIKey-9fkj", "Status": "Active",
    "ServiceCredentialAlias": "BedrockAPIKey-9fkj-at-111",
    "CreateDate": "2026-05-20T05:09:06+00:00", "ExpirationDate": "2027-05-20T05:09:06+00:00",
    "ServiceSpecificCredentialId": "ACCAZVMTVBG7V2OB2AV6K", "ServiceName": "bedrock.amazonaws.com",
}
CN_KEY = {
    "UserName": "cn-ops", "Status": "Inactive", "ServiceCredentialAlias": "cn-ops-at-222",
    "CreateDate": "2025-01-01T00:00:00+00:00",
    "ServiceSpecificCredentialId": "ACCACN1", "ServiceName": "bedrock.amazonaws.com",
}


def _setup(monkeypatch):
    calls = []

    def fake(profile, region, args):
        calls.append((profile, tuple(args)))
        if args[1] == "list-users":
            return {"Users": [{"UserName": "cn-ops"}, {"UserName": "nobody"}]}
        svc = args[args.index("--service-name") + 1]
        if "--all-users" in args:
            if profile == "222":
                return None                                   # 中国区：不支持 --all-users
            return {"ServiceSpecificCredentials": [BEDROCK_KEY] if svc == "bedrock.amazonaws.com" else []}
        user = args[args.index("--user-name") + 1]
        return {"ServiceSpecificCredentials":
                [CN_KEY] if (user == "cn-ops" and svc == "bedrock.amazonaws.com") else []}
    monkeypatch.setattr(aws, "_aws_json", fake)
    monkeypatch.setattr(aws.deploy_config, "aws_accounts", lambda: ACCOUNTS)
    return calls


def test_global_all_users_and_china_skipped(monkeypatch, capsys):
    calls = _setup(monkeypatch)
    rows = PROVIDERS["aws"].list_apikeys()
    assert [(r["account"], r["name"]) for r in rows] == [("111", "BedrockAPIKey-9fkj-at-111")]

    g = rows[0]
    assert g["cloud"] == "aws" and g["id"] == "ACCAZVMTVBG7V2OB2AV6K"
    assert g["purpose"] == "Bedrock" and g["targets"] == ["bedrock.amazonaws.com"] and g["unrestricted"] is False
    assert g["service_account"] == "BedrockAPIKey-9fkj" and g["key_type"] == "长期 key" and g["status"] == "Active"
    assert g["created"] == "2026-05-20" and g["expires"] == "2027-05-20" and g["flag"] == "🌐"
    assert isinstance(g["age_days"], int) and g["age_days"] > 0

    # 全球区：每个服务一次 --all-users，不列用户；中国区：完全不调（不支持 --all-users，产品决定不盘）
    assert sum(1 for p, a in calls if p == "111" and "--all-users" in a) == 2
    assert not any(p == "222" for p, _ in calls)
    assert "AI 密钥" not in capsys.readouterr().err           # 全球区两个服务都成功 → 无诊断
    # 只盘元数据：行里绝不出现密钥串
    for r in rows:
        assert not any("secret" in k.lower() or "keystring" in k.lower() for k in r)


def test_account_failure_yields_no_rows_but_diagnoses(monkeypatch, capsys):
    """缺权限 / CLI 不认服务名：该账号为空、不兜底逐用户、stderr 说明原因（看板「没 key」≠「没权限」）。"""
    calls = []
    monkeypatch.setattr(aws, "_aws_json", lambda p, r, a: calls.append(tuple(a)) or None)
    monkeypatch.setattr(aws.deploy_config, "aws_accounts", lambda: ACCOUNTS)
    assert PROVIDERS["aws"].list_apikeys() == []
    assert all("--all-users" in a for a in calls) and len(calls) == 2    # 只有全球区 2 次，无 list-users / --user-name
    err = capsys.readouterr().err
    assert "AI 密钥" in err and "bedrock.amazonaws.com" in err
