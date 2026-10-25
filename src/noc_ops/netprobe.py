"""网络域判定与端点存活预检（LAN vs Tailscale）。

内网端点与覆盖网端点分别检查连接条件，避免一个网络域不可达时影响另一个域。
- `network_of(host_or_url)` 判定一个端点属于哪个网络域（"tailscale" / "lan"）；
- `endpoint_alive(url)` 是各 provider 认证前的统一预检，**按域用不同手段**——
  Tailscale 端点直接问 `tailscale status`（对端离线是已知事实，0 网络等待），
  LAN 端点走 2.5s TCP 连接。两者都把"连不上"从数十秒超时压到秒级。
"""
import ipaddress
import json
import os
import shutil
import socket
import subprocess
from urllib.parse import urlsplit

# Tailscale 给节点分配的 CGNAT 段（100.64.0.0/10）——见 tailscale 官方 IP 分配说明
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_MAGICDNS_SUFFIX = ".ts.net"

NET_LAN = "lan"
NET_TAILSCALE = "tailscale"

# 清单/看板里的分区标题（两个网络域各占一格，不再混成一个"本地网络"）
NET_LABEL = {NET_LAN: "本地网络", NET_TAILSCALE: "Tailscale"}

_TCP_TIMEOUT = 2.5   # 秒；LAN/隧道内握手都在这之内，超了就是真不通


def host_of(url_or_host: str) -> str:
    """从 url（https://host:port/…）或裸 host[:port] 里取出主机名。"""
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "//" in s:
        s = urlsplit(s).hostname or ""
    else:
        s = s.rsplit(":", 1)[0] if s.count(":") == 1 else s
    return s.strip("[]")


def hostport_of(url_or_host: str, default_port: int = 443) -> tuple:
    """从 url 或 host[:port] 取 (host, port)；url 无端口时按 scheme 推 443/80。"""
    s = (url_or_host or "").strip()
    if "//" in s:
        parts = urlsplit(s)
        return (parts.hostname or "", parts.port or (80 if parts.scheme == "http" else 443))
    host, _, port = s.rpartition(":")
    if host and port.isdigit():
        return host.strip("[]"), int(port)
    return s.strip("[]"), default_port


def network_of(url_or_host: str) -> str:
    """端点所属网络域：Tailscale IP（100.64/10）或 MagicDNS 名 → "tailscale"，其余 → "lan"。

    显式配置（账号里的 `network = "..."`）优先，此函数只做**自动推导**兜底，
    这样新增一台 Tailscale 机器只要填它的 100.x 地址就自动归位，不会忘配。
    """
    host = host_of(url_or_host)
    if not host:
        return NET_LAN
    if host.lower().rstrip(".").endswith(_TAILSCALE_MAGICDNS_SUFFIX):
        return NET_TAILSCALE
    try:
        if ipaddress.ip_address(host) in _TAILSCALE_NET:
            return NET_TAILSCALE
    except ValueError:
        pass
    return NET_LAN


# ─── Tailscale 状态（每进程只问一次 tailscaled）──────────────────────────────
_TS_STATUS: dict | None = None
_TS_QUERIED = False


def _tailscale_bin() -> str | None:
    for cand in (shutil.which("tailscale"),
                 "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                 "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"):
        if cand and os.path.exists(cand):
            return cand
    return None


def tailscale_status() -> dict:
    """`tailscale status --json` 的结果（进程内缓存）；取不到返回 {}。"""
    global _TS_STATUS, _TS_QUERIED
    if _TS_QUERIED:
        return _TS_STATUS or {}
    _TS_QUERIED = True
    binary = _tailscale_bin()
    if not binary:
        _TS_STATUS = {}
        return {}
    try:
        r = subprocess.run([binary, "status", "--json"],
                           capture_output=True, text=True, timeout=10)
        _TS_STATUS = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        _TS_STATUS = {}
    return _TS_STATUS or {}


def tailnet_up() -> tuple:
    """本机 Tailscale 是否已连上 tailnet → (ok, 说明)。"""
    if not _tailscale_bin():
        return False, "本机未安装 tailscale CLI"
    st = tailscale_status()
    if not st:
        return False, "tailscaled 未运行或 status 取不到"
    state = st.get("BackendState", "")
    if state != "Running":
        return False, f"Tailscale 未连接（BackendState={state or '未知'}）"
    return True, ""


def _peer_by_host(host: str) -> dict | None:
    """按 Tailscale IP 或 MagicDNS 名在 status 的 Self/Peer 里找节点。"""
    st = tailscale_status()
    if not st:
        return None
    want = host.lower().rstrip(".")
    nodes = list((st.get("Peer") or {}).values())
    if st.get("Self"):
        nodes.append(st["Self"])
    for n in nodes:
        if want in [ip.lower() for ip in (n.get("TailscaleIPs") or [])]:
            return n
        dns = (n.get("DNSName") or "").lower().rstrip(".")
        if dns and (want == dns or want == dns.split(".")[0]):
            return n
    return None


def tailscale_peer_alive(host: str) -> tuple:
    """Tailscale 节点是否在线 → (ok, 说明)。查不到该节点时保守判定为"在线"（交给 TCP 兜底）。"""
    ok, why = tailnet_up()
    if not ok:
        return False, why
    peer = _peer_by_host(host)
    if peer is None:
        return True, ""            # status 里没有（可能是子网路由后的地址）→ 不拦，让 TCP 预检去判
    if peer.get("Online"):
        return True, ""
    last = peer.get("LastSeen") or ""
    name = (peer.get("DNSName") or host).rstrip(".").split(".")[0]
    return False, f"Tailscale 节点 {name} 离线" + (f"（last seen {last[:16]}）" if last else "")


# ─── 统一端点预检 ─────────────────────────────────────────────────────────────
def tcp_alive(host: str, port: int, timeout: float = _TCP_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def endpoint_alive(url_or_host: str, default_port: int = 443, timeout: float = _TCP_TIMEOUT) -> tuple:
    """provider 认证前的统一预检 → (ok, 原因)。

    Tailscale 端点：先问 `tailscale status`——对端离线是**已知事实**，0 网络等待就能判定，
    不必去吃 HTTP 那个 30s 超时（离线节点的包直接被隧道黑洞掉，必然耗满）。
    LAN 端点（以及 status 里查不到的 Tailscale 地址）：2.5s TCP 连接。
    """
    host, port = hostport_of(url_or_host, default_port)
    if not host:
        return False, "端点地址为空"
    if network_of(host) == NET_TAILSCALE:
        ok, why = tailscale_peer_alive(host)
        if not ok:
            return False, why
    if tcp_alive(host, port, timeout):
        return True, ""
    return False, f"{host}:{port} {timeout:g}s 内连不上"
