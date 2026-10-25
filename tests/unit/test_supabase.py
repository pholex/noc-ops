"""Supabase provider：项目行读全（算力 / 库大小 / PITR / 备份 / 网络限制），已暂停项目不跑 SQL，缺权限只提示一次。离线。"""
import pytest

from noc_ops import deploy_config
from noc_ops.providers.supabase import SupabaseProvider, _human

pytestmark = pytest.mark.unit


def _install(monkeypatch, routes):
    """routes: {(method, path 前缀): payload 或 None(=失败)}；记录调用。"""
    calls = []

    def fake_call(self, token, path, method="GET", body=None, quiet=False):
        calls.append((method, path))
        # 最长前缀优先：/v1/projects 是其它所有路径的前缀，不能让它先命中
        for (m, prefix), resp in sorted(routes.items(), key=lambda kv: -len(kv[0][1])):
            if m == method and path.startswith(prefix):
                return (False, "HTTP 403 forbidden") if resp is None else (True, resp)
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(SupabaseProvider, "_call", fake_call)
    monkeypatch.setattr(SupabaseProvider, "_token", staticmethod(lambda a: "tok"))
    monkeypatch.setattr(deploy_config, "cloud_accounts",
                        lambda cloud: [{"account": "sb", "keychain_service": "noc-supabase"}] if cloud == "supabase" else [])
    return calls


def _proj(name, ref, status="ACTIVE_HEALTHY"):
    return {"id": ref, "ref": ref, "name": name, "region": "ap-northeast-1", "status": status,
            "created_at": "2026-06-14T06:01:57Z", "organization_slug": "org1",
            "database": {"host": f"db.{ref}.supabase.co", "version": "17.6.1.127", "postgres_engine": "17"}}


ADDONS = {"selected_addons": [
    {"type": "compute_instance", "variant": {"id": "ci_micro", "name": "Micro", "meta": {"cpu_cores": 2, "memory_gb": 1}}},
    {"type": "custom_domain", "variant": {"id": "cd_default"}}]}
BACKUPS = {"pitr_enabled": False, "walg_enabled": True, "backups": [
    {"status": "COMPLETED", "inserted_at": "2026-10-23T17:05:04Z"},
    {"status": "COMPLETED", "inserted_at": "2026-10-24T17:06:26Z"},
    {"status": "FAILED", "inserted_at": "2026-10-25T17:06:26Z"}]}


def test_full_row(monkeypatch):
    calls = _install(monkeypatch, {
        ("GET", "/v1/projects"): [_proj("acme-app", "abc")],
        ("GET", "/v1/projects/abc/billing/addons"): ADDONS,
        ("GET", "/v1/projects/abc/database/backups"): BACKUPS,
        ("GET", "/v1/projects/abc/network-restrictions"): {"config": {"dbAllowedCidrs": ["203.0.113.0/24", "198.51.100.7/32"]}},
        ("POST", "/v1/projects/abc/database/query"): [{"bytes": 56020115}],
    })
    rows = SupabaseProvider().list_databases()
    assert len(rows) == 1
    r = rows[0]
    assert r["class"] == "Micro（2 vCPU / 1 GB）" and r["storage"] == "56 MB 已用" and r["state"] == "available"
    assert r["engine"] == "PostgreSQL 17.6.1.127" and r["endpoint"] == "db.abc.supabase.co:5432"
    assert r["note"] == "PITR 关 · 最近备份 2026-10-24 · 限 2 段 IP · 自定义域名"
    assert r["ref"] == "abc" and r["db_size_bytes"] == 56020115 and r["pitr"] is False
    assert r["network_restricted"] is True and r["custom_domain"] is True and r["created_at"] == "2026-06-14"
    body = [c for c in calls if c[0] == "POST"]
    assert len(body) == 1                                     # 只跑了一条 SELECT


def test_paused_project_skips_sql_and_says_how_to_resume(monkeypatch):
    calls = _install(monkeypatch, {
        ("GET", "/v1/projects"): [_proj("old", "p1", status="INACTIVE")],
        ("GET", "/v1/projects/p1/billing/addons"): {"selected_addons": []},
        ("GET", "/v1/projects/p1/database/backups"): {"pitr_enabled": True, "backups": []},
        ("GET", "/v1/projects/p1/network-restrictions"): {"config": {"dbAllowedCidrs": ["0.0.0.0/0"]}},
    })
    r = SupabaseProvider().list_databases()[0]
    assert r["state"] == "paused" and r["storage"] == "—" and r["class"] == "—"
    assert r["note"].startswith("已暂停（恢复：ops db resume") and "PITR 开" in r["note"] and "无备份" in r["note"] and "公网全开" in r["note"]
    assert not any(c[0] == "POST" for c in calls)


