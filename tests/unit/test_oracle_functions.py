"""OracleProvider.list_serverless：OCI Functions（application → function）归一化为「函数」tab 行（_oci 全桩，不碰真 oci）。"""
import pytest

from noc_ops.providers import PROVIDERS, oracle

pytestmark = pytest.mark.unit

TENANCY = "ocid1.tenancy.oc1..example0001"
APP_ID = "ocid1.fnapp.oc1.ap-tokyo-1.example0001"
ACCOUNTS = [{"profile": "DEFAULT", "account": "acme", "compartment": TENANCY,
             "regions": {"ap-tokyo-1": ["Tokyo", "🇯🇵"], "us-ashburn-1": ["Ashburn", "🇺🇸"]}}]

APP = {"id": APP_ID, "display-name": "demo-app", "lifecycle-state": "ACTIVE", "shape": "GENERIC_X86",
       "time-updated": "2026-09-01T02:03:04.000000+00:00"}
FN_IMAGE = {"id": "ocid1.fnfunc.oc1.ap-tokyo-1.example0001", "display-name": "hello", "application-id": APP_ID,
            "image": "ap-tokyo-1.ocir.io/examplens/repo/hello:0.0.3", "memory-in-mbs": 256, "timeout-in-seconds": 30,
            "lifecycle-state": "ACTIVE", "time-created": "2026-08-01T00:00:00.000000+00:00",
            "time-updated": "2026-10-20T12:00:00.000000+00:00"}
FN_PBF = {"id": "ocid1.fnfunc.oc1.ap-tokyo-1.example0002", "display-name": "prebuilt", "application-id": APP_ID,
          "image": None, "source-details": {"source-type": "PRE_BUILT_FUNCTIONS", "pbf-listing-id": "ocid1.pbf.example"},
          "memory-in-mbs": 128, "timeout-in-seconds": 60, "lifecycle-state": "ACTIVE",
          "time-created": "2026-07-15T00:00:00.000000+00:00"}


def _setup(monkeypatch, responses):
    """responses：{(region, "fn application list"|"fn function list"): 返回值}；缺省 → None（调用失败）。
    记录每次调用 (region, argv) 供断言。"""
    calls = []

    def fake(account, args, region=None, timeout=60, soft=False):
        key = " ".join(args[1:4])
        calls.append((region, list(args), soft))
        return responses.get((region, key), None)

    monkeypatch.setattr(oracle, "_oci", fake)
    monkeypatch.setattr(type(PROVIDERS["oracle"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    return calls


def test_functions_rows_and_argv(monkeypatch):
    calls = _setup(monkeypatch, {
        ("ap-tokyo-1", "fn application list"): [APP],
        ("ap-tokyo-1", "fn function list"): [FN_IMAGE, FN_PBF],
        ("us-ashburn-1", "fn application list"): [],                 # 另一区域没应用
    })
    rows = PROVIDERS["oracle"].list_serverless()
    assert [r["name"] for r in rows] == ["demo-app/hello", "demo-app/prebuilt"]
    r = rows[0]
    assert r == {
        "name": "demo-app/hello", "cloud": "oracle", "account": "acme",
        "region": "ap-tokyo-1", "region_display": "Tokyo", "flag": "🇯🇵",
        "runtime": "hello:0.0.3",                                    # 镜像只留最后一段
        "memory": 256, "timeout": 30, "code_mb": None, "last_modified": "2026-10-20",
    }
    # 预构建函数无镜像 → runtime=container；无 time-updated 退回 time-created
    assert rows[1]["runtime"] == "container" and rows[1]["last_modified"] == "2026-07-15"
    # argv：application 按 compartment 列、function 按 application 列；都带 --all、都是只读 list
    app_calls = [c for c in calls if c[1][1:4] == ["fn", "application", "list"]]
    fn_calls = [c for c in calls if c[1][1:4] == ["fn", "function", "list"]]
    assert [c[0] for c in app_calls] == ["ap-tokyo-1", "us-ashburn-1"]
    assert app_calls[0][1] == ["oci", "fn", "application", "list", "--compartment-id", TENANCY, "--all"]
    assert fn_calls == [("ap-tokyo-1", ["oci", "fn", "function", "list", "--application-id", APP_ID, "--all"], True)]
    assert all(c[2] is True for c in calls)                          # 一律 soft：未开通服务不刷屏


def test_service_unavailable_returns_empty_and_hints_once(monkeypatch, capsys):
    """Functions 未开通/无权限：两个区域都返回 None → 空列表、stderr 只提示一次、不抛异常。"""
    calls = _setup(monkeypatch, {})
    assert PROVIDERS["oracle"].list_serverless() == []
    err = capsys.readouterr().err
    assert err.count("⚠️ OCI DEFAULT fn") == 1 and "Functions 未开通" in err
    assert not any(c[1][1:4] == ["fn", "function", "list"] for c in calls)   # 没有应用就不查函数


def test_function_list_failure_only_skips_that_app(monkeypatch, capsys):
    """application 列得到、function list 失败 → 该应用留空、提示一次，别让 ops sync 崩。"""
    _setup(monkeypatch, {("ap-tokyo-1", "fn application list"): [APP]})
    assert PROVIDERS["oracle"].list_serverless() == []
    assert "function list 失败" in capsys.readouterr().err


def test_oci_wrapper_handles_missing_data_key(monkeypatch):
    """真 oci 空结果是 {"opc-total-items":"0"}（无 data 键）或空输出：_oci 要统一成 []，list_serverless 据此不出行。"""
    import subprocess

    class R:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    outs = iter(['{"opc-total-items": "0"}', ""])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(next(outs)))
    assert oracle._oci({"profile": "DEFAULT"}, ["oci", "fn", "application", "list", "--compartment-id", TENANCY]) == []
    assert oracle._oci({"profile": "DEFAULT"}, ["oci", "fn", "application", "list", "--compartment-id", TENANCY]) == []
    monkeypatch.setattr(type(PROVIDERS["oracle"]), "_accounts", staticmethod(lambda: ACCOUNTS))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R('{"opc-total-items": "0"}'))
    assert PROVIDERS["oracle"].list_serverless() == []


def test_accounts_without_compartment_are_skipped(monkeypatch):
    calls = _setup(monkeypatch, {})
    monkeypatch.setattr(type(PROVIDERS["oracle"]), "_accounts", staticmethod(lambda: [{"profile": "DEFAULT"}]))
    assert PROVIDERS["oracle"].list_serverless() == [] and calls == []
