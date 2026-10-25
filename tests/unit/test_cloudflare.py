"""Cloudflare provider（只读）：zone 翻页、记录数 / 代理数 / SSL / 套餐 / Registrar 到期日，权限不足只提示一次。离线。"""
import datetime as dt

import pytest

from noc_ops import deploy_config
from noc_ops.providers.cloudflare import CloudflareProvider

pytestmark = pytest.mark.unit


def _ok(result, **info):
    return {"success": True, "result": result, "result_info": info}


def _install(monkeypatch, routes):
    """routes: {path 前缀: 响应 dict 或 None(=失败)}。记录被调用的 path 便于断言次数。"""
    calls = []

    def fake_get(self, token, path, quiet=False):
        calls.append(path)
        for prefix, resp in routes.items():
            if path.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(CloudflareProvider, "_get", fake_get)
    monkeypatch.setattr(CloudflareProvider, "_token", staticmethod(lambda a: "tok"))
    monkeypatch.setattr(deploy_config, "cloud_accounts",
                        lambda cloud: [{"account": "cf", "keychain_service": "noc-cloudflare"}] if cloud == "cloudflare" else [])
    return calls


def _zone(name, zid, **kw):
    z = {"id": zid, "name": name, "status": "active", "paused": False, "created_on": "2024-05-01T00:00:00Z",
         "plan": {"name": "Free Website"}, "name_servers": ["a.ns.cloudflare.com", "b.ns.cloudflare.com"],
         "account": {"id": "acct1"}}
    z.update(kw)
    return z


def test_full_row_with_registrar_expiry(monkeypatch):
    exp = (dt.date.today() + dt.timedelta(days=40)).isoformat() + "T00:00:00Z"
    calls = _install(monkeypatch, {
        "/zones?": _ok([_zone("example.com", "z1")], total_pages=1),
        "/zones/z1/dns_records?per_page=1&proxied=true": _ok([{}], total_count=7),
        "/zones/z1/dns_records?per_page=1": _ok([{}], total_count=12),
        "/zones/z1/settings/ssl": _ok({"id": "ssl", "value": "full"}),
        "/accounts/acct1/registrar/domains": _ok([{"name": "Example.COM", "expires_at": exp, "auto_renew": True}]),
    })
    rows = CloudflareProvider().list_domains()
    assert len(rows) == 1
    r = rows[0]
    assert r["registrar"] == "Cloudflare" and r["account"] == "cf" and r["reg_date"] == "2024-05-01"
    assert r["dns_records"] == 12 and r["dns_proxied"] == 7 and r["ssl_mode"] == "full" and r["plan"] == "Free Website"
    assert r["expire_date"] == exp[:10] and r["days_left"] == 40 and r["auto_renew"] is True
    assert r["note"] == "active · 12 条记录/7 代理 · SSL full · Free Website · 自动续费"
    assert r["name_servers"] == ["a.ns.cloudflare.com", "b.ns.cloudflare.com"]
    assert calls.count("/accounts/acct1/registrar/domains") == 1


def test_zone_pagination_and_registrar_cached_per_account(monkeypatch):
    calls = _install(monkeypatch, {
        "/zones?per_page=50&page=1": _ok([_zone("a.com", "z1")], total_pages=2),
        "/zones?per_page=50&page=2": _ok([_zone("b.com", "z2", paused=True)], total_pages=2),
        "/zones/z1/dns_records": _ok([], total_count=0),
        "/zones/z2/dns_records": _ok([], total_count=3),
        "/zones/z1/settings/ssl": _ok({"value": "strict"}),
        "/zones/z2/settings/ssl": _ok({"value": "off"}),
        "/accounts/acct1/registrar/domains": _ok([]),
    })
    rows = CloudflareProvider().list_domains()
    assert [r["domain"] for r in rows] == ["a.com", "b.com"]
    assert rows[1]["note"].startswith("active（已暂停） · 3 条记录")
    assert rows[1]["expire_date"] == "" and rows[1]["days_left"] is None
    assert calls.count("/accounts/acct1/registrar/domains") == 1        # 两个 zone 同一 account，只查一次


