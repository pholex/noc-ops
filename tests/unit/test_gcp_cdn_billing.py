"""Offline Cloud CDN and BigQuery billing regressions; all cloud I/O is mocked."""
import json
import subprocess

import pytest

from noc_ops import collection, deploy_config, sync
from noc_ops.providers import gcp

pytestmark = pytest.mark.unit
PROJECT = "demo-project"
ACCOUNT = {"project": PROJECT, "configuration": "demo", "billing_export_table":
           "demo-billing.costs.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX", "billing_location": "US"}
REF = f"projects/{PROJECT}/global/backendServices/web"
BUCKET_REF = f"projects/{PROJECT}/global/backendBuckets/static"
BACKEND = {"name": "web", "selfLink": "https://www.googleapis.com/compute/v1/" + REF,
           "enableCDN": True, "backends": [{"group": "projects/demo-project/zones/us-central1-a/instanceGroups/web"}],
           "cdnPolicy": {"cacheMode": "CACHE_ALL_STATIC"}}
BUCKET = {"name": "static", "enableCDN": True, "bucketName": "demo-static"}


@pytest.fixture
def scope(monkeypatch, tmp_path):
    deploy_config.reload()
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"gcp": {"accounts": [ACCOUNT]}})
    monkeypatch.setattr(sync, "PROVIDERS", {"gcp": gcp.GcpProvider()})
    monkeypatch.setattr(sync, "NOC_DATA", tmp_path / "data.json")
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync.shutil, "which", lambda cmd: "/mock/" + cmd)
    sync.NOC_DATA.write_text(json.dumps({"cdn": [], "billing": []}))
    def no_cloud(*args, **kwargs):
        raise AssertionError("unexpected live request")
    monkeypatch.setattr(subprocess, "run", no_cloud)
    monkeypatch.setattr(gcp.urllib.request, "urlopen", no_cloud)
    yield
    deploy_config.reload()


def cdn_mock(monkeypatch, overrides=None):
    responses = {"backend-services": [BACKEND, {"name": "disabled", "enableCDN": False}],
                 "backend-buckets": [BUCKET], "url-maps": [{"name": "routes", "defaultService": REF,
                 "hostRules": [{"hosts": ["cdn.example.com", "*.example.com"], "pathMatcher": "main"}],
                 "pathMatchers": [{"name": "main", "defaultService": BUCKET_REF,
                 "routeRules": [{"routeAction": {"weightedBackendServices": [{"backendService": REF, "weight": 100}],
                                                  "requestMirrorPolicy": {"backendService": "mirror"}}}]}]}]}
    responses.update(overrides or {})
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:2] == ["gcloud", "compute"]
        value = responses[cmd[2]]
        return subprocess.CompletedProcess(cmd, 1 if value is None else 0,
                                           json.dumps(value), "query unavailable" if value is None else "")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_cdn_services_buckets_routes_and_no_secrets(scope, monkeypatch):
    calls = cdn_mock(monkeypatch)
    rows = gcp.GcpProvider().list_cdn()
    assert len(rows) == 2
    service = next(r for r in rows if r["name"] == "web")
    bucket = next(r for r in rows if r["name"] == "static")
    assert service["domain"] == "*, *.example.com, cdn.example.com"
    assert service["cache_mode"] == "CACHE_ALL_STATIC" and service["id"] == REF
    assert bucket["origin"] == "gs://demo-static"
    assert bucket["domain"] == "*.example.com, cdn.example.com"
    assert all("--configuration" in cmd and cmd[-1] == "--quiet" for cmd in calls)
    assert all(cmd[cmd.index("--project") + 1] == PROJECT for cmd in calls)
    assert all(gcp.GcpProvider.is_read(" ".join(cmd[1:])) for cmd in calls)
    assert gcp._route_backends({"requestMirrorPolicy": {"backendService": "mirror"}}) == set()


@pytest.mark.parametrize("resource", ["backend-services", "backend-buckets"])
def test_cdn_failure_preserves_and_empty_clears(scope, monkeypatch, resource):
    old = {"id": "old", "cloud": "gcp", "account": PROJECT}
    sync.NOC_DATA.write_text(json.dumps({"cdn": [old]}))
    cdn_mock(monkeypatch, {resource: None})
    assert sync.refresh_account(PROJECT, "cdn") == 0
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["cdn"] == [old] and data["collection_status"][0]["status"] == "error"
    calls = cdn_mock(monkeypatch, {"backend-services": [], "backend-buckets": []})
    assert sync.refresh_account(PROJECT, "cdn") == 0
    assert json.loads(sync.NOC_DATA.read_text())["cdn"] == []
    assert len(calls) == 2


