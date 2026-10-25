"""本地网络 Linux 服务器 Provider（ESXi / HCI 等物理宿主机）。

数据源：工作区根目录 local-servers.toml（gitignored，含密码字段可选）。
VIP 条目（vip=true 或 cpu=0）跳过。
list_instances() 并发 TCP 探测 SSH 端口（timeout=2s），state 写 running/stopped。

路径必须走 paths.workspace_dir()：全局 `ops` 是 uv-tool 的独立拷贝（装在 site-packages
下），拿 __file__ 往上数目录推不到工作区，会静默扫出 0 台。
"""
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

from .. import netprobe
from .. import paths
from .base import Provider

_TCP_TIMEOUT = 2   # 秒，内网延迟极低，2s 足够


def _tcp_ok(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=_TCP_TIMEOUT):
            return True
    except OSError:
        return False


class LocalProvider(Provider):
    cloud   = "local"
    display = "本地"
    flag    = "🏠"
    order   = 10

    def list_instances(self, network: str | None = None) -> list:
        toml_path = paths.local_servers_path()
        if not toml_path.exists():
            print(f"  ⚠️ 本地: 找不到 {toml_path}，本地物理机（ESXi/HCI 宿主）本轮扫不到",
                  file=sys.stderr)
            return []
        if tomllib is None:
            print("  ⚠️ 本地: 无 tomllib/tomli，跳过 local-servers.toml", file=sys.stderr)
            return []

        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        # HCI 和 VMware ESX 由专属 Provider 通过 API 纳管，这里只保留纯本地物理机
        _API_MANAGED = {"Sangfor HCI", "VMware ESX"}
        servers = [s for s in data.get("servers", [])
                   if not s.get("vip") and s.get("cpu")
                   and s.get("category") not in _API_MANAGED]
        # 网络域按 ip 自动推导（100.64/10 → tailscale），显式 network 字段优先；
        # 传了 network= 就只留该域的机器——LAN 与 Tailscale 分开刷，互不误伤。
        for s in servers:
            s["_net"] = s.get("network") or netprobe.network_of(s.get("ip", ""))
        if network:
            servers = [s for s in servers if s["_net"] == network]

        # 并发 TCP 探测：check_port 优先，否则用 port（SSH）
        reachable: dict[str, bool] = {}
        if not servers:
            return []
        with ThreadPoolExecutor(max_workers=min(32, len(servers))) as ex:
            futs = {ex.submit(_tcp_ok, s["ip"],
                              s.get("check_port") or s.get("port", 22)): s["ip"]
                    for s in servers}
            for fut in as_completed(futs):
                reachable[futs[fut]] = fut.result()

        result = []
        for s in servers:
            ip      = s["ip"]
            port    = s.get("port")
            user    = s.get("user", "root")
            os_name = s.get("os", "")
            is_win  = "windows" in os_name.lower() or "Windows" in os_name

            if s.get("connect"):
                connect  = s["connect"]
                ssh_user = s.get("user") or None  # 有 user 字段就填上，否则留空
            elif is_win or not port:
                rdp_ip   = f"{ip}:3389" if s.get("check_port") == 3389 else ip
                connect  = f"mstsc /v:{rdp_ip}"
                ssh_user = s.get("user") or None
            else:
                port_part = f":{port}" if port != 22 else ""
                connect   = f"ssh {user}@{ip}{port_part}"
                ssh_user  = user

            raw_hostname = s.get("hostname")
            hostname = raw_hostname or f"local-{ip}"
            # ssh_alias/ssh_port：只看有无 SSH port + 非 Windows，与 connect 字段无关
            # （机器可同时有 web connect URL 和 SSH port，如 ESXi 主机）
            is_ssh = not is_win and bool(port)
            ssh_port  = port if is_ssh else None
            ssh_alias = f"local-{raw_hostname or ip}" if is_ssh else None
            result.append({
                "name":           s["name"],
                "id":             hostname,
                "cloud":          "local",
                "profile":        "本地",
                "network":        s["_net"],
                "region":         s.get("category", ""),
                "region_display": s.get("category", ""),
                "flag":           "🏠",
                "type":           f"{s.get('cpu',0)}C/{s.get('mem','')}",
                "state":          "running" if reachable.get(ip) else "stopped",
                "az":             None,
                "private_ip":     ip,
                "public_ip":      None,
                "ssh_port":       ssh_port,
                "ssh_alias":      ssh_alias,
                "ssh_user":       ssh_user,
                "key_file":       None,
                "connect":        connect,
                "proxyjump":      None,
                "verified":       False,
                "desc":           s.get("desc", ""),
                "os":             os_name,
                "disk":           s.get("disk", ""),
            })
        return result
