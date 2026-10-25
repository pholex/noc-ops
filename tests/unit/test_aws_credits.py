"""AwsProvider.list_credits：预算告警 / 免费额度 / 本月 credits 抵扣 归一化成账务行（_aws_json 全桩，不碰真 aws）。"""
import json

import pytest

from noc_ops import deploy_config
from noc_ops.providers import PROVIDERS, aws

pytestmark = pytest.mark.unit

GLOBAL = {"profile": "123456789012", "partition": "global"}
CHINA = {"profile": "111122223333", "partition": "china"}

BUDGETS = {"Budgets": [
    {"BudgetName": "monthly-8k", "BudgetType": "COST", "TimeUnit": "MONTHLY",
     "BudgetLimit": {"Amount": "8000.0", "Unit": "USD"},
     "CalculatedSpend": {"ActualSpend": {"Amount": "3152.6", "Unit": "USD"},
                         "ForecastedSpend": {"Amount": "4088.7", "Unit": "USD"}}},
    {"BudgetName": "daily-50", "BudgetType": "COST", "TimeUnit": "DAILY",
     "BudgetLimit": {"Amount": "50.0", "Unit": "USD"},
     "CalculatedSpend": {"ActualSpend": {"Amount": "72.0", "Unit": "USD"}}},            # 超限
    {"BudgetName": "forecast-only", "BudgetType": "COST", "TimeUnit": "MONTHLY",
     "BudgetLimit": {"Amount": "100.0", "Unit": "USD"},
     "CalculatedSpend": {"ActualSpend": {"Amount": "60.0", "Unit": "USD"},
                         "ForecastedSpend": {"Amount": "130.0", "Unit": "USD"}}},        # 预计超限
]}
FREE_TIER = {"freeTierUsages": [
    {"service": "AWS CodeCommit", "usageType": "User-Month", "limit": 5.0, "actualUsageAmount": 1.0,
     "forecastedUsageAmount": 1.24, "unit": "User-Month", "freeTierType": "Always Free"},
    {"service": "Amazon Simple Notification Service", "usageType": "Requests-Tier1", "limit": 1000000.0,
     "actualUsageAmount": 900000.0, "forecastedUsageAmount": 1100000.0, "unit": "Requests", "freeTierType": "Always Free"},
    {"service": "CloudWatch Events", "usageType": "ScheduledInvocation", "limit": 14000000.0,
     "actualUsageAmount": 185.0, "forecastedUsageAmount": 229.4, "unit": "Invocations", "freeTierType": "Always Free"},
]}
CE_CREDIT = {"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-26"}, "Estimated": True,
                                "Groups": [], "Total": {"UnblendedCost": {"Amount": "-12.3456", "Unit": "USD"}}}]}


def _setup(monkeypatch, accounts, responses):
    """responses：{"budgets"|"freetier"|"ce": 返回值}；缺省 → None（调用失败/无权限）。记录 (profile, region, argv)。"""
    calls = []

    def fake(profile, region, args):
        calls.append((profile, region, list(args)))
        return responses.get(args[0], None)

    monkeypatch.setattr(aws, "_aws_json", fake)
    monkeypatch.setattr(deploy_config, "aws_accounts", lambda: accounts)
    return calls


def test_global_account_full_row_and_argv(monkeypatch):
    calls = _setup(monkeypatch, [GLOBAL], {"budgets": BUDGETS, "freetier": FREE_TIER, "ce": CE_CREDIT})
    rows = PROVIDERS["aws"].list_credits()
    assert len(rows) == 1
    r = rows[0]
    assert r["cloud"] == "aws" and r["account"] == "123456789012" and r["account_name"] == "AWS 123456789012"
    assert r["state"] == "Active" and r["spending_limit"] == "Off" and r["currency"] == "USD"
    assert r["budget"] == "3 个预算，1 个超限（daily-50），1 个预计超限（forecast-only）"
    # 免费额度：按 实际/上限 占比排序，SNS 90% 标 ⚠️，服务名去掉 Amazon/AWS 前缀
    assert r["free_tier"].startswith("3 项 Always Free · 用量最高：Simple Notification Service Requests-Tier1 900000/1e+06 ⚠️")
    assert "CodeCommit User-Month 1/5" in r["free_tier"]
    assert r["free_tier_end"] is None and r["free_tier_days"] is None               # AWS 不直接给到期日
    # credits：CE 负数取绝对值，语义写进 plan
    assert r["credit"] == 12.35 and r["credit_currency"] == "USD"
    assert r["plan"] == "PayAsYouGo（credit=本月已抵扣，非余额）"
    assert r["fetched_at"].endswith("+00:00")
    # argv：三条都是只读 get/describe；budgets/freetier 固定 us-east-1；CE 按 RECORD_TYPE=Credit 过滤、MONTHLY
    assert [(p, reg, a[:2]) for p, reg, a in calls] == [
        ("123456789012", "us-east-1", ["budgets", "describe-budgets"]),
        ("123456789012", "us-east-1", ["freetier", "get-free-tier-usage"]),
        ("123456789012", "us-east-1", ["ce", "get-cost-and-usage"]),
    ]
    assert calls[0][2] == ["budgets", "describe-budgets", "--account-id", "123456789012"]
    ce_argv = calls[2][2]
    assert ce_argv[ce_argv.index("--granularity") + 1] == "MONTHLY"
    assert json.loads(ce_argv[ce_argv.index("--filter") + 1]) == {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}
    tp = ce_argv[ce_argv.index("--time-period") + 1]
    assert tp.startswith("Start=") and tp[13:16] == "-01"                             # 本月 1 日起


