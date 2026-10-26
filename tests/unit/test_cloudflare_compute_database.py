"""Offline Containers / D1 inventory, pagination and account isolation."""
import io
import json
import subprocess
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from noc_ops import collection, deploy_config, resources, sync
from noc_ops.providers import cloudflare

pytestmark = pytest.mark.unit
APPS = "/accounts/demo-account/containers/applications"
INSTANCES = APPS + "/demo-app/instances-v2"
D1 = "/accounts/demo-account/d1/database"
APP = {"id": "demo-app", "name": "web", "configuration": {
    "environment_variables": [{"name": "EXAMPLE", "value": "private-fixture-value"}]}}
INSTANCE = {"id": "a" * 64, "application_id": "demo-app", "name": "web-one",
            "image": "registry.example.com/web:v1", "status": {"state": "running", "updated_at": "2026-01-01T00:00:00Z"},
            "configuration": {"vcpu": 0.5, "memory": 1024, "disk": 4000},
            "location": {"name": "demo-location", "region": "APAC"}}
DATABASE = {"uuid": "demo-db", "name": "catalog", "jurisdiction": "eu", "version": "production",
            "created_at": "2026-01-01T00:00:00Z"}


def ok(rows, **info):
    return {"success": True, "result": rows, "result_info": info}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    deploy_config.reload()
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"cloudflare": {"accounts": [
        {"account": "cf-demo"}, {"account": "cf-other"}]}})
    monkeypatch.setattr(cloudflare.CloudflareProvider, "_token", staticmethod(lambda a: a["account"]))
    monkeypatch.setattr(sync, "PROVIDERS", {"cloudflare": cloudflare.CloudflareProvider()})
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    for attr, filename in (("NOC_DATA", "data.json"), ("SSH_CONFIG", "ssh-config"),
                           ("INVENTORY", "inventory.md"), ("SNAPSHOT_FILE", "snapshot.json"),
                           ("USER_CACHE_FILE", "ssh-users.json")):
        monkeypatch.setattr(sync, attr, tmp_path / filename)
    sync.NOC_DATA.write_text('{"instances": [], "databases": []}')
    def no_live(*args, **kwargs):
        raise AssertionError("unexpected live I/O")
    monkeypatch.setattr(subprocess, "run", no_live)
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", no_live)
    deploy_config.set_account_scope("cloudflare", "cf-demo")
    yield tmp_path
    deploy_config.reload()


def api(monkeypatch, overrides=None):
    routes = {
        ("/accounts", "1"): ok([{"id": "demo-account", "name": "Demo"}], total_pages=1),
        (APPS, "1"): ok([APP]),
        (INSTANCES, "1"): ok([INSTANCE]),
        (D1, "1"): ok([DATABASE], total_count=1),
        (D1 + "/demo-db", "1"): ok({"file_size": 1048576, "read_replication": {"mode": "auto"}}),
    }
    routes.update(overrides or {})
    calls = []
    def get(request, **kwargs):
        assert request.get_method() == "GET"
        parsed = urlsplit(request.full_url)
        path = parsed.path.removeprefix("/client/v4")
        query = parse_qs(parsed.query)
        key = (path, query.get("page_token", query.get("page", ["1"]))[0])
        calls.append((key, request.get_header("Authorization")))
        assert key in routes, key
        result = routes[key]
        if result is None:
            raise urllib.error.HTTPError(request.full_url, 403, "private-fixture-value", {}, None)
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", get)
    return calls


