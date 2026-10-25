"""深信服 SCP（Super Cloud Platform）HCI Provider。

认证：Token 方式（用户名 + RSA-PKCS1v1.5 加密密码）
  1. GET  /janus/public-key           → RSA 模数（指数固定 0x10001）
  2. POST /janus/authenticate         → token（24h 有效，本进程内缓存）
  3. 请求头：Authorization: Token <token_id>
     旧版 SCP < 6.3.70 需加 Cookie: aCMPAuthToken=<uuid>

展示（只读）：list_instances() 查询虚拟机列表，支持分页。
生命周期：status 查询单台状态；reboot 依赖服务端支持对应操作端点。
不支持该重启端点的版本返回明确错误；用户可通过控制台执行重启。

多账号配置（ops-config.toml）：
  [[sangfor.accounts]]
  account          = "sangfor"          # 看板展示名
  url              = "https://SCP_IP"   # SCP 管理地址，SSL verify=False 自动处理
  username         = "admin"
  keychain_service = "noc-sangfor"

密码存 macOS Keychain：
  security add-generic-password -U -s noc-sangfor -a admin -T /usr/bin/security -w

依赖（二选一）：pip install pycryptodome  或  pip install cryptography
"""
from __future__ import annotations

import binascii
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import deploy_config, netprobe, paths
from ..netprobe import NET_LABEL
from .base import Provider


