"""GcpProvider.list_credits：结算账号级账务行（不碰真 gcloud，_gcloud 全部打桩）。"""
import pytest

from noc_ops.providers import PROVIDERS, gcp

pytestmark = pytest.mark.unit

ACCOUNTS = [{"project": "proj-a", "configuration": "c"},
            {"project": "proj-b", "configuration": "c"}]      # 两项目共用同一结算账号


def _fake_gcloud(responses, calls):
    def run(account, args, timeout=60):
        key = " ".join(args[1:4])                                # "billing projects describe" 等
        calls.append((account["project"], key))
        return responses.get(key, None)
    return run


def _setup(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(gcp, "_gcloud", _fake_gcloud(responses, calls))
    monkeypatch.setattr(type(PROVIDERS["gcp"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return calls


def test_credits_full_permission(monkeypatch):
    calls = _setup(monkeypatch, {
        "billing projects describe": {"billingAccountName": "billingAccounts/0132CB-X",
                                      "billingEnabled": True},
        "billing accounts describe": {"displayName": "My Billing Account", "open": True,
                                      "currencyCode": "JPY"},
        "billing budgets list": [{"displayName": "月预算",
                                  "amount": {"specifiedAmount": {"units": "5000", "currencyCode": "JPY"}},
                                  "thresholdRules": [{"thresholdPercent": 0.5}, {"thresholdPercent": 1.0}]}],
    })
    rows = PROVIDERS["gcp"].list_credits()
    assert [r["account"] for r in rows] == ["proj-a", "proj-b"]
    r = rows[0]
    assert r["cloud"] == "gcp" and r["plan"] == "PayAsYouGo" and r["state"] == "Open"
    assert r["account_name"] == "My Billing Account" and r["currency"] == "JPY"
    assert r["budget"] == "月预算 5000 JPY @50%/100%"
    assert r["credit"] is None and r["free_tier"] == "Always Free" and r["free_tier_end"] is None
    assert r["spending_limit"] == "Off"
    # 结算账号级两条查询只跑一次（两项目共用缓存），项目级每项目一次
    assert sum(1 for _, k in calls if k == "billing accounts describe") == 1
    assert sum(1 for _, k in calls if k == "billing budgets list") == 1
    assert sum(1 for _, k in calls if k == "billing projects describe") == 2


def test_credits_without_billing_account_viewer(monkeypatch):
    """只有项目级权限：能看到绑了结算、启用；账号级字段为 None（渲染 —），不报错不丢行。"""
    _setup(monkeypatch, {
        "billing projects describe": {"billingAccountName": "billingAccounts/0132CB-X",
                                      "billingEnabled": True},
        # accounts describe / budgets list 缺省 → None（模拟 PERMISSION_DENIED）
    })
    rows = PROVIDERS["gcp"].list_credits()
    assert len(rows) == 2
    assert rows[0]["state"] is None and rows[0]["budget"] is None and rows[0]["currency"] is None
    assert rows[0]["account_name"] == "0132CB-X"          # 退回显示结算账号 id


def test_credits_no_budget_marks_empty_string(monkeypatch):
    """有权限但没设预算 → budget=""（渲染层据此标「⚠️ 未设」）。"""
    _setup(monkeypatch, {
        "billing projects describe": {"billingAccountName": "billingAccounts/BA", "billingEnabled": True},
        "billing accounts describe": {"displayName": "BA", "open": True, "currencyCode": "USD"},
        "billing budgets list": [],
    })
    assert PROVIDERS["gcp"].list_credits()[0]["budget"] == ""


def test_credits_unlinked_project_and_api_failure(monkeypatch):
    """没绑结算 → plan=Unlinked 且不再查账号级；projects describe 失败 → 整行跳过。"""
    calls = _setup(monkeypatch, {"billing projects describe": {"billingEnabled": False}})
    rows = PROVIDERS["gcp"].list_credits()
    assert len(rows) == 2 and rows[0]["plan"] == "Unlinked" and rows[0]["account_name"] == "proj-a"
    assert not any(k == "billing accounts describe" for _, k in calls)

    _setup(monkeypatch, {})                                  # API 未启用：describe 返回 None
    assert PROVIDERS["gcp"].list_credits() == []


def test_summarize_budgets_last_period_amount():
    s = gcp._summarize_budgets([{"displayName": "跟上期", "amount": {"lastPeriodAmount": {}},
                                 "thresholdRules": [{"thresholdPercent": 0.9}]}])
    assert s == "跟上期 上期实际花费 @90%"
    assert gcp._summarize_budgets(None) is None