def test_containers_cursor_pagination_metadata_and_read_only(workspace, monkeypatch):
    calls = api(monkeypatch, {
        (APPS, "1"): ok([APP], next_page_token="app+/next"),
        (APPS, "app+/next"): ok([{"id": "empty-app", "name": "empty"}]),
        (APPS + "/empty-app/instances-v2", "1"): ok([]),
        (INSTANCES, "1"): ok([INSTANCE], next_page_token="instance+/next"),
        (INSTANCES, "instance+/next"): ok([{"id": "b" * 64, "status": {"state": "inactive"}}]),
    })
    rows = cloudflare.CloudflareProvider().list_instances()
    assert len(rows) == 2
    running = next(r for r in rows if r["state"] == "running")
    assert running["application"] == "web" and running["image"] == INSTANCE["image"]
    assert running["instance_id"] == INSTANCE["id"] and running["region"] == "APAC"
    assert running["type"] == "Container · 0.5 vCPU / 1024 MiB"
    assert running["memory_mib"] == 1024 and running["disk_mb"] == 4000
    assert all(r["connect"] == "none" and r["ssh_alias"] is None for r in rows)
    assert all(resources.capabilities(r, "instances") == ["inventory"] for r in rows)
    assert "private-fixture-value" not in json.dumps(rows)
    assert "Host " not in sync._render_ssh_config([], rows)
    assert ((INSTANCES, "instance+/next"), "Bearer cf-demo") in calls
    sync.write_inventory([], [], rows, {})
    assert "无 SSH，不支持 `ops reboot`" in sync.INVENTORY.read_text()


def test_d1_pages_detail_and_no_sql(workspace, monkeypatch):
    calls = api(monkeypatch, {
        (D1, "1"): ok([DATABASE], total_count=2),
        (D1, "2"): ok([{**DATABASE, "uuid": "second-db", "name": "empty"}], total_count=2),
        (D1 + "/second-db", "1"): ok({"file_size": 0}),
    })
    rows = cloudflare.CloudflareProvider().list_databases()
    assert len(rows) == 2
    assert rows[0]["engine"] == "D1 (SQLite)" and rows[0]["storage"] == "1.00 MiB"
    assert rows[0]["read_replication"] == "auto" and rows[0]["created"] == "2026-01-01"
    assert rows[1]["db_size_bytes"] == 0 and rows[1]["storage"] == "0.00 MiB"
    assert all(r["state"] == "—" and r["endpoint"] == "—" for r in rows)
    assert all(resources.capabilities(r, "databases") == ["inventory"] for r in rows)
    assert not any("query" in key[0] or "raw" in key[0] for key, _ in calls)


@pytest.mark.parametrize("kind,path", [("instances", INSTANCES), ("databases", D1)])
@pytest.mark.parametrize("failure", ["permission", "page", "malformed", "repeat"])
def test_failed_inventory_retains_only_selected_account(workspace, monkeypatch, capsys, kind, path, failure):
    provider = cloudflare.CloudflareProvider()
    api(monkeypatch)
    old = getattr(provider, "list_" + kind)()
    account_key = "profile" if kind == "instances" else "account"
    other = {**old[0], "id": "other-id", account_key: "cf-other"}
    sync.NOC_DATA.write_text(json.dumps({kind: old + [other]}))
    first = [INSTANCE] if kind == "instances" else [DATABASE]
    page = "next" if kind == "instances" else "2"
    info = {"next_page_token": "next"} if kind == "instances" else {"total_count": 2}
    overrides = {(path, "1"): None}
    if failure == "malformed":
        overrides = {(path, "1"): ok({"unexpected": []})}
    elif failure in ("page", "repeat"):
        overrides = {(path, "1"): ok(first, **info), (path, page):
                     None if failure == "page" else ok(first, **info)}
    calls = api(monkeypatch, overrides)
    assert sync.refresh_account("cf-demo", kind) == 0
    data = json.loads(sync.NOC_DATA.read_text())
    assert {r["id"] for r in data[kind]} == {r["id"] for r in old + [other]}
    assert data["collection_status"][0]["preserved"] is True
    assert all(token == "Bearer cf-demo" for _, token in calls)
    assert "private-fixture-value" not in capsys.readouterr().err


