"""Cloudflare billable usage: billing cycles, decimal sums and failure retention."""
import datetime as dt
import io
import json
import subprocess
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from noc_ops import deploy_config, sync
from noc_ops.providers import cloudflare

pytestmark = pytest.mark.unit
PATH = "/accounts/demo-account/billable-usage"


def charge(amount=0.1, **changes):
    return {"BillingAccountId": "demo-account", "BillingCurrency": "USD",
            "ServiceName": "Workers Standard", "BillingPeriodStart": "2026-12-20T00:00:00Z",
            "ChargePeriodStart": "2027-01-01T00:00:00Z", "ChargePeriodEnd": "2027-01-02T00:00:00Z",
            "ContractedCost": amount, "CumulatedContractedCost": 999,
            "ChargeCategory": "Usage", **changes}


def ok(result):
    return {"success": True, "result": result}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 1, 5, 12, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(cloudflare._dt, "datetime", Clock)
    deploy_config.reload()
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"cloudflare": {"accounts": [
        {"account": "cf-demo"}, {"account": "cf-other"}]}})
    monkeypatch.setattr(cloudflare.CloudflareProvider, "_token", staticmethod(lambda a: a["account"]))
    monkeypatch.setattr(sync, "PROVIDERS", {"cloudflare": cloudflare.CloudflareProvider()})
    monkeypatch.setattr(sync, "NOC_DATA", tmp_path / "data.json")
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    old = [{"cloud": "cloudflare", "account": a, "month": "2026-12", "service": "old",
            "currency": "USD", "amount": 5} for a in ("cf-demo", "cf-other")]
    sync.NOC_DATA.write_text(json.dumps({"billing": old}))
    def no_io(*args, **kwargs):
        raise AssertionError("unexpected live I/O")
    monkeypatch.setattr(subprocess, "run", no_io)
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", no_io)
    yield old
    deploy_config.reload()


def mock_api(monkeypatch, overrides=None):
    routes = {"/accounts": ok([{"id": "demo-account", "name": "Demo"}]),
              PATH + "/info": ok({"covered": True, "subscriptions": []}), PATH: ok([charge()])}
    routes.update(overrides or {})
    calls = []
    def get(req, **kwargs):
        parsed = urlsplit(req.full_url)
        path = parsed.path.removeprefix("/client/v4")
        calls.append((path, parse_qs(parsed.query), req.get_header("Authorization")))
        assert req.get_method() == "GET" and path in routes
        response = routes[path]
        if response is None:
            raise urllib.error.HTTPError(req.full_url, 403, "private-fixture-value", {}, None)
        return io.BytesIO(json.dumps(response).encode())
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", get)
    return calls


def test_actual_cycles_decimal_totals_currency_and_account_isolation(workspace, monkeypatch):
    calls = mock_api(monkeypatch, {PATH: ok([
        charge(0.1), charge(0.2), charge(-0.05),
        charge(3, BillingCurrency="EUR"), charge(0, ServiceName="D1"),
        charge(0.00002, ServiceName="R2"),
        charge(90, BillingPeriodStart="2026-10-20T00:00:00Z"),
        charge(0.5, BillingPeriodStart="2027-01-01T00:30:00+01:00"),
    ])})
    assert sync.refresh_account("cf-demo", "billing") == 0
    result = json.loads(sync.NOC_DATA.read_text())
    rows = [r for r in result["billing"] if r["account"] == "cf-demo"]
    period = "2026-12-20T00:00:00+00:00"
    amounts = {(r["service"], r["currency"], r["period_start"]): r["amount"] for r in rows}
    assert amounts == {("Workers Standard", "USD", period): 0.25,
                       ("Workers Standard", "EUR", period): 3,
                       ("D1", "USD", period): 0, ("R2", "USD", period): 0.00002,
                       ("Workers Standard", "USD", "2026-12-31T23:30:00+00:00"): 0.5}
    assert all(r["month"] == "2026-12" and r["partial"] for r in rows)
    assert all(r["cost_basis"] == "按量费用 / 账期" and r["period_basis"] == "billing_cycle" for r in rows)
    assert workspace[1] in result["billing"]
    assert calls[-1][1] == {"from": ["2026-11-01"], "to": ["2027-01-05"]}
    assert all(token == "Bearer cf-demo" for _, _, token in calls)
    assert result["collection_status"][0]["status"] == "ok"


@pytest.mark.parametrize("path,response,code", [
    ("/accounts", None, "query_failed"),
    (PATH + "/info", None, "query_failed"),
    (PATH + "/info", ok({"covered": False}), "billing_not_covered"),
    (PATH + "/info", ok({}), "invalid_response"),
    (PATH, None, "query_failed"),
    (PATH, {"success": False, "errors": [{"message": "private-fixture-value"}]}, "query_failed"),
    (PATH, ok({}), "invalid_response"),
    (PATH, [], "query_failed"),
])
def test_failures_preserve_costs(workspace, monkeypatch, capsys, path, response, code):
    calls = mock_api(monkeypatch, {path: response})
    sync.refresh_account("cf-demo", "billing")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["billing"] == workspace
    assert code in data["collection_status"][0]["errors"]
    assert data["collection_status"][0]["preserved"]
    if path.endswith("/info"):
        assert PATH not in [p for p, _, _ in calls]
    assert "private-fixture-value" not in capsys.readouterr().err


@pytest.mark.parametrize("change", [
    {"ContractedCost": None}, {"ContractedCost": "NaN"}, {"ContractedCost": "Infinity"},
    {"ContractedCost": True}, {"ContractedCost": "1e999"}, {"BillingCurrency": ""},
    {"BillingPeriodStart": "2027-01-01"}, {"BillingPeriodStart": "invalid"},
    {"BillingPeriodStart": None}, {"ServiceName": ""}, {"BillingAccountId": "other-account"},
    {"ChargeCategory": "Purchase"},
])
def test_invalid_rows_never_replace_old_costs(workspace, monkeypatch, change):
    mock_api(monkeypatch, {PATH: ok([charge(), charge(**change)])})
    sync.refresh_account("cf-demo", "billing")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["billing"] == workspace
    assert data["collection_status"][0]["status"] == "error"


def test_successful_empty_clears_only_selected_label(workspace, monkeypatch):
    mock_api(monkeypatch, {PATH: ok([])})
    sync.refresh_account("cf-demo", "billing")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["billing"] == workspace[1:]
    assert data["collection_status"][0]["status"] == "ok"
    assert "billing" not in sync.ACCOUNT_DEFAULT_KINDS["cloudflare"]


def test_full_cost_collection_includes_cloudflare(workspace, monkeypatch):
    mock_api(monkeypatch)
    data = {"billing": sync._billing_with_cache({"billing": []})}
    assert {r["account"] for r in data["billing"]} == {"cf-demo", "cf-other"}


def test_one_label_multiple_accounts_remain_distinct(workspace, monkeypatch):
    second = PATH.replace("demo-account", "second-account")
    mock_api(monkeypatch, {"/accounts": ok([{"id": "demo-account"}, {"id": "second-account"}]),
                          second + "/info": ok({"covered": True}),
                          second: ok([charge(BillingAccountId="second-account")])})
    sync.refresh_account("cf-demo", "billing")
    rows = [r for r in json.loads(sync.NOC_DATA.read_text())["billing"] if r["account"] == "cf-demo"]
    assert {r["cf_account_id"] for r in rows} == {"demo-account", "second-account"}
    assert sum(r["amount"] for r in rows) == 0.2
