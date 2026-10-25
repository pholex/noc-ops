"""阿里云 billing / credits（BssOpenApi）—— 离线、零云调用。"""
import pytest

from noc_ops import sync
from noc_ops.providers import PROVIDERS, aliyun

pytestmark = pytest.mark.unit

ACCOUNTS = [{"profile": "acme", "account": "1234567890123456"},
            {"profile": "acme-dns", "account": "6543210987654321"}]


def _overview(items):
    return {"Success": True, "Code": "Success", "Data": {"Items": {"Item": items}}}


def test_billing_and_credits(monkeypatch):
    cycles = []

    def fake(account, args, timeout=60):
        if args[1] == "QueryBillOverview":
            cycles.append((account["profile"], args[args.index("--BillingCycle") + 1]))
            if account["profile"] != "acme-dns":
                return _overview([{"ProductName": "对象存储", "PretaxAmount": 0, "Currency": "CNY"}])   # 0 元抹零行
            if cycles[-1][1].endswith("-09"):
                return _overview([
                    {"ProductName": "大模型服务平台百炼", "ProductCode": "bailian", "SubscriptionType": "PayAsYouGo",
                     "PretaxAmount": 657, "CashAmount": 0, "OutstandingAmount": 657, "Currency": "CNY"},
                    {"ProductName": "云解析DNS", "PretaxAmount": 19.9, "CashAmount": 19.9, "Currency": "CNY"}])
            return _overview([])
        if args[1] == "QueryAccountBalance":
            if account["profile"] == "acme-dns":
                return {"Success": True, "Code": "200", "Data": {
                    "AvailableCashAmount": "1,475.05", "AvailableAmount": "1,475.05", "Currency": "CNY",
                    "CreditAmount": "0.00", "QuotaLimit": "0.00"}}
            return {"Success": True, "Code": "200", "Data": {
                "AvailableAmount": "-12.30", "Currency": "CNY", "QuotaLimit": "500.00"}}
        return None
    monkeypatch.setattr(aliyun, "_ali_json", fake)
    monkeypatch.setattr(type(PROVIDERS["aliyun"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    p = PROVIDERS["aliyun"]

    rows = p.list_billing(months=3)
    assert len(cycles) == 6                                   # 2 账号 × 近 3 个月
    assert len(rows) == 2 and all(r["cloud"] == "aliyun" and r["account"] == "6543210987654321" for r in rows)
    bailian, dns = rows                                        # 同月按金额倒序
    assert bailian["service"].startswith("大模型") and bailian["amount"] == 657.0     # 后付费未扣款也计入（取 PretaxAmount 非 CashAmount）
    assert dns["amount"] == 19.9 and dns["currency"] == "CNY"
    assert bailian["month"].endswith("-09") and bailian["period_start"] == bailian["month"] + "-01"
    assert bailian["partial"] is False and bailian["fetched_at"]

    c = p.list_credits()
    assert [x["account_name"] for x in c] == ["阿里云 acme", "阿里云 acme-dns"]
    ph, orf = c
    assert orf["credit"] == 1475.05 and orf["credit_currency"] == "CNY"            # 千分位字符串已转数
    assert orf["state"] == "Normal" and orf["spending_limit"] == "On" and orf["budget"] is None
    assert ph["state"] == "Arrears" and ph["credit"] == -12.3 and ph["spending_limit"] == "Off"   # 有信控额度→可先欠着
    assert ph["plan"] == "Balance+PostPaid" and ph["free_tier"] is None


def test_skipped_when_api_unavailable(monkeypatch):
    monkeypatch.setattr(aliyun, "_ali_json", lambda *a, **k: None)
    monkeypatch.setattr(type(PROVIDERS["aliyun"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    assert PROVIDERS["aliyun"].list_credits() == [] and PROVIDERS["aliyun"].list_billing() == []


def test_ali_json_treats_cli_errors_as_failure(monkeypatch, capsys):
    """aliyun CLI 对未知 profile / API 不存在 / 业务 Success=false 都 exit 0，必须看 JSON 判失败。"""
    import subprocess as sp

    class R:
        returncode, stderr = 0, ""
        def __init__(self, out): self.stdout = out
    acct = {"profile": "x", "account": "1"}
    cases = [
        "ERROR: unknown profile nope, run configure to check\n",                                 # 非 JSON
        '{"schemaVersion":"v1","message":"Specified api is not found","error_code":"InvalidApi.NotFound","status_code":404}',
        '{"schemaVersion":"v1","message":"\\"Foo\\" is not a valid api."}',                        # CLI 层无 error_code
        '{"Success":false,"Code":"NotApplicable","Message":"This API is not applicable"}',          # 业务层
    ]
    for out in cases:
        monkeypatch.setattr(sp, "run", lambda *a, **k: R(out))
        assert aliyun._ali_json(acct, ["bssopenapi", "QueryAccountBalance"]) is None, out
    assert capsys.readouterr().err.count("⚠️ 阿里云") == len(cases)
    monkeypatch.setattr(sp, "run", lambda *a, **k: R('{"Success":true,"Code":"200","Data":{"AvailableAmount":"8.11"}}'))
    assert aliyun._ali_json(acct, ["bssopenapi", "QueryAccountBalance"])["Data"]["AvailableAmount"] == "8.11"


def test_amount_parsing():
    assert aliyun._amount("1,475.05") == 1475.05 and aliyun._amount(0.0001525899) == pytest.approx(0.0001525899)
    assert aliyun._amount(None) == 0.0 and aliyun._amount("") == 0.0 and aliyun._amount("n/a") == 0.0


def test_sync_wiring():
    assert sync.ACCOUNT_SCOPED_KINDS["aliyun"] == ("instances", "storage", "domains", "databases", "serverless", "cdn", "iam", "billing", "credits")
    assert "billing" not in sync.ACCOUNT_DEFAULT_KINDS["aliyun"] and "credits" in sync.ACCOUNT_DEFAULT_KINDS["aliyun"]