def test_url_map_failure_is_optional_and_unattached_backend_is_kept(scope, monkeypatch):
    cdn_mock(monkeypatch, {"url-maps": None})
    sync.refresh_kind("cdn")
    data = json.loads(sync.NOC_DATA.read_text())
    assert len(data["cdn"]) == 2 and all(not r["domain"] for r in data["cdn"])
    assert data["collection_status"][0]["status"] == "partial"


def bq_row(amount="12.34", currency="USD", month="2026-10", service="Compute Engine"):
    return {"f": [{"v": v} for v in (service, month, currency, amount)]}


def mock_query(monkeypatch, pages):
    monkeypatch.setattr(gcp, "_billing_token", lambda a: "fixture-token")
    calls = []
    iterator = iter(pages)
    def request(token, project, path, body=None):
        calls.append((project, path, body))
        return next(iterator)
    monkeypatch.setattr(gcp, "_billing_request", request)
    return calls


def test_billing_query_polls_and_paginates_with_location_and_parameters(scope, monkeypatch):
    job = {"jobId": "demo-job", "location": "US"}
    calls = mock_query(monkeypatch, [
        {"jobComplete": False, "jobReference": job},
        {"jobComplete": True, "jobReference": job, "rows": [bq_row()], "pageToken": "page 2", "totalRows": "2"},
        {"jobComplete": True, "rows": [bq_row("-2", "JPY")], "totalRows": "2"}])
    rows = gcp._billing_query(ACCOUNT, "2026-08-01T00:00:00Z", "2026-10-26T00:00:00Z")
    assert len(rows) == 2 and len(calls) == 3
    project, path, body = calls[0]
    assert project == "demo-billing" and path == "/queries"
    assert body["maximumBytesBilled"] == "1073741824" and not body["useLegacySql"]
    assert body["queryParameters"][0]["parameterValue"]["value"] == PROJECT
    assert "WHERE project.id = @project" in body["query"] and "UNNEST(credits)" in body["query"]
    assert "SUM(CAST(cost AS NUMERIC))" in body["query"]
    assert "location=US" in calls[1][1] and "pageToken=page+2" in calls[2][1]


@pytest.mark.parametrize("pages", [
    [None], [{"jobComplete": True}],
    [{"jobComplete": True, "rows": [bq_row()], "totalRows": "2"}],
    [{"jobComplete": True, "rows": [bq_row()], "totalRows": "2", "pageToken": "next",
      "jobReference": {"jobId": "demo"}}, None],
    [{"jobComplete": True, "rows": [], "totalRows": "0", "schema": {"fields": [{"name": "unexpected"}]}}],
])
def test_billing_incomplete_results_preserve_old(scope, monkeypatch, pages):
    mock_query(monkeypatch, pages)
    old = {"cloud": "gcp", "account": PROJECT, "amount": 10, "currency": "USD"}
    sync.NOC_DATA.write_text(json.dumps({"billing": [old]}))
    sync.refresh_account(PROJECT, "billing")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["billing"] == [old] and data["collection_status"][0]["status"] == "error"


def test_missing_export_is_not_zero_and_never_requests_token(scope, monkeypatch):
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"gcp": {"accounts": [{"project": PROJECT}]}})
    sync.refresh_kind("billing")
    data = json.loads(sync.NOC_DATA.read_text())
    assert "billing_export_not_configured" in data["collection_status"][0]["errors"]


@pytest.mark.parametrize("change", [{"billing_export_table": "bad`table"}, {"billing_max_bytes": 0},
                                    {"billing_max_bytes": "unlimited"}])
def test_invalid_export_config_never_authenticates(scope, change):
    with collection.Session({}) as session:
        rows = session.collect("gcp", PROJECT, "billing", [],
                               lambda: gcp._billing_query({**ACCOUNT, **change}, "start", "end"))
    assert rows == [] and session.failures()


def test_billing_net_negative_currency_and_year_boundary(scope, monkeypatch):
    class Clock(gcp.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 1, 5, tzinfo=tz)
    monkeypatch.setattr(gcp, "datetime", Clock)
    calls = mock_query(monkeypatch, [{"jobComplete": True, "totalRows": "3", "rows": [
        bq_row("12.345", "JPY", "2026-11"), bq_row("-3.50", "EUR", "2027-01"), bq_row("0")]}])
    rows = gcp.GcpProvider().list_billing()
    assert {(r["currency"], r["amount"]) for r in rows} == {("JPY", 12.34), ("EUR", -3.5)}
    assert rows[0]["partial"] is False and rows[1]["partial"] is True
    assert calls[0][2]["queryParameters"][1]["parameterValue"]["value"] == "2026-11-01T00:00:00Z"


