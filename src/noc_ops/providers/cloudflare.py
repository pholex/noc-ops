"""Cloudflare 驱动（DNS zone → 「域名」tab，只读）。

list_domains 把每个 zone 当一条「域名」列进看板「域名」tab（registrar=Cloudflare），并尽量读全：
  - zone 状态 / 是否暂停 / 套餐 / 分配的 NS；
  - DNS 记录总数与其中走代理（橙云）的条数；
  - SSL 模式（off / flexible / full / strict）；
  - 若域名是在 Cloudflare Registrar 注册的，补上注册到期日与剩余天数（和 Route53 / 阿里云域名同一列）。
不是 compute 云——没有实例/存储/IAM，也没有 reboot（基类默认返回空/不实现）。**不做任何写操作。**

认证：Cloudflare API token（**密钥**）。按 noc 规矩**绝不进 ops-config.toml**，存 macOS Keychain，
运行时用 `security find-generic-password` 读取（token 不进配置、不进进程参数、不进 chat）。
配置 [[cloudflare.accounts]] 只放非密：account（标签，也作 Keychain account 名）+ keychain_service。
token 需要的只读权限：Zone:Read、DNS:Read、Zone Settings:Read；要显示注册到期日再加 Account → Registrar:Read。
缺某项权限只会少那一列（每类只提示一次），不会让整朵云失败。
"""
import datetime as _dt
import json
import subprocess
import sys
import urllib.error
import urllib.request

from ..collection import observed, optional
from .. import deploy_config, credentials
from .base import Provider

_API = "https://api.cloudflare.com/client/v4"
_SCOPE_HINT = {
    "dns": "DNS:Read",
    "ssl": "Zone Settings:Read",
    "registrar": "Account → Registrar:Read（只有 Cloudflare Registrar 注册的域名才有到期日）",
    "r2": "Account → Workers R2 Storage:Read",
    "workers": "Account → Workers Scripts:Read",
}


