"""本机出口链路探测（`ops netprobe`）——把"我这台机器的流量从哪出去"变成可重跑的快照。

**为什么要有它**：本机网络是全工程唯一还靠人肉记在文档里的部分，也因此腐化最快。
实证：runbook 里写死的 `utun7`、某个上游 IP、"某台中转机跑 xray" 三条，写下时都对，
两个月后全错（utun 编号被 macOS 重新分配、EIP 已释放、代理软件换成了 v2ray）。

**分层原则**（决定什么该写进代码、什么该留在文档）：
- **方法**（跨机器通用、几乎不变）→ skill / 本模块代码：按网段与 MTU 特征认接口，
  **绝不按 utun 编号认**；按目标选探测源。
- **事实**（跟机器/位置/时间变）→ 本模块产出的快照，靠重跑消除腐化，不进文档正文。
- **语义**（探测不出来、要人写）→ `ops-config.toml` 的 `[known_egress]` / `[instance_notes]`。

**为什么出口不是一个 IP 而是一条链**：本机每种隧道模型的 upstream 自己还会再分流。
已实证的一条：本机 →(WireGuard)→ 中转机 → 国内目标就地出局 / 海外目标再上游到海外节点。
所以"走了哪条隧道"推不出"出口 IP 是什么"，**必须按目标分别探**——这就是下面同时打
三个不同归属的探测源、而不是只查一次 ifconfig.me 的原因。

纯只读：只跑 ifconfig/route 与 HTTP GET，不改任何网络设置。
"""
from __future__ import annotations

import concurrent.futures as _fut
import ipaddress
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from . import deploy_config, paths

_HTTP_TIMEOUT = 8
_UA = "noc-ops/1.0"          # 具名 UA：部分站点（CF 后面的）会拿默认 urllib UA 当爬虫封

# ---- 隧道特征：按**网段**认，不按接口编号认（编号是 macOS 抢最低空闲号分配的，重连即变）----
# ⚠️ `100.64.0.0/10` 是**整个 CGNAT 段**，不是 Tailscale 专属：2026-08-02 实测某 OpenVPN
# 隧道拿到 `100.88.0.3`，若按段认就会被误判成 Tailscale——而且恰好把一条**正在承载出口**
# 的链路判成"不参与出口分流"，是最坏的一类错。所以 Tailscale 一律**问它自己**要地址
# （`tailscale ip -4`），CGNAT 段只当兜底提示。
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_FAKEIP_NET = ipaddress.ip_network("198.18.0.0/15")        # Clash/mihomo TUN 的 fake-ip 惯用段

# 探测源：按"目标在哪"选，三个源打到的是**同一条链上的三个出口点**，不是三条并行线路
# 每个源给**多个候选 URL，按序降级**：2026-08-02 实测 Clash TUN(fake-ip) 下 cip.cc 的
# HTTPS 握手稳定超时、HTTP 却正常——只配一个 https 会让探测器在这类环境里直接瞎掉。
_PROBES = [
    # cip.cc 的 **HTTPS 在 Clash TUN 下稳定握手超时**（curl 与 urllib 一致复现，http 却 1.7s
    # 就回），先试 https 只会白烧一个超时。它是明文查询服务、不传任何凭据，**http 优先**没有
    # 安全损失；https 留作兜底。
    ("cip.cc", ["http://cip.cc/", "https://cip.cc/"], "国内目标（多数不走代理规则）"),
    ("myip.ipip.net", ["https://myip.ipip.net/", "http://myip.ipip.net/"], "国内目标（命中代理规则）"),
    ("ip-api.com", ["http://ip-api.com/json"], "海外目标"),
]