def test_missing_scope_warns_once_and_other_columns_survive(monkeypatch, capsys):
    calls = _install(monkeypatch, {
        ("GET", "/v1/projects"): [_proj("a", "r1"), _proj("b", "r2")],
        ("GET", "/v1/projects/r1/billing/addons"): None,
        ("GET", "/v1/projects/r2/billing/addons"): None,
        ("GET", "/v1/projects/r1/database/backups"): BACKUPS,
        ("GET", "/v1/projects/r2/database/backups"): BACKUPS,
        ("GET", "/v1/projects/r1/network-restrictions"): None,
        ("GET", "/v1/projects/r2/network-restrictions"): None,
        ("POST", "/v1/projects/r1/database/query"): [{"bytes": 2_500_000_000}],
        ("POST", "/v1/projects/r2/database/query"): [{"bytes": 10}],
    })
    rows = SupabaseProvider().list_databases()
    assert [r["class"] for r in rows] == ["—", "—"]
    assert rows[0]["storage"] == "2.50 GB 已用"
    err = capsys.readouterr().err
    assert err.count("读不到 addons") == 1 and err.count("读不到 network") == 1
    assert sum(1 for c in calls if "billing/addons" in c[1]) == 1          # 第二个项目不再白调


def test_pause_restore_and_project_use_expected_endpoints(monkeypatch):
    calls = _install(monkeypatch, {
        ("POST", "/v1/projects/abc/pause"): {},
        ("POST", "/v1/projects/abc/restore"): None,
        ("GET", "/v1/projects/abc"): _proj("acme-app", "abc"),
    })
    p = SupabaseProvider()
    assert p.pause("tok", "abc") == (True, "")
    ok, err = p.restore("tok", "abc")
    assert ok is False and "403" in err
    assert p.project("tok", "abc")["status"] == "ACTIVE_HEALTHY"
    assert ("POST", "/v1/projects/abc/pause") in calls


def test_token_for_label(monkeypatch):
    monkeypatch.setattr(SupabaseProvider, "_token", staticmethod(lambda a: "tok-" + a["account"]))
    monkeypatch.setattr(deploy_config, "cloud_accounts",
                        lambda cloud: [{"account": "x"}, {"account": "y"}] if cloud == "supabase" else [])
    p = SupabaseProvider()
    assert p.token_for("y") == "tok-y" and p.token_for("nope") == ""


def test_human_sizes():
    assert _human(None) == "—" and _human(56020115) == "56 MB" and _human(2_500_000_000) == "2.50 GB"


def test_storage_and_edge_functions(monkeypatch):
    calls = _install(monkeypatch, {
        ("GET", "/v1/projects"): [_proj("app", "abc"), _proj("old", "p1", status="INACTIVE")],
        ("GET", "/v1/projects/abc/storage/buckets"): [
            {"id": "avatars", "name": "avatars", "public": True, "created_at": "2026-01-02T03:04:05Z"},
            {"id": "private-docs", "name": "private-docs", "public": False, "created_at": 1767312245000}],
        ("GET", "/v1/projects/abc/functions"): [
            {"slug": "hello", "name": "hello", "status": "ACTIVE", "version": 11, "created_at": 1785644558099,
             "updated_at": 1788700640526, "verify_jwt": False}],
    })
    p = SupabaseProvider()
    st = p.list_storage()
    assert [s["name"] for s in st] == ["avatars", "private-docs"]
    assert st[0]["public"] is True and st[0]["note"].startswith("公开桶 ⚠") and st[0]["created"] == "2026-01-02"
    assert st[1]["created"] == "2026-01-02" and st[1]["domain"] == "abc.supabase.co/storage/v1"
    fn = p.list_serverless()
    assert len(fn) == 1 and fn[0]["name"] == "hello" and fn[0]["state"] == "active" and fn[0]["version"] == 11
    assert fn[0]["runtime"] == "Edge Function (Deno)" and fn[0]["memory"] is None
    assert fn[0]["last_modified"] == "2026-09-06" and "不校验 JWT ⚠" in fn[0]["note"]
    assert not any("/v1/projects/p1/" in c[1] for c in calls)      # 已暂停项目不调 storage/functions


def test_storage_scope_missing_warns_once(monkeypatch, capsys):
    _install(monkeypatch, {
        ("GET", "/v1/projects"): [_proj("a", "r1"), _proj("b", "r2")],
        ("GET", "/v1/projects/r1/storage/buckets"): None,
        ("GET", "/v1/projects/r2/storage/buckets"): None,
    })
    assert SupabaseProvider().list_storage() == []
    assert capsys.readouterr().err.count("读不到 buckets") == 1