# ─── local-servers.toml SSH 补充（Sangfor HCI）───────────────────────────────
def _toml_supplement() -> dict:
    """读 local-servers.toml category="Sangfor HCI" 条目，按 sangfor_id **和 ip** 双键建索引。

    路径走 paths.workspace_dir()——全局 `ops` 是 uv-tool 拷贝，__file__ 相对路径推不到工作区。

    ⚠️ 只按 sangfor_id 索引会静默丢 SSH 信息：手抄 VM UUID 很少有人做（22 条里只有 1 条填了），
    没命中的条目 port/user/hostname 全部取不到，`connect` 一律退化成 `mstsc`——
    明明是 CentOS + SSH 的机器在清单里显示成 RDP。IP 才是实际可用的关联键，故按 ip 兜底。
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
        if s.get("category") == "Sangfor HCI":
            sid = s.get("sangfor_id")
            if sid:
                idx[sid] = s
            ip = s.get("ip")
            if ip and ip not in idx:      # id 优先：同 IP 已被显式 sangfor_id 条目占位则不覆盖
                idx[ip] = s
    return idx

# ─── RSA-PKCS1v1.5 加密（pycryptodome 优先，cryptography 备用）────────────────
try:
    from Crypto.PublicKey import RSA as _RSA  # type: ignore[import]
    from Crypto.Cipher import PKCS1_v1_5 as _PKCS1v15  # type: ignore[import]

    def _rsa_encrypt(modulus_hex: str, plaintext: bytes) -> bytes:
        n = int(modulus_hex.strip().rstrip("\\n"), 16)
        return _PKCS1v15.new(_RSA.construct((n, 0x10001))).encrypt(plaintext)

except Exception:   # 不只 ImportError：pycryptodome 装坏时抛的是 cffi 的 Version mismatch，
                    # 那也该退到 cryptography，而不是让整条 SCP 认证跟着挂掉
    try:
        from cryptography.hazmat.backends import default_backend as _be  # type: ignore[import]
        from cryptography.hazmat.primitives.asymmetric import padding as _pad  # type: ignore[import]
        from cryptography.hazmat.primitives.asymmetric.rsa import (  # type: ignore[import]
            RSAPublicNumbers as _RSANum,
        )

        def _rsa_encrypt(modulus_hex: str, plaintext: bytes) -> bytes:  # type: ignore[misc]
            n = int(modulus_hex.strip().rstrip("\\n"), 16)
            return _RSANum(0x10001, n).public_key(_be()).encrypt(plaintext, _pad.PKCS1v15())

    except ImportError:
        def _rsa_encrypt(modulus_hex: str, plaintext: bytes) -> bytes:  # type: ignore[misc]
            raise ImportError(
                "深信服 SCP Token 认证需要 RSA 库：pip install pycryptodome 或 pip install cryptography"
            )


# ─── SSL / HTTP ──────────────────────────────────────────────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", "noc-ops/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=20) as r:
        return json.loads(r.read())


# ─── Token 认证（进程内缓存 23h）─────────────────────────────────────────────
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 23 * 3600


def _keychain_pw(service: str, account: str) -> str:
    # timeout：keychain 若弹授权对话框，capture_output 会把提示吞掉、进程无限挂死（终端看着像卡死）
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return r.stdout.strip()


def get_token(base_url: str, username: str, keychain_service: str) -> str:
    """获取（或从进程内缓存返回）SCP Token。"""
    cache_key = f"{base_url}:{username}"
    token, exp = _TOKEN_CACHE.get(cache_key, ("", 0.0))
    if token and time.time() < exp:
        return token

    password = _keychain_pw(keychain_service, username)

    # 1. 取公钥模数（免密接口）
    pk_resp = _http("GET", f"{base_url}/janus/public-key")
    data_part = pk_resp.get("data", pk_resp)
    modulus_hex = data_part.get("public_key") if isinstance(data_part, dict) else None
    if not modulus_hex:
        modulus_hex = pk_resp.get("public_key", "")
    if not modulus_hex:
        raise ValueError(f"SCP {base_url} 未返回 public_key")

    # 2. RSA-PKCS1v1.5 加密密码 → hex
    encrypted_hex = binascii.b2a_hex(_rsa_encrypt(modulus_hex, password.encode())).decode()

    # 3. 换 Token（OpenStack v2 passwordCredentials 格式）
    auth_resp = _http(
        "POST", f"{base_url}/janus/authenticate",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"auth": {"passwordCredentials": {"username": username, "password": encrypted_hex}}}).encode(),
    )
    token = auth_resp["data"]["access"]["token"]["id"]
    _TOKEN_CACHE[cache_key] = (token, time.time() + _TOKEN_TTL)
    return token


# ─── API 调用封装 ─────────────────────────────────────────────────────────────
def _api(base_url: str, token: str, path: str, *, method: str = "GET", body=None):
    hdrs = {"Authorization": f"Token {token}"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    resp = _http(method, f"{base_url}{path}", headers=hdrs,
                 body=json.dumps(body).encode() if body is not None else None)
    return resp.get("data", resp)


# ─── 供 reboot_argv / status_argv 内联脚本直接调用 ────────────────────────────
_SRC_DIR = str(Path(__file__).parents[2])  # .../src/


def _reboot_server(base_url: str, username: str, keychain_service: str, server_id: str) -> None:
    token = get_token(base_url, username, keychain_service)
    try:
        _api(base_url, token, f"/janus/20180725/servers/{server_id}/action",
             method="POST", body={"action": "reboot"})
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # 不支持该端点时，给出明确错误和可用的操作入口。
        raise SystemExit(
            f"深信服 SCP 不认这个重启端点（404）：{base_url}/janus/20180725/servers/{server_id}/action\n"
            "请在 SCP 控制台执行重启，或使用已配置的 SSH 别名连接后操作。\n"
            "要修好：在控制台点一次重启、用 devtools 抓那条 XHR 的 path+payload 给我。"
        )
    print(f"已发送重启: {server_id}")


def _get_server_status(base_url: str, username: str, keychain_service: str, server_id: str) -> None:
    token = get_token(base_url, username, keychain_service)
    data = _api(base_url, token, f"/janus/20180725/servers/{server_id}")
    status = data.get("status", "unknown") if isinstance(data, dict) else "unknown"
    print(status)


# ─── Provider ─────────────────────────────────────────────────────────────────
_STATE_MAP = {
    "running":  "running",
    "active":   "running",
    "on":       "running",   # power_state 值
    "offline":  "stopped",
    "stopped":  "stopped",
    "shutoff":  "stopped",
    "off":      "stopped",   # power_state 值
    "error":    "error",
    "building": "pending",
    "build":    "pending",
    "pending":  "pending",
}


class SangforProvider(Provider):
    cloud   = "sangfor"
    display = "Sangfor HCI"
    flag    = "🖥️"
    order   = 8

    def _accounts(self, network: str | None = None) -> list:
        return deploy_config.cloud_accounts("sangfor", network=network)

    def _acc_for(self, inst: dict) -> dict | None:
        # profile 已改为展示用的"私有云"，账号反查走专用字段 _acct
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
            ks = acc.get("keychain_service", f"noc-sangfor-{username}")
            account_name = acc.get("account", username)

            # 认证前预检：不通就秒判死，别去吃 _http 的 20s 超时
            alive, why = netprobe.endpoint_alive(base_url)
            if not alive:
                print(f"  ⚠️ 深信服 {account_name}: {why}，跳过", file=sys.stderr)
                self._unreachable.append(account_name)
                continue

            try:
                token = get_token(base_url, username, ks)
            except Exception as e:
                print(f"  ⚠️ 深信服 {base_url}: 认证失败 ({e})", file=sys.stderr)
                self._unreachable.append(account_name)
                continue

            sup_idx = _toml_supplement()
            page_num, page_size = 0, 100
            total: int | None = None
            while True:
                try:
                    data = _api(
                        base_url, token,
                        f"/janus/20180725/servers?page_num={page_num}&page_size={page_size}",
                    )
                except Exception as e:
                    print(f"  ⚠️ 深信服 {base_url} 云主机列表 p{page_num}: {e}", file=sys.stderr)
                    # 列表拉取中断 → 该账号本轮结果不完整，标记未连上，让 refresh_local 沿用旧记录
                    # （账号级沿用按 id 去重，已拿到的分页不会与沿用记录重复）
                    self._unreachable.append(account_name)
                    break

                if isinstance(data, dict):
                    servers = data.get("data", [])
                    if total is None:
                        total = int(data.get("total_size", 0))
                elif isinstance(data, list):
                    servers = data
                    total = len(data)
                else:
                    break

                for s in servers:
                    raw_state = (s.get("status") or s.get("power_state") or "").lower()
                    state = _STATE_MAP.get(raw_state, raw_state or "unknown")

                    # IP 在 networks[*].ip_address（SCP 实际响应格式）
                    private_ip = None
                    for net in (s.get("networks") or []):
                        private_ip = net.get("ip_address") or net.get("addr")
                        if private_ip:
                            break

                    vcpus = s.get("cores") or s.get("vcpus") or s.get("cpu") or 0
                    ram_mb = int(s.get("memory_mb") or s.get("ram") or s.get("memory") or 0)
                    ram_str = f"{ram_mb // 1024}G" if ram_mb >= 1024 else f"{ram_mb}M"

                    # toml 补充（IP 兜底 / SSH 信息）
                    vm_id = s.get("id") or s.get("server_id", "")
                    # 先按 VM UUID 命中，再按 API 给的 IP 兜底（多数 toml 条目没填 sangfor_id）
                    sup = sup_idx.get(vm_id) or sup_idx.get(private_ip) or {}
                    private_ip = private_ip or sup.get("ip")
                    port     = sup.get("port")
                    ssh_user = sup.get("user")
                    hostname = sup.get("hostname")
                    guest_os = sup.get("os") or s.get("image_name", "")
                    is_win   = "windows" in guest_os.lower() or sup.get("check_port") == 3389
                    if sup.get("connect"):
                        connect = sup["connect"]
                    elif is_win or not port:
                        connect = f"mstsc /v:{private_ip}" if private_ip else None
                    else:
                        port_part = f":{port}" if port != 22 else ""
                        connect = f"ssh {ssh_user}@{private_ip}{port_part}" if ssh_user and private_ip else None
                    ssh_alias = f"local-{hostname}" if hostname and not is_win and port else None

                    result.append({
                        "name":           s.get("name", ""),
                        "id":             vm_id,
                        "cloud":          "sangfor",
                        "profile":        "Sangfor HCI",
                        "_acct":          account_name,
                        "network":        acc["network"],
                        "region":         NET_LABEL[acc["network"]],
                        "region_display": NET_LABEL[acc["network"]],
                        "flag":           "🖥️",
                        "type":           f"{vcpus}C/{ram_str}",
                        "state":          state,
                        "az":             s.get("az_id"),
                        "private_ip":     private_ip,
                        "public_ip":      None,
                        "ssh_alias":      ssh_alias,
                        "ssh_user":       ssh_user,
                        # 端口必须单独出字段：SSH 配置渲染与看板 SSH 按钮都读 ssh_port，
                        # 只把端口塞进 connect 字符串 → 别名渲染不出 Port、默认走 22 连不上。
                        "ssh_port":       port,
                        "key_file":       sup.get("key_file"),
                        "connect":        connect,
                        "proxyjump":      None,
                        "verified":       False,
                        "desc":           s.get("description", ""),
                        "os":             guest_os,
                    })

                fetched = page_num * page_size + len(servers)
                if not servers or (total is not None and fetched >= total) or isinstance(data, list):
                    break
                page_num += 1

        return result

    def _inline(self, func: str, *args: str) -> list:
        args_repr = ", ".join(repr(a) for a in args)
        script = (
            f"import sys; sys.path.insert(0, {repr(_SRC_DIR)})\n"
            f"from noc_ops.providers.sangfor import {func}\n"
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
            raise ValueError(f"深信服: 找不到 profile={inst.get('profile')!r} 的账号")
        ks = acc.get("keychain_service", f"noc-sangfor-{acc['username']}")
        return self._inline("_reboot_server", acc["url"].rstrip("/"), acc["username"], ks, inst["id"])

    def status_argv(self, inst: dict) -> list:
        acc = self._acc_for(inst)
        if not acc:
            raise ValueError(f"深信服: 找不到 profile={inst.get('profile')!r} 的账号")
        ks = acc.get("keychain_service", f"noc-sangfor-{acc['username']}")
        return self._inline("_get_server_status", acc["url"].rstrip("/"), acc["username"], ks, inst["id"])