class CloudflareProvider(Provider):
    cloud = "cloudflare"
    cli = ""                        # 无 CLI：走 REST API（urllib，token 留在进程内不进 argv）
    display = "Cloudflare"
    flag = "🌐"
    order = 7                       # AWS=0 阿里=1 腾讯=2 火山=3 GCP=4 Azure=5 Oracle=6 Cloudflare=7

    @staticmethod
    @observed(result="token")
    def _token(account):
        """从 Keychain 读该账号的 API token（service/account 名来自配置，token 值只从环境变量或 Keychain 读取）。"""
        return credentials.token(account, "cloudflare")

    @observed(result="nullable")
    def _get(self, token, path, quiet=False):
        """GET Cloudflare API，返回整个响应 dict（含 result / result_info）；失败返回 None。
        失败默认打诊断到 stderr（不静默吞）；quiet=True 由调用方自己决定怎么提示。"""
        req = urllib.request.Request(_API + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if not quiet:
                print(f"  ⚠️ Cloudflare API {path}: HTTP {e.code}（token 权限/有效性？需 Zone:Read）", file=sys.stderr)
            return None
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            if not quiet:
                print(f"  ⚠️ Cloudflare API {path}: {e}", file=sys.stderr)
            return None
        if not data.get("success"):
            if not quiet:
                print(f"  ⚠️ Cloudflare API {path}: {data.get('errors')}", file=sys.stderr)
            return None
        return data

    def _api(self, token, path):
        """GET 并只取 result（列表或对象）；失败返回 []。"""
        data = self._get(token, path)
        return data.get("result", []) if data else []

    def _zones(self, token):
        """全部 zone（按 result_info 翻页，别只拿第一页 50 个）。"""
        out, page = [], 1
        while True:
            data = self._get(token, f"/zones?per_page=50&page={page}")
            if not data:
                break
            out.extend(data.get("result") or [])
            info = data.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                break
            page += 1
        return out

    # ---- 每类补充信息：权限不足只提示一次、后续 zone 直接跳过，避免 N 个 zone 刷 N 条告警 ----
    def _feature(self, token, label, key, path, state):
        if state.get(key) is False:
            return None
        data = self._get(token, path, quiet=True)
        if data is None:
            if state.get(key) is None:                      # 第一次失败才提示，并记为不可用
                print(f"  ⚠️ Cloudflare {label}: 读不到 {key}（token 需 {_SCOPE_HINT[key]}），该列留空", file=sys.stderr)
            state[key] = False
            return None
        state[key] = True
        return data

    @optional
    def _record_count(self, token, label, zid, state, proxied=None):
        q = f"/zones/{zid}/dns_records?per_page=1" + ("&proxied=true" if proxied else "")
        data = self._feature(token, label, "dns", q, state)
        if data is None:
            return None
        info = data.get("result_info") or {}
        return int(info.get("total_count", len(data.get("result") or [])))

    @optional
    def _ssl_mode(self, token, label, zid, state):
        data = self._feature(token, label, "ssl", f"/zones/{zid}/settings/ssl", state)
        return ((data or {}).get("result") or {}).get("value") if data else None

    @optional
    def _registrar(self, token, label, account_id, state, cache):
        """该 Cloudflare account 下 Registrar 注册的域名 {name: {expires_at, auto_renew}}；每个 account 只查一次。"""
        if account_id in cache:
            return cache[account_id]
        data = self._feature(token, label, "registrar", f"/accounts/{account_id}/registrar/domains", state)
        m = {}
        for d in (data or {}).get("result") or []:
            if d.get("name"):
                m[d["name"].lower()] = {"expires_at": d.get("expires_at") or "", "auto_renew": bool(d.get("auto_renew"))}
        cache[account_id] = m
        return m

    def _accounts(self, token):
        """token 能看到的 Cloudflare account（R2 / Workers 挂在 account 下，不在 zone 下）。"""
        out, page = [], 1
        while True:
            data = self._get(token, f"/accounts?per_page=50&page={page}")
            if not data:
                break
            out.extend(a for a in (data.get("result") or []) if a.get("id"))
            info = data.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                break
            page += 1
        return out

    def _each_account(self):
        """(配置标签, token) 生成器；Keychain 没 token 的账号提示并跳过。"""
        for a in deploy_config.cloud_accounts("cloudflare"):
            label = a.get("account", "cloudflare")
            token = self._token(a)
            if not token:
                print(f"  ⚠️ Cloudflare {label}: token_env / Keychain 取 token 失败 "
                      f"(security add-generic-password -s {a.get('keychain_service','noc-cloudflare')} -a {label} -w)",
                      file=sys.stderr)
                continue
            yield label, token

    def list_storage(self):
        """R2 桶 → 「存储」tab。需要 Account → Workers R2 Storage:Read；没权限只提示一次、整类留空。"""
        out = []
        for label, token in self._each_account():
            state = {}
            for acct in self._accounts(token):
                aid = acct["id"]
                data = self._feature(token, label, "r2", f"/accounts/{aid}/r2/buckets", state)
                if data is None:
                    continue
                res = data.get("result") or {}
                buckets = res.get("buckets") if isinstance(res, dict) else res
                for b in buckets or []:
                    loc = b.get("location") or ""
                    out.append({
                        "name": b.get("name"), "cloud": "cloudflare", "account": label,
                        "region": loc or "auto", "region_display": loc or "自动", "flag": self.flag,
                        "domain": f"{aid}.r2.cloudflarestorage.com",     # S3 兼容端点（账号级，不含密钥）
                        "created": (b.get("creation_date") or "")[:10],
                        "storage_class": b.get("storage_class") or "",
                        "cf_account": acct.get("name") or "",
                    })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    def list_serverless(self):
        """Workers 脚本 → 「函数」tab。需要 Account → Workers Scripts:Read。"""
        out = []
        for label, token in self._each_account():
            state = {}
            for acct in self._accounts(token):
                aid = acct["id"]
                data = self._feature(token, label, "workers", f"/accounts/{aid}/workers/scripts", state)
                if data is None:
                    continue
                for s in data.get("result") or []:
                    out.append({
                        "name": s.get("id"), "cloud": "cloudflare", "account": label,
                        "region": "global", "region_display": "全球", "flag": self.flag,
                        "kind": "worker", "runtime": "Cloudflare Workers",
                        "memory": None, "timeout": None, "code_mb": None,
                        "last_modified": (s.get("modified_on") or s.get("created_on") or "")[:10],
                        "usage_model": s.get("usage_model") or "",
                        "cf_account": acct.get("name") or "",
                    })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    def list_cdn(self):
        """Cloudflare 本身就是 CDN：开了代理（橙云）记录的 zone 各出一行，和 CloudFront 并排。
        需要 DNS:Read 才知道哪些 zone 有代理记录；读不到则整类留空。"""
        out = []
        for label, token in self._each_account():
            state = {}
            for z in self._zones(token):
                zid = z.get("id", "")
                proxied = self._record_count(token, label, zid, state, proxied=True)
                if not proxied:
                    continue
                ssl = self._ssl_mode(token, label, zid, state)
                out.append({
                    "id": zid, "domain": z.get("name"), "aliases": "",
                    "origin": f"按 DNS 记录代理（{proxied} 条）",
                    "status": z.get("status", ""), "enabled": not z.get("paused"),
                    "cert": f"SSL {ssl}" if ssl else "—",
                    "price_class": (z.get("plan") or {}).get("name") or "",
                    "last_modified": (z.get("modified_on") or "")[:10],
                    "account": label, "cloud": "cloudflare", "partition": "global", "comment": "",
                })
        return out

    @staticmethod
    def _days_left(expires_at):
        if not expires_at:
            return None
        try:
            exp = _dt.date.fromisoformat(expires_at[:10])
        except ValueError:
            return None
        return (exp - _dt.date.today()).days

    def list_domains(self):
        out = []
        for label, token in self._each_account():
            state, reg_cache = {}, {}
            for z in self._zones(token):
                zid = z.get("id", "")
                name = (z.get("name") or "").lower()
                status = z.get("status", "")
                plan = (z.get("plan") or {}).get("name") or ""
                total = self._record_count(token, label, zid, state)
                proxied = self._record_count(token, label, zid, state, proxied=True) if total is not None else None
                ssl = self._ssl_mode(token, label, zid, state)
                reg = self._registrar(token, label, (z.get("account") or {}).get("id", ""), state, reg_cache).get(name, {})
                expires_at = reg.get("expires_at", "")

                parts = [status + ("（已暂停）" if z.get("paused") else "")]
                if total is not None:
                    parts.append(f"{total} 条记录" + (f"/{proxied} 代理" if proxied is not None else ""))
                if ssl:
                    parts.append(f"SSL {ssl}")
                if plan:
                    parts.append(plan)
                if reg.get("auto_renew"):
                    parts.append("自动续费")
                out.append({
                    "domain": z.get("name"),
                    "type": "DNS zone",
                    "registrar": "Cloudflare",
                    "account": label,                       # 用配置标签，跨 tab 一致（非 CF 内部 account id）
                    "reg_date": (z.get("created_on") or "")[:10],
                    "expire_date": expires_at[:10],         # 只有 Cloudflare Registrar 注册的域名才有
                    "days_left": self._days_left(expires_at),
                    "note": " · ".join(parts),
                    # 机读补充（agent 从 data.json 读；看板「备注」列是上面的摘要）
                    "zone_id": zid,
                    "status": status,
                    "paused": bool(z.get("paused")),
                    "plan": plan,
                    "name_servers": z.get("name_servers") or [],
                    "dns_records": total,
                    "dns_proxied": proxied,
                    "ssl_mode": ssl,
                    "auto_renew": reg.get("auto_renew"),
                })
        return out