@pytest.mark.parametrize("kind,path", [("instances", APPS), ("databases", D1)])
def test_successful_empty_clears_selected_account(workspace, monkeypatch, kind, path):
    api(monkeypatch)
    old = getattr(cloudflare.CloudflareProvider(), "list_" + kind)()
    key = "profile" if kind == "instances" else "account"
    other = {**old[0], "id": "other-id", key: "cf-other", "note": old[0].get("note", "")}
    sync.NOC_DATA.write_text(json.dumps({kind: old + [other]}))
    api(monkeypatch, {(path, "1"): ok([], total_count=0)})
    sync.refresh_account("cf-demo", kind)
    data = json.loads(sync.NOC_DATA.read_text())
    assert data[kind] == [other]
    assert data["collection_status"][0]["status"] == "ok"


def test_d1_optional_detail_failure_keeps_list(workspace, monkeypatch):
    api(monkeypatch, {(D1 + "/demo-db", "1"): None})
    sync.refresh_account("cf-demo", "databases")
    data = json.loads(sync.NOC_DATA.read_text())
    assert data["databases"][0]["uuid"] == "demo-db"
    assert data["databases"][0]["storage"] == "—"
    assert data["collection_status"][0]["status"] == "partial"


def test_account_discovery_paginates_and_disambiguates_instances(workspace, monkeypatch):
    second_apps = APPS.replace("demo-account", "second-account")
    api(monkeypatch, {
        ("/accounts", "1"): ok([{"id": "demo-account"}], total_count=2),
        ("/accounts", "2"): ok([{"id": "second-account"}], total_count=2),
        (second_apps, "1"): ok([APP]),
        (second_apps + "/demo-app/instances-v2", "1"): ok([INSTANCE]),
    })
    rows = cloudflare.CloudflareProvider().list_instances()
    assert len({r["id"] for r in rows}) == 2


def test_cursor_cycle_and_empty_missing_numbered_page_fail(workspace, monkeypatch):
    for path, cursor, overrides in [
        (APPS, True, {(APPS, "1"): ok([], next_page_token="loop"),
                      (APPS, "loop"): ok([], next_page_token="loop")}),
        (D1, False, {(D1, "1"): ok([], total_count=2)}),
    ]:
        api(monkeypatch, overrides)
        with collection.Session({}) as session:
            old = [{"id": "old"}]
            rows = session.collect("cloudflare", "cf-demo", "instances", old,
                                   lambda: cloudflare.CloudflareProvider()._list_pages(
                                       "cf-demo", path, "containers" if cursor else "d1", cursor=cursor))
        assert rows == old and session.failures()


@pytest.mark.parametrize("response", [None, ok({}), ok([{}]), {"success": False}, []])
def test_failed_account_discovery_preserves_inventory(workspace, monkeypatch, response):
    api(monkeypatch, {("/accounts", "1"): response})
    with collection.Session({}) as session:
        old = [{"id": "old"}]
        result = session.collect("cloudflare", "cf-demo", "instances", old,
                                 cloudflare.CloudflareProvider().list_instances)
    assert result == old and session.failures()


def test_dashboard_container_connection_and_columns():
    import shutil
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute dashboard JavaScript")
    html = (Path(__file__).parents[2] / "src/noc_ops/dashboard/index.html").read_text()
    columns = html[html.index("const COLS = ["):html.index("const DOMAIN_COLS=")]
    terminal = html[html.index("function _canTerminal("):html.index("// 点连接列")]
    script = "const assert=require('node:assert/strict'); const CLOUD_RANK={}; const byRank=()=>()=>0;\n"
    script += terminal + columns + """
const data={cloud:'cloudflare',connect:'none',ssh_user:null,ssh_alias:null};
assert.equal(COLS.find(c=>c.colId==='connect').valueGetter({data}), '无 SSH');
assert.equal(COLS.find(c=>c.field==='ssh_user').valueFormatter({data}), '—');
assert.equal(_canTerminal(data),false);
assert.ok(COLS.find(c=>c.field==='application'));
assert.ok(COLS.find(c=>c.field==='image'));
assert.equal(COLS.find(c=>c.colId==='connect').valueGetter({data:{connect:'direct'}}), '直连');
"""
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
