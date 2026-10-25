"""Supabase 驱动（托管 Postgres 项目 → 「数据库」tab）。

list_databases 把 Supabase 的**项目**（每个=一个托管 Postgres 实例）列进看板「数据库」tab，并尽量读全：
  - 状态（ACTIVE_HEALTHY / INACTIVE=已暂停 / …）、区域、Postgres 版本、连接端点；
  - 算力档位（Micro / Small …，含 vCPU / 内存）→「规格」列；
  - 库实际占用（经 Management API 的 SQL 端点查 pg_database_size，只 SELECT）→「存储」列；
  - PITR 是否开启、最近一次备份、网络访问限制是否收紧、是否挂自定义域名 →「用途/备注」列的摘要。
写操作只有两项，且都由 `ops db pause|resume` 经确认后调用：**暂停项目 / 恢复已暂停的项目**（可逆）。
不提供删项目、改配置之类的操作。Supabase 不是传统 IaaS：无实例/存储桶/IAM，不实现 reboot。

认证：Supabase **Management API** 的 Personal Access Token（账号级，**密钥**）。按 noc 规矩**绝不进
ops-config.toml**，存 macOS Keychain，运行时 `security find-generic-password` 读取（token 不进配置、
不进进程参数、不进 chat）。注意这是**账号级 PAT**（列所有项目），不是某项目的 anon/service_role key。
配置 [[supabase.accounts]] 只放非密：account（标签，也作 Keychain account 名）+ keychain_service。
⚠️ 不要调 /postgrest 端点：它的响应里带项目 JWT secret，没有任何只读场景需要它。
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

from ..collection import observed, optional
from .. import deploy_config, credentials
from .base import Provider

_API = "https://api.supabase.com"
_UA = "noc-ops/1.0"
# 项目状态 → 看板状态灯用的短词
_STATE = {
    "ACTIVE_HEALTHY": "available", "ACTIVE_UNHEALTHY": "unhealthy",
    "INACTIVE": "paused", "PAUSING": "pausing", "PAUSE_FAILED": "pause-failed",
    "RESTORING": "restoring", "RESTORE_FAILED": "restore-failed",
    "COMING_UP": "starting", "GOING_DOWN": "stopping", "UPGRADING": "upgrading",
    "INIT_FAILED": "init-failed", "REMOVED": "removed",
}
_SCOPE_HINT = {
    "addons": "billing 读权限", "backups": "database 读权限",
    "network": "network-restrictions 读权限", "size": "database query 权限（只跑 SELECT）",
    "buckets": "storage 读权限", "functions": "functions 读权限",
}


def _ms_date(v):
    """Supabase 的 created_at/updated_at 有的是 ISO 串、有的是毫秒时间戳；统一成 YYYY-MM-DD。"""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(v / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:10]


def _human(nbytes):
    if nbytes is None:
        return "—"
    return f"{nbytes / 1e9:.2f} GB" if nbytes >= 1e9 else f"{nbytes / 1e6:.0f} MB"


class SupabaseProvider(Provider):
    cloud = "supabase"
    cli = ""                        # 无 CLI：走 Management API（urllib，token 留进程内不进 argv）
    display = "Supabase"
    flag = "🌐"
    order = 11

    @staticmethod
    @observed(result="token")
    def _token(account):
        """从 Keychain 读 Management API PAT（service/account 名来自配置，token 值只从环境变量或 Keychain 读取）。"""
        return credentials.token(account, "supabase")

    def token_for(self, label):
        """按配置标签找账号并读 token；`ops db` 用。没配置或 Keychain 没有则返回 ""。"""
        for a in deploy_config.cloud_accounts("supabase"):
            if a.get("account", "supabase") == label:
                return self._token(a)
        return ""

    @observed(result="pair")
    def _call(self, token, path, method="GET", body=None, quiet=False):
        """调 Management API，返回 (ok, payload)：ok 时 payload 是解析后的 JSON，失败时是错误说明字串。

        ⚠️ 必须带 User-Agent：api.supabase.com 在 Cloudflare 后面，默认 `Python-urllib` UA 会被
        Cloudflare 拦成 HTTP 403 `error code: 1010`（按浏览器签名封禁）。给个具名 UA 即可绕过。
        """
        headers = {"Authorization": "Bearer " + token, "User-Agent": _UA}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(_API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return True, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except OSError:
                detail = ""
            msg = f"HTTP {e.code} {detail}".strip()
            if not quiet:
                print(f"  ⚠️ Supabase API {path}: {msg}（PAT 权限/有效性？需 Management API token）", file=sys.stderr)
            return False, msg
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            if not quiet:
                print(f"  ⚠️ Supabase API {path}: {e}", file=sys.stderr)
            return False, str(e)

    def _api(self, token, path):
        """GET 并只取 JSON；失败返回 []（兼容旧调用方）。"""
        ok, payload = self._call(token, path)
        return payload if ok else []

    # ---- 每类补充信息：权限不足只提示一次、后续项目直接跳过 ----
    def _feature(self, token, label, key, path, state, method="GET", body=None):
        if state.get(key) is False:
            return None
        ok, payload = self._call(token, path, method=method, body=body, quiet=True)
        if not ok:
            if state.get(key) is None:
                print(f"  ⚠️ Supabase {label}: 读不到 {key}（PAT 需 {_SCOPE_HINT[key]}）：{payload}；该列留空",
                      file=sys.stderr)
            state[key] = False
            return None
        state[key] = True
        return payload

    @optional
    def _addons(self, token, label, ref, state):
        """(算力档位名, vCPU, 内存 GB, pitr 加购?, 自定义域名加购?)"""
        data = self._feature(token, label, "addons", f"/v1/projects/{ref}/billing/addons", state)
        if not isinstance(data, dict):
            return None
        out = {"compute": "", "cpu": None, "memory_gb": None, "pitr_addon": False, "custom_domain": False}
        for a in data.get("selected_addons") or []:
            t, v = a.get("type"), a.get("variant") or {}
            if t == "compute_instance":
                meta = v.get("meta") or {}
                out.update(compute=v.get("name") or v.get("id") or "", cpu=meta.get("cpu_cores"), memory_gb=meta.get("memory_gb"))
            elif t == "pitr":
                out["pitr_addon"] = True
            elif t == "custom_domain":
                out["custom_domain"] = True
        return out

    @optional
    def _backups(self, token, label, ref, state):
        data = self._feature(token, label, "backups", f"/v1/projects/{ref}/database/backups", state)
        if not isinstance(data, dict):
            return None
        done = [b for b in data.get("backups") or [] if b.get("status") == "COMPLETED"]
        latest = max((b.get("inserted_at") or "" for b in done), default="")
        return {"pitr": bool(data.get("pitr_enabled")), "walg": bool(data.get("walg_enabled")),
                "backup_latest": latest[:10], "backup_count": len(done)}

    @optional
    def _network(self, token, label, ref, state):
        data = self._feature(token, label, "network", f"/v1/projects/{ref}/network-restrictions", state)
        if not isinstance(data, dict):
            return None
        cidrs = (data.get("config") or {}).get("dbAllowedCidrs") or []
        open_all = not cidrs or "0.0.0.0/0" in cidrs
        return {"network_restricted": not open_all, "allowed_cidrs": len(cidrs)}

    @optional
    def _db_size(self, token, label, ref, state):
        """只 SELECT：pg_database_size(current_database())。项目没在跑（已暂停等）时不查。"""
        data = self._feature(token, label, "size", f"/v1/projects/{ref}/database/query", state,
                             method="POST", body={"query": "select pg_database_size(current_database()) as bytes"})
        if isinstance(data, list) and data and isinstance(data[0], dict):
            try:
                return int(data[0].get("bytes"))
            except (TypeError, ValueError):
                return None
        return None

    # ---- 单项目：ops db 用 ----
    def project(self, token, ref):
        ok, payload = self._call(token, f"/v1/projects/{ref}", quiet=True)
        return payload if ok and isinstance(payload, dict) else None

    def pause(self, token, ref):
        """暂停项目（可逆，恢复用 restore）。返回 (ok, 说明)。"""
        ok, payload = self._call(token, f"/v1/projects/{ref}/pause", method="POST", quiet=True)
        return ok, ("" if ok else str(payload))

    def restore(self, token, ref):
        """恢复已暂停（INACTIVE）的项目。返回 (ok, 说明)。"""
        ok, payload = self._call(token, f"/v1/projects/{ref}/restore", method="POST", quiet=True)
        return ok, ("" if ok else str(payload))

    def _each_account(self):
        """(配置标签, token, 项目列表) 生成器；Keychain 没 token 或列不到项目的账号提示并跳过。"""
        for a in deploy_config.cloud_accounts("supabase"):
            label = a.get("account", "supabase")
            token = self._token(a)
            if not token:
                print(f"  ⚠️ Supabase {label}: token_env / Keychain 取 token 失败 "
                      f"(security add-generic-password -s {a.get('keychain_service','noc-supabase')} -a {label} -w)",
                      file=sys.stderr)
                continue
            projects = self._api(token, "/v1/projects")
            if isinstance(projects, list):
                yield label, token, projects

    def list_storage(self):
        """Storage 桶 → 「存储」tab（每个项目的桶，公开桶在 note 里标出）。"""
        out = []
        for label, token, projects in self._each_account():
            state = {}
            for p in projects:
                ref = p.get("ref") or p.get("id") or ""
                if p.get("status") == "INACTIVE":          # 已暂停的项目 Storage API 不可达
                    continue
                data = self._feature(token, label, "buckets", f"/v1/projects/{ref}/storage/buckets", state)
                if not isinstance(data, list):
                    continue
                for b in data:
                    out.append({
                        "name": b.get("name") or b.get("id"), "cloud": "supabase", "account": label,
                        "region": p.get("region", ""), "region_display": p.get("region", ""), "flag": self.flag,
                        "domain": f"{ref}.supabase.co/storage/v1",
                        "created": _ms_date(b.get("created_at")),
                        "public": bool(b.get("public")),
                        "project": p.get("name"), "ref": ref,
                        "note": ("公开桶 ⚠" if b.get("public") else "私有") + f" · 项目 {p.get('name')}",
                    })
        out.sort(key=lambda x: (x["account"], x.get("project") or "", x["name"] or ""))
        return out

    def list_serverless(self):
        """Edge Functions → 「函数」tab（Supabase 不暴露内存/超时，留空）。"""
        out = []
        for label, token, projects in self._each_account():
            state = {}
            for p in projects:
                ref = p.get("ref") or p.get("id") or ""
                if p.get("status") == "INACTIVE":
                    continue
                data = self._feature(token, label, "functions", f"/v1/projects/{ref}/functions", state)
                if not isinstance(data, list):
                    continue
                for f in data:
                    status = (f.get("status") or "").lower()
                    out.append({
                        "name": f.get("slug") or f.get("name"), "cloud": "supabase", "account": label,
                        "region": p.get("region", ""), "region_display": p.get("region", ""), "flag": self.flag,
                        "kind": "edge-function", "runtime": "Edge Function (Deno)",
                        "memory": None, "timeout": None, "code_mb": None,
                        "last_modified": _ms_date(f.get("updated_at") or f.get("created_at")),
                        "state": status, "version": f.get("version"),
                        "verify_jwt": bool(f.get("verify_jwt")),
                        "project": p.get("name"), "ref": ref,
                        "note": f"项目 {p.get('name')} · v{f.get('version')}" + ("" if f.get("verify_jwt") else " · 不校验 JWT ⚠"),
                    })
        out.sort(key=lambda x: (x["account"], x.get("project") or "", x["name"] or ""))
        return out

    def list_databases(self):
        out = []
        for a in deploy_config.cloud_accounts("supabase"):
            label = a.get("account", "supabase")
            token = self._token(a)
            if not token:
                print(f"  ⚠️ Supabase {label}: token_env / Keychain 取 token 失败 "
                      f"(security add-generic-password -s {a.get('keychain_service','noc-supabase')} -a {label} -w)",
                      file=sys.stderr)
                continue
            projects = self._api(token, "/v1/projects")
            if not isinstance(projects, list):
                continue
            state = {}
            for p in projects:
                ref = p.get("ref") or p.get("id") or ""
                db = p.get("database") or {}
                ver = db.get("version") or db.get("postgres_engine") or ""
                host = db.get("host") or (f"db.{ref}.supabase.co" if ref else "")
                raw = p.get("status", "") or ""
                running = raw == "ACTIVE_HEALTHY"

                addons = self._addons(token, label, ref, state) or {}
                backups = self._backups(token, label, ref, state) or {}
                network = self._network(token, label, ref, state) or {}
                size = self._db_size(token, label, ref, state) if running else None

                cls = addons.get("compute") or "—"
                if addons.get("cpu") and addons.get("memory_gb"):
                    cls = f"{cls}（{addons['cpu']} vCPU / {addons['memory_gb']} GB）"

                parts = []
                if raw == "INACTIVE":
                    parts.append("已暂停（恢复：ops db resume <名>）")
                if backups:
                    parts.append("PITR 开" if backups["pitr"] else "PITR 关")
                    parts.append(f"最近备份 {backups['backup_latest']}" if backups["backup_latest"] else "无备份")
                if network:
                    parts.append(f"限 {network['allowed_cidrs']} 段 IP" if network["network_restricted"] else "公网全开")
                if addons.get("custom_domain"):
                    parts.append("自定义域名")

                out.append({
                    "name": p.get("name"), "cloud": "supabase", "account": label,
                    "region": p.get("region", ""), "region_display": p.get("region", ""), "flag": self.flag,
                    "engine": ("PostgreSQL " + str(ver)).strip(),
                    "class": cls,
                    "storage": (_human(size) + " 已用") if size is not None else "—",
                    "state": _STATE.get(raw, raw.lower() or "unknown"),
                    "endpoint": (host + ":5432") if host else "—",
                    "note": " · ".join(parts),               # 用户在配置里写了「用途」则以配置为准（sync 合并）
                    # 机读补充（agent 从 data.json 读）
                    "ref": ref,
                    "status_raw": raw,
                    "created_at": (p.get("created_at") or "")[:10],
                    "organization": p.get("organization_slug") or "",
                    "compute": addons.get("compute") or "",
                    "cpu": addons.get("cpu"),
                    "memory_gb": addons.get("memory_gb"),
                    "db_size_bytes": size,
                    "pitr": backups.get("pitr"),
                    "backup_latest": backups.get("backup_latest"),
                    "network_restricted": network.get("network_restricted"),
                    "custom_domain": addons.get("custom_domain"),
                })
        out.sort(key=lambda d: (d["account"], d["name"] or ""))
        return out
