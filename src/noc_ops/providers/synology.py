"""Synology DSM Provider（群晖 NAS，只读）。

认证：DSM Web API Session（account/passwd → sid，30 min 无操作过期，缓存 25 min）
  POST/GET /webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=login&...  → sid

数据：
  SYNO.Core.System        → 型号 / DSM 版本 / CPU / RAM / 温度
  SYNO.Storage.CGI.Storage → 卷容量 / 磁盘数

多账号配置（ops-config.toml）：
  [[synology.accounts]]
  account          = "sample-resource-13"        # 看板展示标签
  url              = "https://192.0.2.12:5001"
  username         = "nas-admin"
  keychain_service = "noc-synology"    # Keychain service 名
  keychain_account = "nas-admin@192.0.2.12"   # Keychain account（默认 username@host）

密码存 macOS Keychain：
  security add-generic-password -U -s noc-synology -a nas-admin@192.0.2.12 -T /usr/bin/security -w
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from .. import deploy_config
from .. import netprobe
from ..netprobe import NET_LABEL
from .base import Provider

# ─── HTTP / SSL ───────────────────────────────────────────────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "noc-ops/1.0"})
    # 30s：Tailscale 冷启动（DERP→直连握手）可能 >15s，放宽以减少假超时；
    # 真宕机的机器由 refresh_local 账号级沿用兜底，多等这十几秒可接受。
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=30) as r:
        return json.load(r)


# ─── Session 认证（进程内缓存 25 min）────────────────────────────────────────
_SESSION_CACHE: dict[str, tuple[str, float]] = {}
_SESSION_TTL = 25 * 60


def _keychain_pw(service: str, account: str) -> str:
    # timeout：keychain 若弹授权对话框，capture_output 会把提示吞掉、进程无限挂死（终端看着像卡死）
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return r.stdout.strip()


def get_sid(base_url: str, username: str, keychain_service: str, keychain_account: str) -> str:
    key = f"{base_url}:{username}"
    sid, exp = _SESSION_CACHE.get(key, ("", 0.0))
    if sid and time.time() < exp:
        return sid
    password = _keychain_pw(keychain_service, keychain_account)
    params = urllib.parse.urlencode({
        "api": "SYNO.API.Auth", "version": "3", "method": "login",
        "account": username, "passwd": password,
        "session": "noc", "format": "sid",
    })
    data = _get(f"{base_url}/webapi/auth.cgi?{params}")
    if not data.get("success"):
        raise ValueError(f"Synology 登录失败: {data}")
    sid = data["data"]["sid"]
    _SESSION_CACHE[key] = (sid, time.time() + _SESSION_TTL)
    return sid


def _api(base_url: str, sid: str, api: str, version: str = "1", method: str = "get", **kw) -> dict:
    params = urllib.parse.urlencode({"api": api, "version": version, "method": method, "_sid": sid, **kw})
    data = _get(f"{base_url}/webapi/entry.cgi?{params}")
    if not data.get("success"):
        raise ValueError(f"{api} 返回错误: {data.get('error')}")
    return data.get("data", {})


# ─── 工具函数 ─────────────────────────────────────────────────────────────────
def _fmt_bytes(b) -> str:
    b = int(b or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}PB"


# ─── Provider ─────────────────────────────────────────────────────────────────
class SynologyProvider(Provider):
    cloud   = "synology"
    display = "Synology NAS"
    flag    = "🗄️"
    order   = 12

    def _accounts(self, network: str | None = None) -> list:
        return deploy_config.cloud_accounts("synology", network=network)

    def list_instances(self, network: str | None = None) -> list:
        result = []
        self._unreachable = []   # 本轮连不上的账号（_acct 值），供 refresh_net 账号级沿用
        for acc in self._accounts(network):
            base_url = acc["url"].rstrip("/")
            username = acc["username"]
            ks_svc   = acc.get("keychain_service", "noc-synology")
            host     = base_url.split("//")[-1].split(":")[0]
            ks_acct  = acc.get("keychain_account", f"{username}@{host}")
            label    = acc.get("account", host)

            # 认证前先预检：离线的 Tailscale 节点 / 不通的内网口，直接判死，
            # 不去吃 _get 那个 30s 超时（对端离线时包被黑洞掉，必然耗满）
            alive, why = netprobe.endpoint_alive(base_url, default_port=5001)
            if not alive:
                print(f"  ⚠️ Synology {label}: {why}，跳过", file=sys.stderr)
                self._unreachable.append(label)
                continue

            try:
                sid = get_sid(base_url, username, ks_svc, ks_acct)
            except Exception as e:
                print(f"  ⚠️ Synology {base_url}: 认证失败 ({e})", file=sys.stderr)
                self._unreachable.append(label)
                continue

            try:
                sys_info = _api(base_url, sid, "SYNO.Core.System", version="1", method="info")
            except Exception as e:
                print(f"  ⚠️ Synology {base_url} 系统信息: {e}", file=sys.stderr)
                sys_info = {}

            model     = sys_info.get("model", label)
            dsm_ver   = sys_info.get("firmware_ver", "")
            ram_mb    = int(sys_info.get("ram_size", 0))
            ram_str   = f"{ram_mb // 1024}G" if ram_mb >= 1024 else f"{ram_mb}M"
            cpu_cores = sys_info.get("cpu_cores", "?")
            cpu_series= sys_info.get("cpu_series", "")
            temp      = sys_info.get("sys_temp")
            temp_str  = f"{temp}°C" if temp is not None else ""

            # 卷容量
            vol_desc = ""
            try:
                stor = _api(base_url, sid, "SYNO.Storage.CGI.Storage", version="1", method="load_info")
                vols = stor.get("volumes", [])
                if vols:
                    total = sum(int(v.get("size", {}).get("total", 0)) for v in vols)
                    used  = sum(int(v.get("size", {}).get("used",  0)) for v in vols)
                    vol_desc = f"{_fmt_bytes(used)}/{_fmt_bytes(total)}"
                disk_count = len(stor.get("disks", []))
            except Exception:
                disk_count = 0

            desc_parts = []
            if vol_desc:
                desc_parts.append(vol_desc)
            if temp_str:
                desc_parts.append(temp_str)
            if cpu_series:
                desc_parts.append(cpu_series)

            net = acc["network"]
            result.append({
                "name":           model,
                "id":             f"syno-{host}",
                "cloud":          "synology",
                "profile":        "Synology NAS",
                "_acct":          label,
                "network":        net,
                "region":         NET_LABEL[net],
                "region_display": NET_LABEL[net],
                "flag":           self.flag,
                "type":           f"{cpu_cores}C/{ram_str}",
                "state":          "running",
                "az":             None,
                "private_ip":     host,
                "public_ip":      None,
                "ssh_alias":      f"local-{label}",
                "ssh_user":       username,
                "key_file":       None,
                "connect":        base_url,
                "proxyjump":      None,
                "verified":       False,
                "desc":           "  ".join(desc_parts),
                "os":             dsm_ver,
                "disk":           f"{disk_count}盘",
            })
        return result
