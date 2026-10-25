"""腾讯云 billing / credits（tccli billing）—— 离线、零云调用。"""
import pytest

from noc_ops.providers import PROVIDERS, tencent

pytestmark = pytest.mark.unit


def _summary(ready, items):
    return {"Ready": ready, "SummaryTotal": None if not ready else {"RealTotalCost": "0"},
            "SummaryOverview": None if not ready else items, "RequestId": "x"}


def test_billing_and_credits(monkeypatch):
    calls = []
    monkeypatch.setattr(tencent.time, "sleep", lambda s: None)

    def fake(args, timeout=60):
        if args[1] == "DescribeBillSummaryByProduct":
            begin = args[args.index("--BeginTime") + 1]; end = args[args.index("--EndTime") + 1]
            calls.append(begin[:7])
            assert begin.endswith("-01 00:00:00") and end.endswith(" 23:59:59") and end[8:10] in ("28", "29", "30", "31")
            if begin.startswith("2026-05"):
                # 第一次 Ready=0（懒生成），第二次才有数据
                if calls.count("2026-05") == 1:
                    return _summary(0, None)
                return _summary(1, [
                    {"BusinessCode": "p_lighthouse", "BusinessCodeName": "轻量应用服务器",
                     "RealTotalCost": "734.40000000", "TotalCost": "864.00000000", "CashPayAmount": "734.40000000"},
                    {"BusinessCode": "p_adp", "BusinessCodeName": "腾讯云智能体开发平台(ADP)",
                     "RealTotalCost": "0.00000000", "TotalCost": "0.99000000"}])      # 实付 0 → 剔除
            return _summary(1, [])
        if args[1] == "DescribeAccountBalance":
            return {"Balance": 1, "Uin": 100000000001, "RealBalance": 1, "CashAccountBalance": 1,
                    "OweAmount": 0, "IsAllowArrears": False, "IsCreditLimited": False, "CreditAmount": 0}
        return None
    monkeypatch.setattr(tencent, "_tc_json", fake)
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: {"account": "100000000001", "regions": {}})

    # 固定「今天」为 2026-07-15 → 近 3 月 = 05/06/07
    class FakeDT(tencent.datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 7, 15, tzinfo=tz)
    monkeypatch.setattr(tencent, "datetime", FakeDT)

    p = PROVIDERS["tencent"]
    rows = p.list_billing(months=3)
    assert calls == ["2026-05", "2026-05", "2026-06", "2026-07"]           # 05 月重试一次
    assert len(rows) == 1
    r = rows[0]
    assert r["cloud"] == "tencent" and r["account"] == "100000000001" and r["service"] == "轻量应用服务器"
    assert r["amount"] == 734.4 and r["currency"] == "CNY" and r["month"] == "2026-05" and r["period_start"] == "2026-05-01"
    assert r["partial"] is False and r["fetched_at"]

    c = p.list_credits()
    assert len(c) == 1 and c[0]["credit"] == 0.01 and c[0]["credit_currency"] == "CNY"     # 分 → 元
    assert c[0]["state"] == "Normal" and c[0]["spending_limit"] == "On" and c[0]["budget"] is None
    assert c[0]["account_name"] == "腾讯云 100000000001" and c[0]["plan"] == "Balance+PostPaid"


def test_not_ready_month_is_skipped_not_zero(monkeypatch, capsys):
    monkeypatch.setattr(tencent.time, "sleep", lambda s: None)
    n = {"c": 0}

    def fake(args, timeout=60):
        n["c"] += 1
        return _summary(0, None)
    monkeypatch.setattr(tencent, "_tc_json", fake)
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: {"account": "1", "regions": {}})
    assert PROVIDERS["tencent"].list_billing(months=1) == []
    assert n["c"] == 3 and "Ready=0" in capsys.readouterr().err                  # 最多 3 次，然后跳过并提示


def test_credits_arrears_and_allow_arrears(monkeypatch):
    monkeypatch.setattr(tencent, "_tc_json", lambda args, timeout=60: {
        "RealBalance": -2350, "Balance": -2350, "OweAmount": 2350, "IsAllowArrears": True, "CreditAmount": 0})
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: {"account": "1", "regions": {}})
    c = PROVIDERS["tencent"].list_credits()[0]
    assert c["state"] == "Arrears" and c["credit"] == -23.5 and c["spending_limit"] == "Off"


def test_skipped_when_api_unavailable(monkeypatch):
    monkeypatch.setattr(tencent, "_tc_json", lambda *a, **k: None)
    monkeypatch.setattr(tencent.deploy_config, "cloud", lambda name: {"account": "1", "regions": {}})
    assert PROVIDERS["tencent"].list_credits() == [] and PROVIDERS["tencent"].list_billing() == []


def test_tc_json_failure_modes(monkeypatch, capsys):
    import subprocess as sp

    class R:
        def __init__(self, code, out, err=""): self.returncode, self.stdout, self.stderr = code, out, err
    monkeypatch.setattr(sp, "run", lambda *a, **k: R(255, "", "usage: tccli ...\n\n[TencentCloudSDKException] code:AuthFailure.SecretIdNotFound message:bad\n"))
    assert tencent._tc_json(["billing", "DescribeAccountBalance"]) is None
    err = capsys.readouterr().err
    assert "AuthFailure.SecretIdNotFound" in err and "exit 255" in err
    monkeypatch.setattr(sp, "run", lambda *a, **k: R(0, "not json"))
    assert tencent._tc_json(["billing", "DescribeAccountBalance"]) is None
    monkeypatch.setattr(sp, "run", lambda *a, **k: R(0, '{"Balance": 1}'))
    assert tencent._tc_json(["billing", "DescribeAccountBalance"]) == {"Balance": 1}


def test_fen():
    assert tencent._fen(1) == 0.01 and tencent._fen("73440") == 734.4 and tencent._fen(None) == 0.0 and tencent._fen("x") == 0.0