def test_empty_billing_clears_only_selected_project(scope, monkeypatch):
    mock_query(monkeypatch, [{"jobComplete": True, "totalRows": "0"}])
    other = {"cloud": "gcp", "account": "other-project", "amount": 20}
    sync.NOC_DATA.write_text(json.dumps({"billing": [{"cloud": "gcp", "account": PROJECT}, other]}))
    sync.refresh_account(PROJECT, "billing")
    assert json.loads(sync.NOC_DATA.read_text())["billing"] == [other]


def test_full_sync_keeps_gcp_billing_without_querying(scope, monkeypatch):
    old = {"cloud": "gcp", "account": PROJECT, "fetched_at": "2020-01-01T00:00:00Z"}
    calls = []
    monkeypatch.setattr(sync, "_collect_kind", lambda kind, clouds: calls.append(clouds) or [])
    assert sync._billing_with_cache({"billing": [old]}) == [old]
    assert calls == [set()]
    assert "billing" not in sync.ACCOUNT_DEFAULT_KINDS["gcp"]


def test_token_and_http_errors_never_print_secrets(scope, monkeypatch, capsys):
    calls = []
    def command(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "fixture-token\n", "")
    monkeypatch.setattr(subprocess, "run", command)
    assert gcp._billing_token(ACCOUNT) == "fixture-token"
    assert calls[0][-2:] == ["--configuration", "demo"]
    def bad_http(*args, **kwargs):
        raise OSError("fixture-token")
    monkeypatch.setattr(gcp.urllib.request, "urlopen", bad_http)
    assert gcp._billing_request("fixture-token", "demo-billing", "/queries", {}) is None
    assert "fixture-token" not in capsys.readouterr().err


def test_shared_billing_export_filters_each_project(scope, monkeypatch):
    accounts = [ACCOUNT, {**ACCOUNT, "project": "other-project"}]
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"gcp": {"accounts": accounts}})
    calls = mock_query(monkeypatch, [{"jobComplete": True, "totalRows": "1", "rows": [bq_row()]},
                                     {"jobComplete": True, "totalRows": "1", "rows": [bq_row("2")]}])
    rows = gcp.GcpProvider().list_billing()
    assert [(r["account"], r["amount"]) for r in rows] == [(PROJECT, 12.34), ("other-project", 2)]
    assert [c[2]["queryParameters"][0]["parameterValue"]["value"] for c in calls] == [PROJECT, "other-project"]


def test_billing_timeout_and_repeated_page_token_fail(scope, monkeypatch):
    mock_query(monkeypatch, [{"jobComplete": False, "jobReference": {"jobId": "demo"}}])
    ticks = iter([0, 121])
    monkeypatch.setattr(gcp.time, "monotonic", lambda: next(ticks))
    assert gcp._billing_query(ACCOUNT, "start", "end") is None
    monkeypatch.setattr(gcp.time, "monotonic", lambda: 0)
    page = {"jobComplete": True, "jobReference": {"jobId": "demo"}, "pageToken": "repeat", "rows": []}
    mock_query(monkeypatch, [page, page])
    assert gcp._billing_query(ACCOUNT, "start", "end") is None


def test_dashboard_totals_and_overview_keep_currencies_separate():
    import shutil
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute dashboard JavaScript")
    html = (Path(__file__).parents[2] / "src/noc_ops/dashboard/index.html").read_text()
    currency_code = html[html.index("const CUR_SYM="):html.index("const BILLING_COLS=")]
    total_code = html[html.index("let _billTotalKey="):html.index("gridApi = createGrid")]
    overview_code = html[html.index("  const bill=(DATA.billing||[]).filter"):html.index("  // 卡片顺序")]
    script = '''const assert=require('node:assert/strict');
const DATA={billing:[
 {month:'2026-10',currency:'USD',amount:1},
 {month:'2026-10',currency:'CNY',amount:2},
 {month:'2026-10',currency:'JPY',amount:300},
 {month:'2026-10',currency:'EUR',amount:-4}]};
let view='billing', pinned;
let gridApi={forEachNodeAfterFilter:fn=>DATA.billing.forEach(data=>fn({data})),
 setGridOption:(key,value)=>{pinned=value;}};
''' + currency_code + total_code + overview_code + '''
updateBillingTotal();
assert.equal(pinned[0].amount_usd,1);
assert.equal(pinned[0].amount_cny,2);
assert.equal(pinned[0].amount_jpy,300);
assert.equal(pinned[0].amount_eur,-4);
assert.equal(amountCol('USD').valueGetter({data:DATA.billing[2]}),null);
assert.equal(amountCol('JPY').valueGetter({data:DATA.billing[2]}),300);
assert.equal(currentCost,'$1.00 · ¥2.00 · JPY 300.00 · EUR -4.00');
'''
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