def test_missing_scope_warns_once_and_leaves_column_blank(monkeypatch, capsys):
    calls = _install(monkeypatch, {
        "/zones?": _ok([_zone("a.com", "z1"), _zone("b.com", "z2")], total_pages=1),
        "/zones/z1/dns_records": _ok([], total_count=5),
        "/zones/z2/dns_records": _ok([], total_count=6),
        "/zones/z1/settings/ssl": None,                     # 没有 Zone Settings:Read
        "/zones/z2/settings/ssl": None,
        "/accounts/acct1/registrar/domains": None,          # 没有 Registrar:Read
    })
    rows = CloudflareProvider().list_domains()
    assert [r["ssl_mode"] for r in rows] == [None, None]
    assert all("SSL" not in r["note"] for r in rows)
    assert [r["dns_records"] for r in rows] == [5, 6]       # 其它列照常
    err = capsys.readouterr().err
    assert err.count("读不到 ssl") == 1 and err.count("读不到 registrar") == 1
    assert sum(1 for c in calls if "/settings/ssl" in c) == 1          # 第二个 zone 不再白调


def test_no_token_skips_account_with_hint(monkeypatch, capsys):
    monkeypatch.setattr(CloudflareProvider, "_token", staticmethod(lambda a: ""))
    monkeypatch.setattr(deploy_config, "cloud_accounts",
                        lambda cloud: [{"account": "cf"}] if cloud == "cloudflare" else [])
    assert CloudflareProvider().list_domains() == []
    assert "add-generic-password" in capsys.readouterr().err


def test_r2_workers_and_cdn_rows(monkeypatch):
    calls = _install(monkeypatch, {
        "/accounts?": _ok([{"id": "acct1", "name": "Acme"}], total_pages=1),
        "/accounts/acct1/r2/buckets": _ok({"buckets": [{"name": "assets", "creation_date": "2025-03-01T00:00:00Z", "location": "APAC"}]}),
        "/accounts/acct1/workers/scripts": _ok([{"id": "edge-router", "created_on": "2025-01-01T00:00:00Z", "modified_on": "2025-09-09T10:00:00Z", "usage_model": "standard"}]),
        "/zones?": _ok([_zone("a.com", "z1", modified_on="2025-08-08T00:00:00Z"), _zone("b.com", "z2")], total_pages=1),
        "/zones/z1/dns_records?per_page=1&proxied=true": _ok([{}], total_count=4),
        "/zones/z2/dns_records?per_page=1&proxied=true": _ok([], total_count=0),
        "/zones/z1/settings/ssl": _ok({"value": "full"}),
    })
    p = CloudflareProvider()
    st = p.list_storage()
    assert st == [{"name": "assets", "cloud": "cloudflare", "account": "cf", "region": "APAC", "region_display": "APAC",
                   "flag": "🌐", "domain": "acct1.r2.cloudflarestorage.com", "created": "2025-03-01",
                   "storage_class": "", "cf_account": "Acme"}]
    fn = p.list_serverless()
    assert fn[0]["name"] == "edge-router" and fn[0]["runtime"] == "Cloudflare Workers" and fn[0]["last_modified"] == "2025-09-09"
    assert fn[0]["memory"] is None and fn[0]["region_display"] == "全球"
    cdn = p.list_cdn()
    assert [c["domain"] for c in cdn] == ["a.com"]                 # 无代理记录的 zone 不出现在 CDN
    c = cdn[0]
    assert c["id"] == "z1" and c["origin"] == "按 DNS 记录代理（4 条）" and c["cert"] == "SSL full"
    assert c["enabled"] is True and c["price_class"] == "Free Website" and c["last_modified"] == "2025-08-08"
    assert sum(1 for x in calls if "/settings/ssl" in x) == 1       # 只对有代理的 zone 查 SSL


def test_r2_without_scope_is_empty_with_one_hint(monkeypatch, capsys):
    _install(monkeypatch, {
        "/accounts?": _ok([{"id": "acct1"}, {"id": "acct2"}], total_pages=1),
        "/accounts/acct1/r2/buckets": None,
        "/accounts/acct2/r2/buckets": None,
    })
    assert CloudflareProvider().list_storage() == []
    err = capsys.readouterr().err
    assert err.count("读不到 r2") == 1 and "Workers R2 Storage:Read" in err
