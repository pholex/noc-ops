"""Cloudflare 驱动（Containers / D1 / DNS / R2 / Workers / CDN，只读）。

list_domains 把每个 zone 当一条「域名」列进看板「域名」tab（registrar=Cloudflare），并尽量读全：
  - zone 状态 / 是否暂停 / 套餐 / 分配的 NS；
  - DNS 记录总数与其中走代理（橙云）的条数；
  - SSL 模式（off / flexible / full / strict）；
  - 若域名是在 Cloudflare Registrar 注册的，补上注册到期日与剩余天数（和 Route53 / 阿里云域名同一列）。
Containers 实例不提供 SSH 或 reboot；D1 只读元数据，不执行 SQL。**不做任何写操作。**

认证：Cloudflare API token（**密钥**）。按 noc 规矩**绝不进 ops-config.toml**，存 macOS Keychain，
运行时用 `security find-generic-password` 读取（token 不进配置、不进进程参数、不进 chat）。
配置 [[cloudflare.accounts]] 只放非密：account（标签，也作 Keychain account 名）+ keychain_service。
token 需要的只读权限：Zone:Read、DNS:Read、Zone Settings:Read；要显示注册到期日再加 Account → Registrar:Read。
缺某项权限只会少那一列（每类只提示一次），不会让整朵云失败。
"""
import datetime as _dt
import json
import re
from decimal import Decimal, InvalidOperation
from math import isfinite
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from ..collection import failed, observed, optional
from .. import deploy_config, credentials
from .base import Provider

