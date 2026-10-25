"""VMware vCenter Provider。

认证：vSphere REST API Session（Basic auth → session token）
  POST /api/session  →  "token_string"（30 min idle 过期，进程内缓存 25 min）
  请求头：vmware-api-session-id: <token>

VM 列表：GET /api/vcenter/vm → [{vm, name, power_state, cpu_count, memory_size_MiB}]
VM IP：  GET /api/vcenter/vm/{id}/guest/identity → ip_address（需 VMware Tools）
VM 重启：POST /api/vcenter/vm/{id}/power?action=reset

SSH 补充：若工作区 local-servers.toml 有 category="VMware ESX" 的同名条目，
         取其 port / user / ssh_alias / connect / os 等字段。

多账号配置（ops-config.toml）：
  [[vmware.accounts]]
  account          = "vmware"
  url              = "https://192.0.2.20"
  username         = "administrator@vsphere.local"
  keychain_service = "noc-vmware"

密码存 macOS Keychain：
  security add-generic-password -U -s noc-vmware -a administrator@vsphere.local -T /usr/bin/security -w
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import deploy_config, netprobe, paths
from ..netprobe import NET_LABEL
from .base import Provider

# ─── SSL / HTTP ──────────────────────────────────────────────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", "noc-ops/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as r:
        return json.loads(r.read())


# ─── Session 认证（进程内缓存 25 min）────────────────────────────────────────
_SESSION_CACHE: dict[str, tuple[str, float]] = {}
_SESSION_TTL = 25 * 60  # vCenter session 默认 30 min idle 过期


def _keychain_pw(service: str, account: str) -> str:
    # timeout：keychain 若弹授权对话框，capture_output 会把提示吞掉、进程无限挂死（终端看着像卡死）
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return r.stdout.strip()


def get_session(base_url: str, username: str, keychain_service: str) -> str:
    """获取（或从进程内缓存返回）vCenter session token。"""
    key = f"{base_url}:{username}"
    token, exp = _SESSION_CACHE.get(key, ("", 0.0))
    if token and time.time() < exp:
        return token
    password = _keychain_pw(keychain_service, username)
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    token = _http("POST", f"{base_url}/api/session",
                  headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"},
                  body=b"")
    _SESSION_CACHE[key] = (token, time.time() + _SESSION_TTL)
    return token


# ─── API 调用封装 ─────────────────────────────────────────────────────────────
def _api(base_url: str, token: str, path: str, *, method: str = "GET", body=None):
    hdrs = {"vmware-api-session-id": token}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    return _http(method, f"{base_url}{path}", headers=hdrs,
                 body=json.dumps(body).encode() if body is not None else None)


# ─── local-servers.toml SSH 补充索引 ─────────────────────────────────────────
def _ssh_index() -> dict:
    """按 name/hostname 建索引，供 VMware VM 补充 SSH 连接信息。

    路径走 paths.workspace_dir()——全局 `ops` 是 uv-tool 拷贝，__file__ 相对路径推不到工作区。
    """
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    toml_path = paths.local_servers_path()
    if not toml_path.exists():
        return {}
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    idx: dict = {}
    for s in data.get("servers", []):
        if s.get("category") == "VMware ESX":
            idx[s["name"]] = s
            if s.get("hostname"):
                idx[s["hostname"]] = s
    return idx


# ─── 供 reboot_argv / status_argv 内联脚本调用 ───────────────────────────────
_SRC_DIR = str(Path(__file__).parents[2])


def _reboot_vm(base_url: str, username: str, keychain_service: str, vm_id: str) -> None:
    token = get_session(base_url, username, keychain_service)
    _api(base_url, token, f"/api/vcenter/vm/{vm_id}/power?action=reset", method="POST")
    print(f"已发送重启: {vm_id}")


def _get_vm_status(base_url: str, username: str, keychain_service: str, vm_id: str) -> None:
    token = get_session(base_url, username, keychain_service)
    detail = _api(base_url, token, f"/api/vcenter/vm/{vm_id}")
    ps = detail.get("power_state", "UNKNOWN") if isinstance(detail, dict) else "UNKNOWN"
    print({"POWERED_ON": "running", "POWERED_OFF": "stopped", "SUSPENDED": "stopped"}.get(ps, ps.lower()))


# ─── Provider ─────────────────────────────────────────────────────────────────
_STATE_MAP = {
    "POWERED_ON":  "running",
    "POWERED_OFF": "stopped",
    "SUSPENDED":   "stopped",
}


class VMwareProvider(Provider):
    cloud   = "vmware"
    display = "VMware ESX"
    flag    = "🖥️"
    order   = 9

    def _accounts(self, network: str | None = None) -> list:
        return deploy_config.cloud_accounts("vmware", network=network)

    def _acc_for(self, inst: dict) -> dict | None:
        want = inst.get("_acct", inst.get("profile", ""))
        for a in self._accounts():
            if a.get("account", a.get("username", "")) == want:
                return a
        return None

    def list_instances(self, network: str | None = None) -> list:
        result = []
        self._unreachable = []   # 本轮连不上的账号（_acct 值），供 refresh_net 账号级沿用
        for acc in self._accounts(network):
            base_url = acc["url"].rstrip("/")
            username = acc["username"]
            ks = acc.get("keychain_service", f"noc-vmware-{username}")
            account_name = acc.get("account", username)

            # 认证前预检：不通就秒判死，别去吃 _api 的 15s 超时
            alive, why = netprobe.endpoint_alive(base_url)
            if not alive:
                print(f"  ⚠️ VMware {account_name}: {why}，跳过", file=sys.stderr)
                self._unreachable.append(account_name)
                continue

            try:
                token = get_session(base_url, username, ks)
            except Exception as e:
                print(f"  ⚠️ VMware {base_url}: 认证失败 ({e})", file=sys.stderr)
                self._unreachable.append(account_name)
                continue

            try:
                vms = _api(base_url, token, "/api/vcenter/vm")
            except Exception as e:
                print(f"  ⚠️ VMware {base_url} VM 列表: {e}", file=sys.stderr)
                self._unreachable.append(account_name)
                continue

            ssh_idx = _ssh_index()

            # 并发取每台 VM 的 guest identity（需 VMware Tools 已运行）
            def fetch_ip(vm_id: str) -> str | None:
                try:
                    gi = _api(base_url, token, f"/api/vcenter/vm/{vm_id}/guest/identity")
                    return gi.get("ip_address") if isinstance(gi, dict) else None
                except Exception:
                    return None

            vms = [v for v in vms if not v.get("name", "").startswith("vCLS")]
            vm_ids = [v["vm"] for v in vms]
            with ThreadPoolExecutor(max_workers=min(16, len(vm_ids) or 1)) as ex:
                ip_map = dict(zip(vm_ids, ex.map(fetch_ip, vm_ids)))

            for v in vms:
                vm_id  = v["vm"]
                name   = v.get("name", "")
                state  = _STATE_MAP.get(v.get("power_state", ""), "unknown")
                vcpus  = v.get("cpu_count", 0)
                ram_mb = int(v.get("memory_size_MiB", 0))
                ram_str = f"{ram_mb // 1024}G" if ram_mb >= 1024 else f"{ram_mb}M"
                api_ip = ip_map.get(vm_id)

                # SSH 补充（从 local-servers.toml category="VMware ESX"）
                sup = ssh_idx.get(name) or {}
                port     = sup.get("port")
                ssh_user = sup.get("user")
                ip       = api_ip or sup.get("ip")
                guest_os = sup.get("os") or v.get("guest_OS", "")
                is_win   = "windows" in guest_os.lower() or sup.get("check_port") == 3389

                if sup.get("connect"):
                    connect = sup["connect"]
                elif is_win or not port:
                    rdp_ip  = f"{ip}:3389" if sup.get("check_port") == 3389 else ip
                    connect = f"mstsc /v:{rdp_ip}" if ip else None
                else:
                    port_part = f":{port}" if port != 22 else ""
                    connect = f"ssh {ssh_user}@{ip}{port_part}" if ssh_user and ip else None

                hostname = sup.get("hostname")
                ssh_alias = f"local-{hostname}" if hostname and not is_win and port else None

                result.append({
                    "name":           name,
                    "id":             f"vm-{name}" if name else vm_id,
                    "_vcenter_id":    vm_id,
                    "cloud":          "vmware",
                    "profile":        "VMware ESX",
                    "_acct":          account_name,
                    "network":        acc["network"],
                    "region":         NET_LABEL[acc["network"]],
                    "region_display": NET_LABEL[acc["network"]],
                    "flag":           "🖥️",
                    "type":           f"{vcpus}C/{ram_str}",
                    "state":          state,
                    "az":             None,
                    "private_ip":     ip,
                    "public_ip":      None,
                    "ssh_port":       port,
                    "ssh_alias":      ssh_alias,
                    "ssh_user":       ssh_user,
                    "key_file":       sup.get("key_file"),
                    "connect":        connect,
                    "proxyjump":      None,
                    "verified":       False,
                    "desc":           sup.get("desc") or sup.get("name", ""),
                    "os":             guest_os,
                    "disk":           sup.get("disk", ""),
                })
        return result

    def _inline(self, func: str, *args: str) -> list:
        args_repr = ", ".join(repr(a) for a in args)
        script = (
            f"import sys; sys.path.insert(0, {repr(_SRC_DIR)})\n"
            f"from noc_ops.providers.vmware import {func}\n"
            f"{func}({args_repr})"
        )
        # ⚠️ 用 sys.executable 而不是裸 "python3"：本包装在 uv-tool venv 里，脚本又要
        # sys.path.insert 到该 venv 的 site-packages。PATH 上的 python3 常是另一个版本
        # （如系统 3.14 vs venv 3.12），混着跑 C 扩展直接炸 cffi/pycryptodome ABI 不匹配，
        # 报错还指向 cffi 版本，完全看不出是解释器串了。同一解释器才对得上。
        return [sys.executable, "-c", script]

    def reboot_argv(self, inst: dict) -> list:
        acc = self._acc_for(inst)
        if not acc:
            raise ValueError(f"VMware: 找不到 profile={inst.get('profile')!r} 的账号")
        ks = acc.get("keychain_service", f"noc-vmware-{acc['username']}")
        return self._inline("_reboot_vm", acc["url"].rstrip("/"), acc["username"], ks, inst.get("_vcenter_id", inst["id"]))

    def status_argv(self, inst: dict) -> list:
        acc = self._acc_for(inst)
        if not acc:
            raise ValueError(f"VMware: 找不到 profile={inst.get('profile')!r} 的账号")
        ks = acc.get("keychain_service", f"noc-vmware-{acc['username']}")
        return self._inline("_get_vm_status", acc["url"].rstrip("/"), acc["username"], ks, inst.get("_vcenter_id", inst["id"]))