def test_china_account_routes_to_cn_endpoint(monkeypatch):
    """中国区（aws-cn）：三个全局服务都走 cn-northwest-1 端点（2026-10-25 实测 budgets/freetier 在中国区可用，
    不是"仅全球区"），币种 CNY；没预算 → describe-budgets 空输出 → {} → budget=""。"""
    calls = _setup(monkeypatch, [CHINA], {
        "budgets": {},                                                                  # 空 stdout 经 _aws_json 成 {}
        "freetier": {"freeTierUsages": []},
        "ce": {"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-26"},
                                  "Total": {"UnblendedCost": {"Amount": "0", "Unit": "CNY"}}}]},
    })
    rows = PROVIDERS["aws"].list_credits()
    assert len(rows) == 1
    r = rows[0]
    assert [(reg, a[0]) for _, reg, a in calls] == [("cn-northwest-1", "budgets"), ("cn-northwest-1", "freetier"),
                                                     ("cn-northwest-1", "ce")]
    assert r["budget"] == "" and r["free_tier"] is None and r["state"] == "Active"
    assert r["credit"] == 0.0 and r["credit_currency"] == "CNY" and r["currency"] == "CNY"


def test_aws_json_blank_stdout_is_empty_success(monkeypatch):
    """describe-budgets 没预算时 rc=0 但一个字都不输出：要当成功的空结果 {}（budget=""），不能当失败 None。"""
    import subprocess

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert aws._aws_json("123456789012", "us-east-1", ["budgets", "describe-budgets"]) == {}


def test_freetier_error_becomes_none_without_dropping_row(monkeypatch):
    """账号不在免费期 / freetier 报错 → free_tier=None；预算仍在，行不丢。"""
    _setup(monkeypatch, [GLOBAL], {"budgets": {"Budgets": []}, "ce": CE_CREDIT})
    r = PROVIDERS["aws"].list_credits()[0]
    assert r["free_tier"] is None and r["free_tier_end"] is None and r["free_tier_days"] is None
    assert r["budget"] == ""                                                        # 有权限但没设预算 → 渲染层标「未设」
    assert r["state"] == "Active" and r["credit"] == 12.35


def test_ce_failure_keeps_budget_fields(monkeypatch):
    """CE 未启用/缺权限 → credit/credit_currency=None，plan 不带抵扣说明；预算与免费额度照常。"""
    _setup(monkeypatch, [GLOBAL], {"budgets": BUDGETS, "freetier": FREE_TIER})
    r = PROVIDERS["aws"].list_credits()[0]
    assert r["credit"] is None and r["credit_currency"] is None and r["plan"] == "PayAsYouGo"
    assert r["budget"].startswith("3 个预算") and r["free_tier"].startswith("3 项")


def test_all_sources_fail_drops_row_and_hints_once(monkeypatch, capsys):
    """凭证失效 / CLI 缺失：三路全 None → 不造假行、不抛异常，stderr 每账号恰好一条。"""
    _setup(monkeypatch, [GLOBAL, CHINA], {})
    assert PROVIDERS["aws"].list_credits() == []
    err = capsys.readouterr().err
    assert err.count("⚠️ AWS 123456789012 账务") == 1 and err.count("⚠️ AWS 111122223333 账务") == 1


def test_summarize_helpers_edge_cases():
    p = PROVIDERS["aws"]
    assert p._summarize_budgets(None) is None and p._summarize_budgets([]) == ""
    # 无固定上限（PlannedBudgetLimits）的预算不比对、也不报错；全部在限内标「均在限内」
    assert p._summarize_budgets([{"BudgetName": "planned", "PlannedBudgetLimits": {}},
                                 {"BudgetName": "ok", "BudgetLimit": {"Amount": "10", "Unit": "USD"},
                                  "CalculatedSpend": {"ActualSpend": {"Amount": "1", "Unit": "USD"}}}]) == "2 个预算，均在限内"
    assert p._summarize_free_tier(None) is None and p._summarize_free_tier([]) is None
    assert p._summarize_free_tier([{"service": "AWS Glue", "usageType": "Catalog-Request", "limit": 0,
                                    "actualUsageAmount": 3, "freeTierType": "Always Free"}]) \
        == "1 项 Always Free · 用量最高：Glue Catalog-Request 3/0"