_API = "https://api.cloudflare.com/client/v4"
_SCOPE_HINT = {
    "dns": "DNS:Read",
    "ssl": "Zone Settings:Read",
    "registrar": "Account → Registrar:Read（只有 Cloudflare Registrar 注册的域名才有到期日）",
    "r2": "Account → Workers R2 Storage:Read",
    "workers": "Account → Workers Scripts:Read",
    "containers": "Account → Workers Containers:Read",
    "d1": "Account → D1:Read",
    "accounts": "Account → Account Settings:Read",
    "billing": "Billing:Read",
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
        if not isinstance(data, dict) or not data.get("success"):
            if not quiet:
                print("  ⚠️ Cloudflare API：请求失败或响应无效", file=sys.stderr)
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
        """token 能看到的 Cloudflare account；分页失败由采集会话保留旧清单。"""
        return self._list_pages(token, "/accounts", "accounts", per_page=50)

    def _list_pages(self, token, path, feature, *, cursor=False, identity="id", per_page=100):
        """Cloudflare 页码 / 游标列表；缺页、重复页和无效响应不能当成空清单。"""
        out, seen_ids, seen_tokens = [], set(), set()
        page, page_token = 1, None
        while True:
            query = {"per_page": per_page}
            if cursor:
                if page_token:
                    query["page_token"] = page_token
            else:
                query["page"] = page
            data = self._get(token, path + "?" + urlencode(query), quiet=True)
            if data is None:
                failed()
                print(f"  ⚠️ Cloudflare {feature} 查询失败，请检查网络及 {_SCOPE_HINT[feature]} 权限", file=sys.stderr)
                return []
            rows, info = data.get("result"), data.get("result_info") or {}
            if (not isinstance(rows, list) or not isinstance(info, dict)
                    or any(not isinstance(r, dict) or not isinstance(r.get(identity), str)
                           or not r[identity] for r in rows)):
                failed("invalid_response")
                return []
            ids = [r[identity] for r in rows]
            if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                failed("invalid_pagination")
                return []
            seen_ids.update(ids)
            out.extend(rows)
            if cursor:
                page_token = info.get("next_page_token")
                if not page_token:
                    return out
                if not isinstance(page_token, str) or page_token in seen_tokens:
                    failed("invalid_pagination")
                    return []
                seen_tokens.add(page_token)
            else:
                total, pages = info.get("total_count"), info.get("total_pages")
                if any(v is not None and (type(v) is not int or v < 0) for v in (total, pages)):
                    failed("invalid_pagination")
                    return []
                if total is not None:
                    if len(out) >= total:
                        return out
                elif pages is not None:
                    if page >= pages:
                        return out
                elif len(rows) < per_page:
                    return out
                if not rows:
                    failed("invalid_pagination")
                    return []
                page += 1

    def list_instances(self):
        """Containers 的实际容器实例（含未运行实例），不把应用配置冒充机器。"""
        out = []
        for label, token in self._each_account():
            for acct in self._accounts(token):
                aid = acct["id"]
                path = f"/accounts/{quote(aid, safe='')}/containers/applications"
                for app in self._list_pages(token, path, "containers", cursor=True):
                    instances = self._list_pages(token, path + "/" + quote(app["id"], safe="")
                                                 + "/instances-v2", "containers", cursor=True)
                    for inst in instances:
                        loc = inst.get("location") or {}
                        config = inst.get("configuration") or {}
                        status = inst.get("status") or {}
                        region = loc.get("region") or "unknown"
                        shape = [f"{config['vcpu']} vCPU"] if config.get("vcpu") is not None else []
                        if config.get("memory") is not None:
                            shape.append(f"{config['memory']} MiB")
                        out.append({
                            "id": f"cloudflare:{aid}:{app['id']}:{inst['id']}",
                            "instance_id": inst["id"], "name": inst.get("name") or inst["id"],
                            "cloud": self.cloud, "profile": label, "kind": "container",
                            "cf_account": acct.get("name") or "", "cf_account_id": aid,
                            "application": app.get("name") or app["id"], "application_id": app["id"],
                            "image": inst.get("image") or "", "type": "Container" + (" · " + " / ".join(shape) if shape else ""),
                            "vcpu": config.get("vcpu"), "memory_mib": config.get("memory"),
                            "disk_mb": config.get("disk"), "state": status.get("state") or "unknown",
                            "status_updated_at": status.get("updated_at") or "", "exit_code": status.get("exit_code"),
                            "started_at": inst.get("started_at") or "",
                            "region": region, "region_display": region if region != "unknown" else "未知",
                            "flag": self.flag, "az": loc.get("name") or "",
                            "public_ip": None, "private_ip": None, "key_name": None,
                            "ssh_alias": None, "ssh_user": None, "key_file": None,
                            "connect": "none", "proxyjump": None, "verified": False,
                        })
        return sorted(out, key=lambda r: (r["profile"], r["application"], r["name"], r["id"]))

    @optional
    def _d1_details(self, token, path):
        data = self._get(token, path, quiet=True)
        if data is None:
            failed()
            return {}
        result = data.get("result")
        if not isinstance(result, dict):
            failed("invalid_response")
            return {}
        size, replicas = result.get("file_size"), result.get("read_replication")
        if (size is not None and (type(size) not in (int, float) or size < 0)
                or replicas is not None and not isinstance(replicas, dict)):
            failed("invalid_response")
            return {}
        return result

    def list_databases(self):
        """D1 元数据；详情为可选补充，不查询业务数据或已弃用的表数量。"""
        out = []
        for label, token in self._each_account():
            for acct in self._accounts(token):
                aid = acct["id"]
                path = f"/accounts/{quote(aid, safe='')}/d1/database"
                for db in self._list_pages(token, path, "d1", identity="uuid"):
                    detail = self._d1_details(token, path + "/" + quote(db["uuid"], safe=""))
                    jurisdiction = db.get("jurisdiction") or detail.get("jurisdiction") or ""
                    size = detail.get("file_size")
                    replication = (detail.get("read_replication") or {}).get("mode") or ""
                    out.append({
                        "id": f"cloudflare:{aid}:d1:{db['uuid']}", "uuid": db["uuid"],
                        "name": db.get("name") or db["uuid"], "cloud": self.cloud, "account": label,
                        "cf_account": acct.get("name") or "", "cf_account_id": aid,
                        "region": jurisdiction or "auto", "region_display": jurisdiction or "自动",
                        "jurisdiction": jurisdiction, "flag": self.flag,
                        "engine": "D1 (SQLite)", "class": "Serverless", "state": "—", "endpoint": "—",
                        "storage": f"{size / 1024 ** 2:.2f} MiB" if size is not None else "—",
                        "db_size_bytes": size, "read_replication": replication,
                        "created": (db.get("created_at") or detail.get("created_at") or "")[:10],
                        "version": db.get("version") or detail.get("version") or "",
                        "note": "D1" + (f" · 只读副本 {replication}" if replication else ""),
                    })
        return sorted(out, key=lambda r: (r["account"], r["name"], r["id"]))

    def _billing_data(self, token, path):
        data = self._get(token, path, quiet=True)
        if data is None:
            failed()
            print("  ⚠️ Cloudflare 成本查询失败，请检查网络及 Billing:Read 权限。", file=sys.stderr)
        return data

    def list_billing(self, months=3):
        """PayGo 按量费用，按实际账期汇总；不是自然月用量或完整发票。"""
        now = _dt.datetime.now(_dt.timezone.utc)
        index = now.year * 12 + now.month - months
        start = _dt.datetime(index // 12, index % 12 + 1, 1, tzinfo=_dt.timezone.utc)
        # 一次覆盖整段账期起始日，不按自然月拆请求（API 要求包含订阅 anchor day）。
        query = urlencode({"from": start.date().isoformat(), "to": now.date().isoformat()})
        out = []
        for label, token in self._each_account():
            for acct in self._accounts(token):
                aid = acct["id"]
                path = f"/accounts/{quote(aid, safe='')}/billable-usage"
                info = self._billing_data(token, path + "/info")
                if info is None:
                    continue
                coverage = info.get("result")
                if not isinstance(coverage, dict) or type(coverage.get("covered")) is not bool:
                    failed("invalid_response")
                    continue
                if not coverage["covered"]:
                    failed("billing_not_covered")
                    print("  ⚠️ Cloudflare 账号不在按量费用 API 的覆盖范围内，保留旧成本数据。", file=sys.stderr)
                    continue
                data = self._billing_data(token, path + "?" + query)
                if data is None:
                    continue
                rows = data.get("result")
                if not isinstance(rows, list):
                    failed("invalid_response")
                    continue
                summed = {}
                for row in rows:
                    try:
                        service, currency = row["ServiceName"], row["BillingCurrency"]
                        period = _dt.datetime.fromisoformat(row["BillingPeriodStart"].replace("Z", "+00:00"))
                        amount = Decimal(str(row["ContractedCost"]))
                        if (not isinstance(service, str) or not service.strip()
                                or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
                                or period.tzinfo is None or not amount.is_finite()
                                or row.get("BillingAccountId", aid) != aid
                                or row.get("ChargeCategory", "Usage") != "Usage"):
                            raise ValueError("invalid billing row")
                        period = period.astimezone(_dt.timezone.utc)
                        if not start <= period <= now:
                            continue
                        key = (service, currency, period.isoformat())
                        summed[key] = summed.get(key, Decimal(0)) + amount
                    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
                        failed("invalid_response")
                for (service, currency, period_start), amount in summed.items():
                    value = float(amount)
                    if not isfinite(value):
                        failed("invalid_response")
                        continue
                    out.append({
                        "cloud": self.cloud, "account": label, "cf_account_id": aid,
                        "cf_account": acct.get("name") or "", "service": service,
                        "month": period_start[:7], "amount": value, "currency": currency,
                        "period_start": period_start, "period_basis": "billing_cycle", "partial": True,
                        "fetched_at": now.isoformat(timespec="seconds"),
                        "source": "Cloudflare Billable Usage v1", "cost_basis": "按量费用 / 账期",
                    })
        return sorted(out, key=lambda r: (r["account"], r["cf_account_id"], r["period_start"], r["service"], r["currency"]))

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