def _run(cmd: list[str], timeout: int = 6) -> str:
    """跑一条本地只读命令，失败一律返回空串——探测器不该因为某条命令缺失就整个炸掉。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 一、接口全景
# --------------------------------------------------------------------------
def _interfaces() -> list[dict]:
    """解析 ifconfig，列出所有**有 IPv4** 的接口及其 MTU。

    仅统计具有 IPv4 地址的接口，排除只有 IPv6 link-local 地址的接口。
    """
    out = _run(["ifconfig"])
    ifaces, cur, mtu = [], None, None
    for line in out.splitlines():
        m = re.match(r"^([a-z0-9]+):\s.*\bmtu\s+(\d+)", line)
        if m:
            cur, mtu = m.group(1), int(m.group(2))
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and cur:
            ifaces.append({"name": cur, "ipv4": m.group(1), "mtu": mtu})
    return ifaces


def _tailscale_ips() -> set:
    """向 Tailscale 本体要它持有的 IP——**不要按 CGNAT 网段猜**（见上方注释里的误判事故）。

    tailscale 不在/没跑就返回空集，此时 CGNAT 段的接口会落到"隧道"分支按软件判定。
    """
    out = _run(["tailscale", "ip", "-4"]) or \
        _run(["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"])
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _running(pattern: str) -> bool:
    """进程是否在跑。**必须 -i 忽略大小写**：macOS 上 WireGuard.app 的进程名是 `WireGuard`
    （大写 G），用小写模式匹配会静默漏判、把隧道错标成"未识别"。"""
    return bool(_run(["pgrep", "-fi", pattern]).strip())


# 隧道软件 → (进程匹配模式, 人类可读名, 典型 MTU)。MTU 是**次要佐证**：多个隧道软件
# 同时在跑时（本机 WireGuard 与深信服 aTrust 就并存），只看"谁在跑"会张冠李戴。
_TUNNEL_SOFTWARE = [
    ("wireguard", r"WireGuard", "WireGuard", 1420),
    ("openvpn", r"openvpn|Tunnelblick", "OpenVPN", None),
    ("atrust", r"aTrust", "深信服 aTrust SDP", None),
]


def _classify(iface: dict, procs: dict, ts_ips: set) -> tuple[str, str]:
    """按特征把接口归到出局模型，返回 (role, 人类可读标签)。

    判据优先级：**权威自报**（Tailscale 自己说持有哪些 IP）> 专用网段 > 物理口命名 >
    私网+隧道口。**任何时候都不看编号**。
    """
    name, ip = iface["name"], iface["ipv4"]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown", "未知"
    if ip in ts_ips:                       # 权威：tailscale ip -4 自报，比认网段可靠
        return "tailscale", "Tailscale 组网（内网互访，不参与出口分流）"
    if addr in _FAKEIP_NET:
        return "clash", "Clash/mihomo TUN（fake-ip 段）"
    if addr.is_loopback:
        return "loopback", "本机回环"
    if name.startswith(("en", "eth")):
        return "lan", "物理局域网口"
    if name.startswith("bridge"):
        return "bridge", "虚拟机桥接（Parallels/Docker 等，与出口无关）"
    if name.startswith(("utun", "tun", "ppp")):
        # 隧道口但不在专用段：macOS 不直接告诉你哪个 utun 属于谁（要 `sudo wg show` 才确凿），
        # 所以用「在跑的隧道软件」+「典型 MTU」两条线索定，**定不下来就如实说定不下来**，
        # 不猜——猜错会把一条链路的结论安到另一条上。
        cg = "，地址在 CGNAT 段但非 Tailscale 持有" if addr in _CGNAT_NET else ""
        live = [(k, disp, mtu) for k, _pat, disp, mtu in _TUNNEL_SOFTWARE if procs.get(k)]
        if not live:
            return "tunnel", f"隧道（无已知隧道软件在跑{cg}）"
        exact = [x for x in live if x[2] and x[2] == iface.get("mtu")]
        if len(exact) == 1:
            return exact[0][0], f"{exact[0][1]} 隧道（MTU {iface['mtu']} 吻合{cg}）"
        if len(live) == 1:
            return live[0][0], f"{live[0][1]} 隧道（本机仅此一个隧道软件在跑{cg}）"
        # 多个隧道软件并存且 MTU 也不吻合：WireGuard 隧道内地址是 10.0.8.x，OpenVPN 这条在
        # CGNAT 段——用"地址段像谁"再切一刀，仍定不下来才报候选，不猜。
        if addr in _CGNAT_NET:
            ov = [x for x in live if x[0] == "openvpn"]
            if ov:
                return "openvpn", f"OpenVPN 隧道（CGNAT 段地址，非 Tailscale 持有）"
        return "tunnel", "隧道（候选：" + " / ".join(x[1] for x in live) + "，需 `sudo wg show` 确证）"
    return "other", "其它"


def _default_iface() -> str:
    return _iface_for("default")


def _iface_for(dest: str) -> str:
    """某个目标**实际从哪个接口出去**（查内核路由表，不发包）。

    有了它才能识破 **split-tunnel**：2026-08-02 实测某 OpenVPN 场景下默认路由留在 `en0`，
    但海外目标被单独指进隧道——只看默认出接口会得出"没走隧道"的错误结论。
    """
    for line in _run(["route", "-n", "get", dest]).splitlines():
        m = re.search(r"interface:\s*(\S+)", line)
        if m:
            return m.group(1)
    return ""


def _resolve_all(host: str) -> list:
    """取**全部** A 记录，不能只取一条。

    多记录域名可能按地址分别走隧道或直接连接；只检查一条记录会遗漏出口。
    """
    import socket
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET)})
    except Exception:
        return []


def _via_of(host: str, by_name: dict) -> tuple:
    """(出接口描述, 是否多出口混合)。全部 A 记录算一遍，不一致就如实报混合，不挑一条充数。

    ⚠️ **这是提示，不是判据**：它反映"查询这一刻、这些 A 记录的路由去向"，而实际连接
    可能用了另一条记录、或 DNS 两次给了不同答案。2026-08-02 实测把一条真正走了 OpenVPN
    的请求标成了"经 en0"。**出接口与出口 IP 的归属冲突时，以出口 IP 的归属为准**——
    出口 IP 是连接真实到达对端后被对端看到的地址，路由表只是本机的一面之词。
    """
    ips = _resolve_all(host)
    ifs = sorted({_iface_for(ip) for ip in ips if ip} - {""})
    if not ifs:
        return "", False
    if len(ifs) == 1:
        return ifs[0], False
    return "混合:" + "/".join(ifs), True


# --------------------------------------------------------------------------
# 二、出口探测（按目标选源）
# --------------------------------------------------------------------------
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _one_probe(name: str, urls, scope: str) -> dict:
    row = {"probe": name, "scope": scope, "ip": "", "geo": "", "error": ""}
    if isinstance(urls, str):
        urls = [urls]
    body, errs = None, []
    for u in urls:                              # 按序降级（https → http），全失败才算失败
        try:
            body = _fetch(u)
            row["url"] = u
            break
        except Exception as e:                  # 网络抖动/被墙/限频都不该中断整轮探测
            errs.append(f"{u} {type(e).__name__}")
    if body is None:
        row["error"] = "；".join(errs)
        return row
    if "too frequent" in body.lower():          # cip.cc 的限频回包，是 200 不是 429
        row["error"] = "探测源限频（稍后重试）"
        return row
    if name == "ip-api.com":
        try:
            d = json.loads(body)
            row["ip"] = d.get("query", "")
            row["geo"] = " ".join(x for x in (d.get("country"), d.get("regionName"),
                                              d.get("city"), d.get("isp")) if x)
            return row
        except Exception:
            pass
    m = _IP_RE.search(body)
    row["ip"] = m.group(1) if m else ""
    # 两种纯文本格式：cip.cc 是「地址: 中国 江苏 常州」+「运营商: 电信」分行；
    # ipip 是一行「当前 IP：<ip>  来自于：中国 江苏 南京  电信」——不剥掉前半句会把
    # "当前 IP：" 这种噪音当成归属地渲染出去。
    geo = re.search(r"地址\s*[:：]\s*(.+)", body)
    isp = re.search(r"运营商\s*[:：]\s*(.+)", body)
    from_ = re.search(r"来自于\s*[:：]\s*(.+)", body)
    if geo:
        row["geo"] = geo.group(1).strip() + (f" {isp.group(1).strip()}" if isp else "")
    elif from_:
        row["geo"] = re.sub(r"\s+", " ", from_.group(1)).strip()
    elif row["ip"]:
        row["geo"] = re.sub(r"\s+", " ", body.replace(row["ip"], "")).strip()[:80]
    if not row["ip"]:
        row["error"] = "回包里没解析到 IPv4"
    return row


def _probe_egress() -> list[dict]:
    """三个源并发打——它们是不同主机，互不限频，串行只是白等。"""
    with _fut.ThreadPoolExecutor(max_workers=len(_PROBES)) as ex:
        return list(ex.map(lambda a: _one_probe(*a), _PROBES))


# --------------------------------------------------------------------------
# 三、出口 IP 归属反查（自有机器？已知办公出口？还是陌生 IP）
# --------------------------------------------------------------------------
def probe_targets(targets: list, by_name: dict, own: dict) -> list:
    """对**用户真正关心的目标**逐个探：解析 → 各 A 记录的出接口 → TCP 可达性。

    **为什么需要它**：三个内置探测源只代表三个"目标视角"，而分流规则是按目标匹配的——
    你要访问的那个 API/数据库很可能命中另一条规则、从另一个出口出去，甚至这条线路根本
    不通。本机存在多条出局线路，本就是为了「切一条能用的」，所以这里必须直接回答
    **通 / 不通**，而不只是描述拓扑。

    ⚠️ 能力边界：**普通目标不会回显"它看到的你的 IP"**（那是 cip.cc 这类服务才做的事），
    所以对任意目标只能给出「出接口 + 可达性 + 归属」，给不出「出口公网 IP」。别硬猜。
    """
    rows = []
    for raw in targets:
        host, _, port_s = raw.partition(":")
        ports = [int(port_s)] if port_s.isdigit() else [443, 80]
        ips = _resolve_all(host) if not re.fullmatch(r"[\d.]+", host) else [host]
        row = {"target": raw, "host": host, "ips": ips, "hops": []}
        if not ips:
            row["error"] = "DNS 解析不到"
            rows.append(row)
            continue
        for ip in ips:
            via = _iface_for(ip)
            ok, port, ms = False, None, None
            for p in ports:
                t0 = time.perf_counter()
                if _tcp_ok(ip, p):
                    ok, port, ms = True, p, int((time.perf_counter() - t0) * 1000)
                    break
            row["hops"].append({
                "ip": ip, "via": via, "via_label": (by_name.get(via) or {}).get("label", ""),
                "ok": ok, "port": port, "ms": ms, "owner": _attribute(ip, own),
            })
        row["reachable"] = any(h["ok"] for h in row["hops"])
        rows.append(row)
    return rows


def _tcp_ok(ip: str, port: int, timeout: float = 4.0) -> bool:
    import socket
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _owner_index() -> dict:
    """公网 IP → 自有实例。数据来自 data.json，即 `ops sync` 的产物，不另外查云。"""
    idx = {}
    try:
        data = json.loads(paths.data_json_path().read_text())
    except Exception:
        return idx
    for i in data.get("instances") or []:
        ip = i.get("public_ip")
        if ip:
            idx[ip] = {"kind": "own", "name": i.get("name"), "id": i.get("id"),
                       "cloud": i.get("cloud"), "region": i.get("region_display") or i.get("region"),
                       "note": i.get("note") or ""}
    return idx


def _attribute(ip: str, own: dict) -> dict:
    """给出口 IP 定性。顺序：自有实例 > 配置里登记的已知出口 > 陌生。

    "陌生"不等于有问题（家宽 IP 每天变，天然陌生），但它是唯一值得多看一眼的信号。
    """
    if not ip:
        return {}
    if ip in own:
        return own[ip]
    for label, nets in deploy_config.known_egress().items():
        for n in nets:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(n, strict=False):
                    return {"kind": "known", "name": label, "cidr": n}
            except ValueError:
                continue
    return {"kind": "unknown"}


# --------------------------------------------------------------------------
# 四、组装快照
# --------------------------------------------------------------------------
def probe(targets: list = None) -> dict:
    ts_ips = _tailscale_ips()
    procs = {k: _running(pat) for k, pat, _disp, _mtu in _TUNNEL_SOFTWARE}
    # 仅核心进程决定分流状态；安装应用或运行助手进程不代表分流已启用。
    procs["clash_core"] = _running(r"mihomo")
    procs["tailscale"] = _running(r"tailscaled")
    ifaces = _interfaces()
    for f in ifaces:
        f["role"], f["label"] = _classify(f, procs, ts_ips)
    default_if = _default_iface()
    by_name = {f["name"]: f for f in ifaces}
    egress = _probe_egress()
    own = _owner_index()
    for row, (_name, urls, _scope) in zip(egress, _PROBES):
        url = row.get("url") or urls[0]         # 用实际打通的那条 URL 反推主机名
        row["owner"] = _attribute(row["ip"], own)
        # 这个探测目标**实际走了哪个接口**——split-tunnel 下与默认出接口可以不同；
        # 多 A 记录还可能各走各的，那样出口 IP 每次请求都可能不同（已实测）
        host = re.sub(r"^https?://([^/]+)/?.*$", r"\1", url)
        via, mixed = _via_of(host, by_name)
        row["via_interface"] = via
        row["via_mixed"] = mixed
        row["via_label"] = (by_name.get(via) or {}).get("label", "")
        # fake-ip 下所有域名都被解析成 198.18.x 送进 TUN，路由表对**每个**目标都答"进 TUN"，
        # 分流实际发生在 Clash 内部的规则里——此时「出接口」零信息量，只有出口 IP 说真话。
        row["via_blind"] = (by_name.get(via) or {}).get("role") == "clash"
    lan = next((f for f in ifaces if f["role"] == "lan"), None)
    tgt = probe_targets(targets, by_name, own) if targets else []
    return {
        "targets": tgt,
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "default_interface": default_if,
        "default_role": (by_name.get(default_if) or {}).get("role", ""),
        "default_label": (by_name.get(default_if) or {}).get("label", ""),
        "lan_ipv4": (lan or {}).get("ipv4", ""),
        # 默认路由与各目标实际出接口不一致 = split-tunnel（隧道只接管部分路由）
        "split_tunnel": bool({r.get("via_interface") for r in egress if r.get("via_interface")} - {default_if}),
        # 有目标的多条 A 记录分走不同接口 → 同一目标每次请求的出口 IP 都可能不同
        "unstable_egress": any(r.get("via_mixed") for r in egress),
        # fake-ip TUN 在管：本机路由表对所有目标都答"进 TUN"，判不出分流，只能看出口 IP
        "route_table_blind": any(r.get("via_blind") for r in egress),
        "processes": procs,
        "interfaces": ifaces,
        "egress": egress,
    }


# --------------------------------------------------------------------------
# 五、渲染
# --------------------------------------------------------------------------
def _owner_text(o: dict) -> str:
    if not o:
        return ""
    if o.get("kind") == "own":
        return f"自有 {o.get('name')}（{o.get('cloud')} {o.get('region')}）"
    if o.get("kind") == "known":
        return f"{o.get('name')}（{o.get('cidr')}）"
    return "⚠️ 未登记（家宽 IP 每天变属正常；其余值得看一眼）"


def render_lines(snap: dict) -> list[str]:
    """渲染成 inventory.md 的一节。**带 probed_at**——过期与否要一眼看得出来。"""
    if not snap:
        return []
    L = ["\n---\n", "## 🧭 本机出口链路（`ops netprobe` 探测）\n",
         f"> 探测时间：{snap.get('probed_at', '—')}　·　"
         f"默认出接口：`{snap.get('default_interface') or '—'}`"
         f"（{snap.get('default_label') or '未识别'}）",
         "> ⚠️ 这是**快照**，随所在网络与隧道启停而变；`utun` 编号每次重连都会被重新分配，"
         "**按网段/MTU 特征认接口，别按编号认**。要最新值重跑 `ops netprobe`。\n"]
    L.append("| 接口 | IPv4 | MTU | 出局模型 |")
    L.append("|---|---|---|---|")
    for f in snap.get("interfaces", []):
        if f.get("role") in ("loopback", "other"):
            continue
        star = " ⭐默认" if f["name"] == snap.get("default_interface") else ""
        L.append(f"| `{f['name']}`{star} | {f['ipv4']} | {f.get('mtu') or '—'} | {f.get('label', '')} |")
    L.append("")
    L.append("**出口 IP（按目标分别探——三个源打到的是同一条链上的三个出口点，不是三条并行线路）**\n")
    if snap.get("split_tunnel"):
        L.append("> ⚠️ **split-tunnel**：隧道只接管部分路由，**默认出接口 ≠ 各目标实际出接口**——"
                 "只看默认路由会误判成\"没走隧道\"。下表「出接口」列是各目标查内核路由表得到的真实去向。\n")
    if snap.get("unstable_egress"):
        L.append("> ⚠️ **出口不稳定**：标「混合」的目标，其多条 A 记录分走不同接口，"
                 "**同一目标每次请求的出口 IP 都可能不同**（实测 15 秒内在直出与走隧道之间跳）。"
                 "这种状态下\"我的出口 IP 是 X\"这句话本身就不成立，加白/溯源要按连接逐条看。\n")
    L.append("> 「出接口」是查本机路由表得到的**提示**，非判据（多 A 记录下实际连接可能用了另一条）。"
             "**与「反查」冲突时以反查为准**——出口 IP 是对端真实看到的地址。\n")
    if snap.get("route_table_blind"):
        L.append("> ⚠️ **fake-ip TUN 在管，「出接口」列此时零信息量**：域名全被解析成 `198.18.x` "
                 "送进 TUN，路由表对每个目标都答「进 TUN」，真正的分流在 Clash 的规则里。"
                 "此状态下判断走没走代理**只能看出口 IP**。\n")
    L.append("| 探测源 | 目标视角 | 出接口 | 出口 IP | 归属 | 反查 |")
    L.append("|---|---|---|---|---|---|")
    for r in snap.get("egress", []):
        ip = r.get("ip") or f"— {r.get('error', '')}"
        via = f"`{r['via_interface']}`" if r.get("via_interface") else "—"
        L.append(f"| {r['probe']} | {r['scope']} | {via} | {ip} | {r.get('geo') or '—'} | "
                 f"{_owner_text(r.get('owner'))} |")
    return L


def _print_human(snap: dict) -> None:
    print(f"🧭 本机出口链路　探测于 {snap['probed_at']}")
    print(f"   默认出接口: {snap.get('default_interface') or '—'}"
          f"（{snap.get('default_label') or '未识别'}）"
          f"{'　局域网 ' + snap['lan_ipv4'] if snap.get('lan_ipv4') else ''}")
    print("\n· 接口全景（按网段/MTU 特征认，编号仅供参考、每次重连都会变）")
    for f in snap.get("interfaces", []):
        if f.get("role") in ("loopback", "other"):
            continue
        star = " ⭐" if f["name"] == snap.get("default_interface") else "  "
        print(f"  {star}{f['name']:<10} {f['ipv4']:<16} mtu={str(f.get('mtu') or '—'):<5} {f.get('label', '')}")
    if snap.get("split_tunnel"):
        print("   ⚠️ split-tunnel：隧道只接管部分路由，默认出接口 ≠ 各目标实际出接口（见下「出接口」）")
    if snap.get("unstable_egress"):
        print("   ⚠️ 出口不稳定：有目标的多条 A 记录分走不同接口，同一目标每次请求的出口 IP 都可能不同")
    if snap.get("route_table_blind"):
        print("   ⚠️ fake-ip TUN 在管：域名全被解析成 198.18.x 送进 TUN，路由表对每个目标都答"
              "「进 TUN」——「出接口」列在此零信息量，分流在 Clash 规则里，只看出口 IP")
    print("\n· 出口 IP（按目标分别探；这是一条会再分流的链，不是单一出口）")
    for r in snap.get("egress", []):
        via = f"经 {r['via_interface']}" if r.get("via_interface") else ""
        if r.get("error") and not r.get("ip"):
            print(f"    {r['probe']:<14} {r['scope']:<22} ✗ {r['error']}")
            continue
        print(f"    {r['probe']:<14} {r['scope']:<22} {r['ip']:<16} {via:<10} {r.get('geo', '')}")
        own = _owner_text(r.get("owner"))
        if own:
            print(f"    {'':<14} {'':<22} └─ {own}")
    if snap.get("targets"):
        print("\n· 指定目标（`--target`）——回答「我访问这个东西时从哪出去、通不通」")
        for t in snap["targets"]:
            mark = "✅ 通" if t.get("reachable") else "✗ 不通"
            print(f"    {t['target']}　{mark}")
            if t.get("error"):
                print(f"      └─ {t['error']}")
            for h in t.get("hops", []):
                st = f"{h['port']}/tcp {h['ms']}ms" if h["ok"] else "拒绝/超时"
                ow = _owner_text(h.get("owner"))
                print(f"      {h['ip']:<16} 经 {h['via'] or '?':<8} {st:<18}"
                      f"{('· ' + ow) if ow and h.get('owner', {}).get('kind') != 'unknown' else ''}")
        if not all(t.get("reachable") for t in snap["targets"]):
            print("    ⚠️ 有目标不通——本机有多条出局线路，切换一条再探是常规解法"
                  "（切完重跑 `ops netprobe --target …` 对比）")


def main(argv: list[str]) -> int:
    if any(a in argv for a in ("-h", "--help", "help")):
        print("用法: ops netprobe [--target <host[:port]>]... [--json] [--no-write]\n"
              "  探测本机出口链路（接口全景 + 按目标分别探出口 IP + 归属反查），纯只读。\n"
              "  --target  额外探你真正关心的目标：解析 → 各 A 记录的出接口 → TCP 可达性，\n"
              "            回答「访问它时从哪出去、通不通」。可重复。本机有多条出局线路，\n"
              "            探到不通就换一条再探——这正是备多条线路的意义。\n"
              "            ⚠️ 普通目标不回显「它看到的你的 IP」，故只给出接口+可达性，不给出口 IP。\n"
              "  默认把快照写进 data.json 的 local_network 键，并在下次 `ops sync` 时\n"
              "  渲染进 inventory.md；--no-write 只打印不落盘。")
        return 0
    targets = [argv[i + 1] for i, a in enumerate(argv) if a == "--target" and i + 1 < len(argv)]
    targets += [a.split("=", 1)[1] for a in argv if a.startswith("--target=")]
    snap = probe(targets)
    if "--json" in argv:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        _print_human(snap)
    if "--no-write" in argv:
        return 0
    path = paths.data_json_path()
    try:
        data = json.loads(path.read_text())
    except Exception:
        print("\n⚠️ 尚无 data.json（先跑一次 `ops sync`），本次只打印未落盘。", file=sys.stderr)
        return 0
    data["local_network"] = snap
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n✅ 快照已写入 {path} 的 local_network；下次 `ops sync` 会渲染进 inventory.md。")
    return 0
