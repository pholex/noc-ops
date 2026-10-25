"""OracleProvider.list_billing：Usage API（request-summarized-usages）按服务×月归一化为「成本」tab 行
（_oci_raw 全桩，不碰真 oci；示例 OCID/金额，不含任何真实租户数据）。"""
import pytest

from noc_ops.providers import PROVIDERS, oracle

pytestmark = pytest.mark.unit

TENANCY = "ocid1.tenancy.oc1..example0001"
ACCOUNTS = [{"profile": "DEFAULT", "account": "acme", "compartment": TENANCY,
             "regions": {"ap-tokyo-1": ["Tokyo", "🇯🇵"]}}]
ARGV_HEAD = ["oci", "usage-api", "usage-summary", "request-summarized-usages"]


def _item(service, month, amount, currency="USD"):
    """usage-api items[] 一项（只保留本模块消费的键，真实返回还有 sku/compartment/region 等几十个键）。"""
    return {"service": service, "computed-amount": amount, "currency": currency,
            "time-usage-started": f"{month}-01T00:00:00+00:00", "time-usage-ended": f"{month}-01T00:00:00+00:00"}


def _setup(monkeypatch, response, today=(2026, 10, 25)):
    """response：_oci_raw 的返回值（整包 JSON / {} / None）。固定「今天」以便断言时间窗与 partial。记录调用供断言。"""
    calls = []

    def fake(account, args, region=None, timeout=60, soft=False):
        calls.append({"account": account, "args": list(args), "region": region, "timeout": timeout, "soft": soft})
        return response

    monkeypatch.setattr(oracle, "_oci_raw", fake)
    monkeypatch.setattr(type(PROVIDERS["oracle"]), "_accounts", staticmethod(lambda: ACCOUNTS))

    class FakeDT(oracle.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(*today, 9, 30, tzinfo=tz)
    monkeypatch.setattr(oracle, "datetime", FakeDT)
    return calls


def test_rows_argv_merge_and_zero_skip(monkeypatch):
    calls = _setup(monkeypatch, {"data": {"group-by": ["service"], "items": [
        _item("Compute", "2026-08", 12.5),
        _item("Compute", "2026-08", 0.255),                  # 同服务同月第二行 → 合并求和
        _item("Block Storage", "2026-08", 0, currency=" "),  # 0 金额（Always Free 形态：currency 是空格）→ 跳过
        _item("Compute", "2026-09", 30.0),
        _item("Object Storage", "2026-10", 1.2, currency=" "),   # 本月、币种空 → partial=True、回退 USD
        _item("Telemetry", "2026-10", 0.001),                # 四舍五入后 0.00 → 跳过
    ]}})
    rows = PROVIDERS["oracle"].list_billing(months=3)

    # 一次调用覆盖整个 3 月窗口：08-01 起、「明天」止（排他），MONTHLY + COST + 按 service 分组，不传 --region
    assert len(calls) == 1
    c = calls[0]
    assert c["args"] == ARGV_HEAD + [
        "--tenant-id", TENANCY,
        "--time-usage-started", "2026-08-01T00:00:00Z", "--time-usage-ended", "2026-10-26T00:00:00Z",
        "--granularity", "MONTHLY", "--query-type", "COST", "--group-by", '["service"]',
    ]
    assert c["region"] is None and c["soft"] is True and c["account"] is ACCOUNTS[0]

    assert [(r["service"], r["month"], r["amount"]) for r in rows] == [
        ("Compute", "2026-08", 12.76), ("Compute", "2026-09", 30.0), ("Object Storage", "2026-10", 1.2),
    ]
    r = rows[0]
    assert r == {
        "cloud": "oracle", "account": "acme", "service": "Compute", "month": "2026-08", "amount": 12.76,
        "currency": "USD", "period_start": "2026-08-01", "partial": False, "fetched_at": r["fetched_at"],
    }
    assert r["fetched_at"].startswith("2026-10-25T09:30:00")
    assert rows[1]["partial"] is False
    assert rows[2]["partial"] is True and rows[2]["currency"] == "USD"      # 本月 partial；空币种回退 USD


def test_year_wrap_window(monkeypatch):
    """1 月份取近 3 月要跨年：窗口从上一年 11-01 开始。"""
    calls = _setup(monkeypatch, {"data": {"group-by": ["service"], "items": []}}, today=(2027, 1, 5))
    assert PROVIDERS["oracle"].list_billing(months=3) == []
    a = calls[0]["args"]
    assert a[a.index("--time-usage-started") + 1] == "2026-11-01T00:00:00Z"
    assert a[a.index("--time-usage-ended") + 1] == "2027-01-06T00:00:00Z"


def test_all_zero_tenant_returns_empty_without_hint(monkeypatch, capsys):
    """Always Free 租户：每服务每月一行但全 0 → 空列表、不造假行、不提示（这是合法结果不是错误）。"""
    _setup(monkeypatch, {"data": {"group-by": ["service"], "items": [
        _item("Compute", "2026-09", 0, " "), _item("Compute", "2026-10", 0, " "),
        _item("Virtual Cloud Network", "2026-10", 0, " "),
    ]}})
    assert PROVIDERS["oracle"].list_billing() == []
    assert capsys.readouterr().err == ""


def test_no_data_key_is_empty_not_error(monkeypatch, capsys):
    """空结果 oci 可能回 {"opc-total-items":"0"}（无 data 键）或什么都不输出（{}）→ 空列表、无提示。"""
    _setup(monkeypatch, {"opc-total-items": "0"})
    assert PROVIDERS["oracle"].list_billing() == []
    _setup(monkeypatch, {})
    assert PROVIDERS["oracle"].list_billing() == []
    assert capsys.readouterr().err == ""


def test_api_failure_returns_empty_and_hints_once(monkeypatch, capsys):
    """oci 未装 / 认证失败 / 缺 usage-reports 读权限：_oci_raw 返回 None → 空列表、stderr 一条提示、不抛。"""
    _setup(monkeypatch, None)
    assert PROVIDERS["oracle"].list_billing() == []
    err = capsys.readouterr().err
    assert err.count("⚠️ OCI DEFAULT usage-api") == 1 and "usage-reports" in err


def test_tenancy_resolution(monkeypatch, tmp_path):
    """--tenant-id 来源优先级：显式 tenancy > 租户形态的 compartment > ~/.oci/config 的 [profile] tenancy。"""
    assert oracle._tenancy({"tenancy": "ocid1.tenancy.oc1..explicit", "compartment": TENANCY}) == "ocid1.tenancy.oc1..explicit"
    assert oracle._tenancy({"compartment": TENANCY}) == TENANCY
    cfg = tmp_path / "config"
    cfg.write_text("[DEFAULT]\nuser=ocid1.user.oc1..example\ntenancy=ocid1.tenancy.oc1..fromcfg\n"
                   "[other]\ntenancy=ocid1.tenancy.oc1..other\n", encoding="utf-8")
    monkeypatch.setattr(oracle, "_OCI_CONFIG", cfg)
    sub = "ocid1.compartment.oc1..example0001"            # 扫描根是子隔间 → 回 oci 配置找租户
    assert oracle._tenancy({"profile": "DEFAULT", "compartment": sub}) == "ocid1.tenancy.oc1..fromcfg"
    assert oracle._tenancy({"profile": "other", "compartment": sub}) == "ocid1.tenancy.oc1..other"
    assert oracle._tenancy({"profile": "missing", "compartment": sub}) is None
    monkeypatch.setattr(oracle, "_OCI_CONFIG", tmp_path / "nope")
    assert oracle._tenancy({"profile": "DEFAULT", "compartment": sub}) is None


def test_unresolvable_tenancy_skips_account_with_hint(monkeypatch, capsys, tmp_path):
    calls = _setup(monkeypatch, {"data": {"items": []}})
    monkeypatch.setattr(oracle, "_OCI_CONFIG", tmp_path / "nope")
    monkeypatch.setattr(type(PROVIDERS["oracle"]), "_accounts",
                        staticmethod(lambda: [{"profile": "DEFAULT", "compartment": "ocid1.compartment.oc1..sub"}]))
    assert PROVIDERS["oracle"].list_billing() == []
    assert calls == []                                       # 没租户 OCID 就不发请求
    assert "拿不到租户 OCID" in capsys.readouterr().err


def test_oci_wrapper_still_unwraps_lists(monkeypatch):
    """_oci 重构为 _oci_raw 的拆封层后行为不变：list 原样、无 data 键 → []、空输出 → []、失败 → None。"""
    seq = iter([{"data": [{"id": 1}]}, {"opc-total-items": "0"}, {}, None, {"data": {"items": []}}])
    monkeypatch.setattr(oracle, "_oci_raw", lambda *a, **k: next(seq))
    acct = {"profile": "DEFAULT"}
    assert oracle._oci(acct, ["oci", "x", "y", "list"]) == [{"id": 1}]
    assert oracle._oci(acct, ["oci", "x", "y", "list"]) == []
    assert oracle._oci(acct, ["oci", "x", "y", "list"]) == []
    assert oracle._oci(acct, ["oci", "x", "y", "list"]) is None
    assert oracle._oci(acct, ["oci", "x", "y", "list"]) == []           # data 是 dict → 对 list 消费方而言是空


def test_usage_summary_query_is_classified_read_only():
    from noc_ops.aws_safety import is_read_only_oci
    assert is_read_only_oci("usage-api usage-summary request-summarized-usages --tenant-id ocid1.tenancy.oc1..example --granularity MONTHLY")
    assert not is_read_only_oci("compute instance action --instance-id ocid1.instance.oc1..example --action STOP")
