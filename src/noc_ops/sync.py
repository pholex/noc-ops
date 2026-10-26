#!/usr/bin/env python3
"""ops sync —— 同步 EC2 实例清单与 SSH 配置。

做三件事：
1. 扫所有 profile/region 的 EC2 实例，和上次快照对比 → 报告 新增/变更/删除。
2. 生成 ~/.ssh/config 里由本工具托管的 AWS 段（用 marker 包裹，段外内容不动）。
3. 对登录用户名未知的实例（仅 ec2-user / ubuntu 两种）自动探测并缓存，再重写 inventory。

幂等：实例没变、用户名已缓存时，再次运行输出「无变更」，不做任何 SSH 探测。

只能确定登录用户名的途径是真连一下试——AWS API 不提供。其余字段
（key 文件由 KeyName 映射、直连/跳板由有无公网 IP 决定）全部从 API 推导。

默认全量同步**不扫非公网机器**（local/sangfor/vmware/synology——要么依赖内网/VPN、
要么依赖 Tailscale，连不上时不该被误判为"已删除"），沿用 data.json 里上次的记录；
要刷新它们用 `--local`（内网）或 `--tailscale`（Tailscale 组网）。

三条纯本地（不触云账号 API）的轻量刷新，各司其职：
- `--local`（亦作 `--no-cloud`）走 refresh_net("lan")：只重扫**内网**机器，门控是内网探针
  （local_net.probe 全不通 = 不在内网 → 整批沿用）。
- `--tailscale`（亦作 `--ts`）走 refresh_net("tailscale")：只重扫挂在 **Tailscale 组网**上的
  机器，门控是 `tailscale status`（tailscaled 没跑 = 整批沿用；对端节点离线则该账号单独沿用）。
  两个网络域独立探测；单个节点不可达时保留该节点上次的快照。
- `--index`（亦作 `--runbooks` / `--logs`）走 refresh_index：只把 runbooks/ 与操作日志
  重新索引进 data.json（**零网络**、秒级），沉淀剧本或想让操作日志立即上看板时用。

两条按维度收窄的云刷新（都是 patch 式合并写回 data.json，不整份覆盖）：
- `--kind=<键>` 走 refresh_kind：按**资源类型**收窄（看板「按 tab 单刷」），扫所有账号。
- `--account=<账号>` 走 refresh_account：按**云账号**收窄（AWS 账号 ID，或阿里云的 profile 名／
  账号 ID），只扫该账号的资源，其它账号 / 其它云的行原样沿用。可与 `--kind=` 组合
  （`--account=X --kind=instances` = 只刷 X 的实例）。支持哪些云见 ACCOUNT_SCOPED_KINDS。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from . import collection
from . import deploy_config
from . import netprobe
from . import ops_log
from . import paths
from . import schedule_ops
from .providers import PROVIDERS
from .providers.base import Provider

_MISSING_CLI_WARNED = set()


def _collection_session(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if collection.current() is not None:
            return func(*args, **kwargs)
        with collection.Session(_load_json(NOC_DATA, {})) as session:
            try:
                return func(*args, **kwargs)
            finally:
                session.report()
    return wrapped


def _provider_rows(provider, kind):
    """Commit one account/type at a time; failed accounts keep their previous rows."""
    method = getattr(provider, "list_" + kind)
    session = collection.current()
    if session is None or provider.cloud in LOCAL_NET_CLOUDS:
        return method()
    if getattr(type(provider), "list_" + kind, None) is getattr(Provider, "list_" + kind, None):
        return []
    cloud = provider.cloud
    accounts = deploy_config.aws_accounts() if cloud == "aws" else deploy_config.cloud_accounts(cloud)
    if cloud == "aws" and kind == "domains":
        accounts = [{"profile": p} for p in deploy_config.route53_profiles()]
    # Azure billing returns every subscription in a shared billing profile and
    # deduplicates profiles internally. Splitting that call by subscription would
    # count the same charges several times, so commit this inventory together.
    if cloud == "azure" and kind == "billing" and accounts:
        old = [r for r in session.previous.get(kind, []) if _row_cloud(kind, r) == cloud]
        return session.collect(cloud, "计费档案（全部订阅）", kind, old, method,
                               unavailable=bool(provider.cli and not shutil.which(provider.cli)))
    out = []
    original_scope = deploy_config.account_scope(cloud)
    try:
        for a in accounts:
            label = (a.get("project") if cloud == "gcp" else
                     a.get("subscription") if cloud == "azure" else
                     a.get("profile") if cloud == "aws" else
                     a.get("account") or a.get("profile") or cloud)
            label = str(label or cloud)
            scope = a.get("profile") or label
            deploy_config.set_account_scope(cloud, scope)
            old = [r for r in session.previous.get(kind, [])
                   if _row_cloud(kind, r) == cloud and _row_account(kind, r) == label]
            def collect():
                # These CLIs currently use one default identity. A second configured
                # account cannot safely be queried with that identity.
                if cloud in {"tencent", "volcengine"} and a != deploy_config.all_cloud_accounts(cloud)[0]:
                    collection.failed("account_not_supported")
                    return []
                return method()
            out.extend(session.collect(cloud, label, kind, old, collect,
                                       unavailable=bool(provider.cli and not shutil.which(provider.cli))))
    finally:
        deploy_config.set_account_scope(cloud, original_scope)
    return out


def _providers(clouds=None):
    """云采集会保留缺失 CLI 的 provider，由 session 记录失败并沿用旧行。
    非采集调用仍跳过不可用 CLI。clouds=None 为全部，否则只要这几朵。"""
    out = {}
    for c, p in PROVIDERS.items():
        if clouds is not None and c not in clouds:
            continue
        if p.cli and shutil.which(p.cli) is None and collection.current() is None:
            try:
                configured = deploy_config.aws_accounts() if c == "aws" else deploy_config.cloud_accounts(c)
            except Exception:
                configured = []
            if configured and c not in _MISSING_CLI_WARNED:
                _MISSING_CLI_WARNED.add(c)
                print(f"  ⚠️ 未找到 {p.cli} CLI，跳过 {p.display}（装好后重跑 ops sync）")
            continue
        out[c] = p
    return out
from .providers.base import resolve_keyfile
from .providers.aws import REGION_META, regions_for

# --------------------------------------------------------------------------
# 配置（数据落在工作区，见 paths.py）
# --------------------------------------------------------------------------
INVENTORY = paths.inventory_path()
NOC_DATA = paths.data_json_path()
RUNBOOKS_DIR = paths.runbooks_dir()
SSH_CONFIG = Path.home() / ".ssh" / "config"
USER_CACHE_FILE = paths.ssh_users_path()          # {instance_id: {"user":..,"verified":bool}}
SNAPSHOT_FILE = paths.snapshot_path()             # {instance_id: {...fields}}

MARKER_BEGIN = "# >>> ops-sync managed (AWS EC2) — 勿手改，由 ops sync 生成 >>>"
MARKER_END = "# <<< ops-sync managed (AWS EC2) <<<"

LOCAL_MARKER_BEGIN = "# >>> ops-sync managed (本地) — 勿手改，由 ops sync 生成 >>>"
LOCAL_MARKER_END = "# <<< ops-sync managed (本地) <<<"

# 账号 / 扫描区域 / SSH 别名前缀 / 跳板机 等部署配置已抽到 ops-config.toml（经 deploy_config 读）。
# REGION_META（通用 AWS 区域元数据）仍在 providers/aws.py。

# 非 AWS 云的实例/域名拉取已搬到 providers/<cloud>.py（TencentProvider/AliyunProvider 等）。
# 这里只留 inventory 展示用的少量映射。

# 非 AWS 云在 inventory 里的分区标题
CLOUD_SECTION = {"tencent": "腾讯云 Lighthouse", "aliyun": "阿里云 ECS", "volcengine": "火山引擎 ECS", "gcp": "Google Cloud",
                 "azure": "Microsoft Azure", "oracle": "Oracle Cloud"}

# 非公网机器（要内网/VPN 或 Tailscale 才连得到，常规全量刷新连不上时不该判定为"已删除"）：
# 全量 `ops sync` 默认跳过这些 provider 的扫描、沿用上次数据。
# 它们再按**网络域**分两批刷：`ops sync --local`（lan）/ `ops sync --tailscale`（tailscale）。
LOCAL_NET_CLOUDS = {"local", "sangfor", "vmware", "synology"}

# 云厂商默认排序（单一来源 = 各 Provider.order：AWS=0 → 阿里=1 → 腾讯=2）
CLOUD_RANK = {c: p.order for c, p in PROVIDERS.items()}
REGISTRAR_RANK = {"AWS Route53": 0, "阿里云": 1, "腾讯云": 2, "火山引擎": 3, "Cloudflare": 4, "Azure DNS": 5, "OCI DNS": 6, "Google Cloud": 7}
# 域名行是多注册商混排、行里没有 cloud 键，只能按注册商反查所属云（`--account=` 收窄时要用）
REGISTRAR_CLOUD = {"AWS Route53": "aws", "阿里云": "aliyun", "腾讯云": "tencent", "火山引擎": "volcengine",
                   "Cloudflare": "cloudflare", "Azure DNS": "azure", "OCI DNS": "oracle", "Google Cloud": "gcp"}


def resolve_prefix(profile, region, used):
    """(profile,region) 的别名前缀：优先用映射表，否则用区域昵称；撞名则附账号尾号。"""
    if (profile, region) in deploy_config.aws_alias_prefix():
        return deploy_config.aws_alias_prefix()[(profile, region)]
    nick = REGION_META.get(region, (region,))[0]
    return nick if nick not in used else f"{nick}-{profile[-4:]}"


def build_scopes():
    """并发扫描所有 账号×区域，返回 [(scope, instances)]（仅含有实例的区域）。"""
    tasks = [(a["profile"], r) for a in deploy_config.aws_accounts() for r in regions_for(a)]
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        session = collection.current()
        def scan(p, r):
            if session is None:
                return describe(p, r)
            old = [{**i, "_retained_row": i} for i in session.previous.get("instances", [])
                   if i.get("cloud") == "aws" and i.get("profile") == p and i.get("region") == r]
            return session.collect("aws", p, "instances", old, lambda: describe(p, r), region=r,
                                   unavailable=shutil.which("aws") is None)
        futs = {ex.submit(scan, p, r): (p, r) for p, r in tasks}
        for f in as_completed(futs):
            p, r = futs[f]
            try:
                results[(p, r)] = f.result()
            except Exception:
                results[(p, r)] = []
    used, scoped = set(), []
    for account in deploy_config.aws_accounts():
        for region in regions_for(account):
            insts = results.get((account["profile"], region), [])
            if not insts:
                continue
            prefix = resolve_prefix(account["profile"], region, used)
            used.add(prefix)
            ja, ji = deploy_config.aws_jumps().get((account["profile"], region), (None, None))
            nick, disp, flag = REGION_META.get(region, (region, region, ""))
            scoped.append(({
                "profile": account["profile"], "region": region, "prefix": prefix,
                "jump_alias": ja, "jump_instance": ji,
                "region_display": disp, "flag": flag, "nick": nick,
            }, insts))
    return scoped

# resolve_keyfile（~/.ssh/<profile>/<KeyName>.pem 推导）已搬到 providers.base，本文件顶部 import。

PROBE_USERS = ["ec2-user", "ubuntu", "centos"]  # 按 AMI 家族的默认登录名依次试（Amazon Linux / Ubuntu / CentOS 官方镜像）

# 比较快照时关注的字段
WATCH_FIELDS = ["name", "type", "private_ip", "public_ip", "az", "vpc", "key_name", "state"]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, obj):
    if path == NOC_DATA and collection.current() is not None:
        collection.current().attach(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same-directory replace: readers see the old or the new complete document.
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def describe(profile, region):
    """返回该 profile/region 的实例字段列表。"""
    r = subprocess.run(
        ["aws", "ec2", "describe-instances", "--profile", profile, "--region", region,
         "--output", "json"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if r.returncode != 0:
        # 未启用/无权限的区域由采集状态统一报告；其它错误额外打印 CLI 诊断。
        err = r.stderr.strip()
        if not any(k in err for k in ("not authorized", "opt-in", "could not be found",
                                      "AuthFailure", "not enabled", "UnauthorizedOperation")):
            print(f"  ✗ describe-instances 失败 ({profile}/{region}): {err}", file=sys.stderr)
        return None
    data = json.loads(r.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("Reservations"), list):
        return None
    out = []
    for resv in data.get("Reservations", []):
        for inst in resv.get("Instances", []):
            name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), None)
            out.append({
                "id": inst["InstanceId"],
                "name": name or inst["InstanceId"],
                "type": inst.get("InstanceType"),
                "private_ip": inst.get("PrivateIpAddress"),
                "public_ip": inst.get("PublicIpAddress"),
                "az": inst.get("Placement", {}).get("AvailabilityZone"),
                "vpc": inst.get("VpcId"),
                "key_name": inst.get("KeyName"),
                "state": inst.get("State", {}).get("Name"),
            })
    return out


def make_alias(scope, inst):
    """生成 SSH 别名。跳板机用固定别名；带时间戳的名字去掉末尾 -HHMMSS。"""
    if scope["jump_instance"] and inst["name"] == scope["jump_instance"]:
        return scope["jump_alias"]
    short = re.sub(r"(-\d{8})-\d{6}$", r"\1", inst["name"])
    return f"{scope['prefix']}-{short}"


def connection_of(scope, inst):
    """返回 (mode, hostname, proxyjump)。有公网 IP → 直连；否则经本 region 跳板。

    无密钥对（KeyName 为空）→ "ssm"：模板刻意不开 SSH（典型：CFN Guard 强制 KeyName !exists、
    无公网、SG 零入站，实例角色挂 AmazonSSMManagedInstanceCore），只能 SSM Session Manager。
    这是设计不是缺失：不找 pem、不探 SSH、不进 ssh config，看板/inventory 连接列标 SSM。
    """
    if not inst.get("key_name"):
        return "ssm", None, None
    if inst["public_ip"]:
        return "direct", inst["public_ip"], None
    if scope["jump_alias"]:
        return "jump", inst["private_ip"], scope["jump_alias"]
    return "direct", inst["private_ip"], None  # 无公网且无跳板，只能尝试私网（多半不可达）


def ssh_block(alias, hostname, user, keyfile, proxyjump):
    lines = [f"Host {alias}",
             f"    HostName {hostname}",
             f"    User {user}",
             f"    IdentityFile {keyfile}"]
    if proxyjump:
        lines.append(f"    ProxyJump {proxyjump}")
    lines.append("    StrictHostKeyChecking accept-new")
    return "\n".join(lines)


def write_ssh_config(block_body):
    cfg = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    managed = f"{MARKER_BEGIN}\n{block_body}\n{MARKER_END}\n"
    if MARKER_BEGIN in cfg and MARKER_END in cfg:
        pre = cfg.split(MARKER_BEGIN)[0].rstrip()
        post = cfg.split(MARKER_END, 1)[1].lstrip("\n")
        new = f"{pre}\n\n{managed}{post}"
    else:
        new = (cfg.rstrip() + "\n\n" if cfg.strip() else "") + managed
    SSH_CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)   # 新机器可能还没有 ~/.ssh
    SSH_CONFIG.write_text(new)
    os.chmod(SSH_CONFIG, 0o600)


def write_local_ssh_config(local_insts):
    """将本地 Linux SSH 机器写入 ~/.ssh/config 独立托管段。

    只收**本地网络云**的机器（cloud ∈ LOCAL_NET_CLOUDS）。公有云实例一律不收——否则配了 ssh_user 的
    云实例（阿里云/GCP…）会以内网 IP 再写一条同名 Host，与主托管段重复。
    ⚠️ 别用 network 字段过滤：全量 sync 沿用的旧 data.json 行可能没打过 network（老记录），按它过滤会把
    本地机器整段从 ssh config 里抹掉，直到下次 --local/--tailscale 才回来（2026-10-24 审查发现）。
    """
    ssh_entries = [
        i for i in local_insts
        if i.get("cloud") in LOCAL_NET_CLOUDS and i.get("ssh_alias") and i.get("private_ip") and i.get("ssh_user")
    ]
    if not ssh_entries:
        return
    lines = []
    for i in sorted(ssh_entries, key=lambda x: x["ssh_alias"]):
        lines.append(f"Host {i['ssh_alias']}")
        lines.append(f"    HostName {i['private_ip']}")
        lines.append(f"    User {i['ssh_user']}")
        if i.get("ssh_port") and int(i["ssh_port"]) != 22:
            lines.append(f"    Port {i['ssh_port']}")
        lines.append("    StrictHostKeyChecking no")
        lines.append("")
    block_body = "\n".join(lines).rstrip()
    managed = f"{LOCAL_MARKER_BEGIN}\n{block_body}\n{LOCAL_MARKER_END}\n"
    cfg = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    if LOCAL_MARKER_BEGIN in cfg and LOCAL_MARKER_END in cfg:
        pre = cfg.split(LOCAL_MARKER_BEGIN)[0].rstrip()
        post = cfg.split(LOCAL_MARKER_END, 1)[1].lstrip("\n")
        new = f"{pre}\n\n{managed}{post}"
    else:
        new = (cfg.rstrip() + "\n\n" if cfg.strip() else "") + managed
    SSH_CONFIG.write_text(new)
    os.chmod(SSH_CONFIG, 0o600)


def _clear_changed_hostkey(stderr):
    """stderr 若表明 host key 变更（换机/复用 IP 常见），删掉旧指纹并返回被清理的 host；否则 None。

    accept-new 只接受全新主机，遇到"已存在但 key 变了"会在握手阶段直接拒绝，
    错误跟用户名无关。这里识别出该情形、`ssh-keygen -R <host>` 清掉陈旧指纹，
    好让调用方重试时按新 key 重新信任。
    """
    if "HOST IDENTIFICATION HAS CHANGED" not in stderr and "host key" not in stderr.lower():
        return None
    m = re.search(r"Host key for (?:host )?(\S+?) has changed", stderr)
    host = m.group(1) if m else None
    if host:
        subprocess.run(["ssh-keygen", "-R", host], capture_output=True, text=True)
    return host


def probe_user(alias):
    """对别名按 PROBE_USERS（ec2-user→ubuntu→centos）逐个试 SSH，返回能登的用户名或 None。

    若因 host key 变更（换机/复用 IP）握手失败，自动清掉旧指纹后重试一轮——
    这类失败与用户名无关，不清指纹换多少用户名都连不上。
    """
    def _try(u):
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new", "-l", u, alias, "echo __OK__"],
            capture_output=True, text=True, timeout=40,
        )

    cleared = False
    for u in PROBE_USERS:
        r = _try(u)
        if r.returncode == 0 and "__OK__" in r.stdout:
            return u
        if not cleared:                       # 每台只清一次，避免反复 ssh-keygen -R
            host = _clear_changed_hostkey(r.stderr)
            if host:
                cleared = True
                print(f"    ⚠ {alias}: host key 已变更（换机/复用 IP），已清除旧指纹 {host}，重试中")
                r = _try(u)
                if r.returncode == 0 and "__OK__" in r.stdout:
                    return u
    return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _md_section(lines, heading):
    """返回某个 `## <heading>` 标题下、到下一个 `## ` 之前的所有行。"""
    res, cap = [], False
    for l in lines:
        if l.startswith("## "):
            cap = heading in l
            continue
        if cap:
            res.append(l)
    return res


def scan_runbooks():
    """扫 runbooks/*.md（排除 _ 开头的模板），抽元数据供 NOC「剧本」清单。"""
    out = []
    if not RUNBOOKS_DIR.is_dir():
        return out
    for f in sorted(RUNBOOKS_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        title = next((l[2:].strip() for l in lines if l.startswith("# ")), f.stem)
        m = next((re.search(r"沉淀时间[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", l) for l in lines
                  if "沉淀时间" in l), None)
        date_s = m.group(1) if m else ""
        bullets = [re.sub(r"^[-*]\s*", "", l).strip().strip('"“”')
                   for l in _md_section(lines, "触发意图") if l.strip().startswith(("-", "*"))]
        intent = " / ".join(bullets[:2])
        wsec = " ".join(_md_section(lines, "是否写操作")).strip()
        write_op = "是" if wsec.startswith("是") else "否" if wsec.startswith("否") else "—"
        dist = (RUNBOOKS_DIR / "dist" / f.name).is_file()   # dist/ 下有同名副本 = 可分发（通用，无环境耦合）
        out.append({"name": f.stem, "title": title, "date": date_s,
                    "write_op": write_op, "intent": intent, "lines": len(lines), "dist": dist})
    out.sort(key=lambda r: r["date"], reverse=True)   # 新沉淀的在前
    return out


def _local_net_reachable():
    """**内网**（lan）连通性预检：TCP 探 local_net.probe 任一目标通即判定"在内网"。

    返回 (ok, probes)。probes 为空 = 未配置预检 → ok=True（老行为，不门控）。
    用于 refresh_net("lan")：不在内网/未连 VPN 时别把内网机器逐台探成超时再误标 stopped，
    直接沿用上次数据。**只管 lan**——Tailscale 域的门控是 `tailscale status`，见 _net_gate。
    """
    probes = deploy_config.local_net_probe()
    if not probes:
        return True, []
    import socket
    for target in probes:
        host, _, port = target.partition(":")
        try:
            with socket.create_connection((host, int(port or 22)), timeout=2):
                return True, probes
        except (OSError, ValueError):
            continue
    return False, probes


def _net_gate(network):
    """某网络域当前是否可达 → (ok, 说明)。两个域的判定手段**根本不同**：

    - lan：探内网管理口（local_net.probe），全不通 = 人不在内网/没连 VPN；
    - tailscale：问 `tailscale status`——本机 tailscaled 没跑就整批免谈，
      至于**单个节点**是否在线由各 provider 的 endpoint_alive 逐账号判（0 网络等待）。
    """
    if network == netprobe.NET_TAILSCALE:
        ok, why = netprobe.tailnet_up()
        return ok, why or "Tailscale 已连接"
    ok, probes = _local_net_reachable()
    return ok, ("探测 " + ", ".join(probes) + " 均不可达") if not ok else "内网可达"


def _account_networks():
    """{(cloud, _acct): network}——供老记录（没有 `network` 字段）回溯归属哪个网络域。"""
    out = {}
    for cloud in LOCAL_NET_CLOUDS:
        for a in deploy_config.cloud_accounts(cloud):
            out[(cloud, a.get("account", a.get("username", "")))] = a["network"]
    return out


def _inst_network(inst, acct_map=None):
    """一条实例记录属于哪个网络域。

    新记录由 provider 直接写 `network` 字段；老 data.json 里没有，则按
    (cloud,_acct) 回查配置，再退到按 IP 推导（100.64/10 → tailscale），最后默认 lan。
    """
    if inst.get("network"):
        return inst["network"]
    if acct_map is None:
        acct_map = _account_networks()
    key = (inst.get("cloud"), inst.get("_acct"))
    if key in acct_map:
        return acct_map[key]
    return netprobe.network_of(inst.get("private_ip") or inst.get("connect") or "")


def _scan_local_net(prev_instances, network=None):
    """扫 local/sangfor/vmware/synology 里属于 `network` 域的账号（None = 全部）。

    **账号级沿用**：连不上的账号（provider.unreachable_accounts()，按记录的 `_acct` 匹配）
    沿用上次 data.json 里的记录，连得上的账号里缺失的机器才算真删除——这样多设备云
    （synology 两台 NAS、vmware 多台 ESX）部分连通时，超时的那台不会被在线的那台挤掉，
    而 sangfor 平台上真被销毁的 VM 仍会正常消失。整个 provider 抛异常时，整朵云沿用旧记录。
    `prev_instances` 应已按同一网络域筛过，沿用才不会跨域串味。
    """
    merged = []
    for cloud in sorted(LOCAL_NET_CLOUDS):   # 排序：输出顺序稳定（set 迭代序随 hash 种子变）
        p = PROVIDERS.get(cloud)
        if p is None:
            continue
        # 逐朵云报进度（开扫一行、结果一行），别让终端静默几十秒看着像卡死；
        # 两行分开是为了给 provider 的 stderr 告警留位置，不至于插进半行里。
        print(f"  · 扫描 {cloud} …", flush=True)
        try:
            fresh = p.list_instances(network=network)
        except Exception as e:
            kept = [i for i in prev_instances if i.get("cloud") == cloud]
            print(f"  ⚠️ {cloud} 扫描异常（{e}），整朵云沿用上次 {len(kept)} 台", file=sys.stderr)
            merged.extend(kept)
            continue
        print(f"  ✓ {cloud} {len(fresh)} 台", flush=True)
        merged.extend(fresh)
        fresh_ids = {i.get("id") for i in fresh}
        for acct in p.unreachable_accounts():
            kept = [i for i in prev_instances
                    if i.get("cloud") == cloud and i.get("_acct") == acct
                    and i.get("id") not in fresh_ids]
            if kept:
                print(f"  ⏭  {cloud}/{acct} 本轮未连上，沿用上次 {len(kept)} 台", file=sys.stderr)
            merged.extend(kept)
    return merged


def refresh_index(quiet=False):
    """只把纯本地索引 `runbooks` + `ops_log` 重刷进 data.json，**零网络**（不触云账号 API、
    也不扫本地网络机器），秒级。

    用于「新增/改了 runbook 想让 NOC 机读清单立刻登记」，或「想让操作日志立即上看板」——
    inventory.md 不含剧本/日志、云快照 generated_at 也不动，只 patch 这两个键。
    需先有一份 data.json（至少跑过一次完整 `ops sync`）。
    """
    paths.ensure_workspace()
    data = _load_json(NOC_DATA, None)
    if not data:
        print("✗ 尚无 data.json，先跑一次完整 `ops sync` 再用 --index。", file=sys.stderr)
        return 2
    ops_log.backfill_accounts()                 # 历史日志缺 account 的反查补入（幂等）
    local = {"runbooks": scan_runbooks(), "ops_log": ops_log.tail()}
    data.update(local)
    data.setdefault("totals", {}).update({k: len(v) for k, v in local.items()})
    _save_json(NOC_DATA, data)
    if not quiet:
        print(f"✅ 剧本 {len(local['runbooks'])} 个 / 操作日志 {len(local['ops_log'])} 条已刷新，"
              f"零网络未触云。\n已更新：{NOC_DATA}")
    return 0


def refresh_net(network, quiet=False):
    """只重扫某个**网络域**（lan=内网 / tailscale=Tailscale 组网）的机器，**不触云账号 API**。

    复用上次全量 `ops sync` 写入的云账号数据（实例/域名/数据库/存储/函数/IAM/消息/计划任务
    原样保留），也**不动另一个网络域**的记录——只替换 instances 里本域那部分并回写。
    用于「回到内网后单刷内网机器」或「在外面单刷 Tailscale 上的机器」，不必为此全量重扫所有云。
    **纯本地索引（剧本/日志）不在此，用 `--index`。** 需先有一份 data.json。
    """
    label = netprobe.NET_LABEL.get(network, network)
    paths.ensure_workspace()
    data = _load_json(NOC_DATA, None)
    if not data:
        flag = "--local" if network == netprobe.NET_LAN else "--tailscale"
        print(f"✗ 尚无 data.json，先跑一次完整 `ops sync` 再用 {flag}。", file=sys.stderr)
        return 2
    prev_all = data.get("instances") or []
    acct_map = _account_networks()

    def _in_scope(i):
        return i.get("cloud") in LOCAL_NET_CLOUDS and _inst_network(i, acct_map) == network

    prev_scope = [i for i in prev_all if _in_scope(i)]      # 本域上次的记录（沿用兜底用）
    keep = [i for i in prev_all if not _in_scope(i)]        # 云账号 + 另一个网络域，原样保留

    net_ok, why = _net_gate(network)
    if net_ok:
        print(f"· 刷新 {label} 机器（{why}）...", flush=True)
        scoped = _scan_local_net(prev_scope, network=network)
    else:
        # 门控未过：整个网络域此刻不可达。别逐台探成超时再误标 stopped——本域整体沿用上次数据。
        scoped = prev_scope
        print(f"  ⏭  {label} 门控未通过（{why}）——本域 {len(scoped)} 台沿用上次记录、不重扫。",
              file=sys.stderr)

    # 沿用记录（节点离线/门控没过时来自旧 data.json）可能还是没有 network 字段的老格式，
    # 而本域归属这里是**确定**的——补齐标签，让 data.json 自描述、看板也把两域分开显示。
    for i in scoped:
        i["network"] = network
        if i.get("region_display") in netprobe.NET_LABEL.values():   # local 云的 region 是机器类别，别动
            i["region"] = i["region_display"] = netprobe.NET_LABEL[network]

    all_insts = keep + scoped

    def _ip_key(x):
        try:
            return tuple(int(p) for p in (x.get("private_ip") or "").split("."))
        except Exception:
            return (0, 0, 0, 0)

    all_insts.sort(key=lambda x: (
        CLOUD_RANK.get(x["cloud"], 9), x["profile"], x["region"],
        _ip_key(x) if x.get("cloud") in LOCAL_NET_CLOUDS else (0, 0, 0, 0),
        x["name"],
    ))
    data["instances"] = all_insts
    data.setdefault("totals", {})["instances"] = len(data["instances"])
    # SSH config 的「本地」段是两域合并的产物：本域用刚扫到的，另一域沿用记录，别互相抹掉
    ssh_src = [i for i in all_insts if i.get("cloud") in LOCAL_NET_CLOUDS]
    write_local_ssh_config([i for i in ssh_src
                            if i.get("ssh_alias") and i.get("private_ip") and i.get("ssh_user")])
    # 不动 generated_at：那代表云快照的新鲜度，本地刷新没有重扫云，不应让它显得更新。
    _save_json(NOC_DATA, data)
    if not quiet:
        scope = f"{label}机器" if net_ok else f"{label}机器(沿用未重扫)"
        other = "--tailscale" if network == netprobe.NET_LAN else "--local"
        print(f"✅ {scope} {len(scoped)} 台已刷新，未触云账号"
              f"（另一网络域用 `ops sync {other}`，剧本/日志索引用 `ops sync --index`）。"
              f"\n已更新：{NOC_DATA}")
    return 0


def refresh_local(quiet=False):
    """`ops sync --local` 的入口：只刷**内网**（lan）机器。Tailscale 域走 `--tailscale`。"""
    return refresh_net(netprobe.NET_LAN, quiet=quiet)


def refresh_tailscale(quiet=False):
    """`ops sync --tailscale` 的入口：只刷挂在 **Tailscale 组网**上的机器。"""
    return refresh_net(netprobe.NET_TAILSCALE, quiet=quiet)


# --------------------------------------------------------------------------
# 按资源类型单刷（看板「按 tab 单刷」用：只扫该类，不全量重扫，省时）
# --------------------------------------------------------------------------
def _render_ssh_config(records, extra):
    """records（AWS）+ extra（非 AWS 密钥登录实例）→ ~/.ssh/config 托管段正文。"""
    groups = {}
    for r in records:
        if r["mode"] == "ssm":          # 无 SSH 通道，写进去只会是 HostName None 的死条目
            continue
        s = r["scope"]
        label = f"{s['flag']} {s['region_display']} ({s['profile']})"
        groups.setdefault(label, []).append(r)
    parts = []
    for label, rs in groups.items():
        parts.append(f"# --- {label} ---")
        # 直连的（含跳板机本身）排在前，保证 ProxyJump 目标已定义；同类再按别名排，让输出**确定**——
        # EC2 API 的返回顺序并不保证稳定，不定序则两次 sync 之间 ssh config 会无谓抖动；
        # `--account=` 单刷时沿用行也只能按别名还原，定序后两条路径产出一致。
        for r in sorted(rs, key=lambda x: (x["proxyjump"] is not None, x["alias"] or "")):
            u = r["user"] or "ec2-user"
            kf = r["keyfile"] or "~/.ssh/UNKNOWN.pem"
            parts.append(ssh_block(r["alias"], r["hostname"], u, kf, r["proxyjump"]))
    # 其它云中**密钥登录**的（有 key_file 才进 ssh config；密码登录的走 agent PASSWORD_HOSTS）
    keyed = [t for t in extra if t.get("ssh_alias") and t.get("key_file") and t.get("public_ip")]
    if keyed:
        parts.append("# --- 其它云（密钥登录）---")
        for t in sorted(keyed, key=lambda x: x["ssh_alias"]):
            parts.append(ssh_block(t["ssh_alias"], t["public_ip"], t["ssh_user"], t["key_file"], None))
    return "\n\n".join(parts)


def _collect_kind(kind, clouds=None):
    """采集单一类型资源（只触该类的 provider 接口），返回行列表。键与 data.json 资源键一致。

    全量 `ops sync` 与「按 tab 单刷」共用这一份，保证两条路径不漂移。instances 不在此
    （需 SSH config 重写，走 _refresh_instances_into）；ops_log 的 backfill 由调用方负责。

    clouds：只问这几朵云的 provider（`ops sync --account=` 传被点名账号所属的那朵云，免得为刷
    一个 AWS 账号白扫阿里/腾讯/Cloudflare 等）。None = 全部 provider。
    """
    provs = list(_providers(clouds).values())
    if kind == "domains":
        rows = [d for p in provs for d in _provider_rows(p, "domains")]
        # 已放弃的域名在此统一打标（一处覆盖所有注册商）：abandoned 为放弃原因字符串，
        # 未登记则为 ""。看板据此标「已放弃」而非临期告警，见 deploy_config.abandoned_domains。
        gone = deploy_config.abandoned_domains()
        for d in rows:
            d["abandoned"] = gone.get(str(d.get("domain") or "").strip().lower(), "")
        return sorted(
            rows,
            key=lambda d: (REGISTRAR_RANK.get(d["registrar"], 9),
                           d["days_left"] if d["days_left"] is not None else 99999),
        )
    if kind == "databases":
        rows = [d for p in provs for d in _provider_rows(p, "databases")]
        notes = deploy_config.database_notes()   # 人工维护的用途/授权备忘（配置里，跨 sync 存活）
        for d in rows:                           # 配置里写了用途以配置为准；没写就留 provider 自己给的摘要（如 Supabase 的 PITR/备份/网络）
            d["note"] = notes.get(str(d.get("name") or ""), "") or d.get("note", "")
        return rows
    if kind == "storage":
        return [s for p in provs for s in _provider_rows(p, "storage")]
    if kind == "serverless":
        return [f for p in provs for f in _provider_rows(p, "serverless")]
    if kind == "cdn":
        return [c for p in provs for c in _provider_rows(p, "cdn")]           # 含 OCI Media Streams CDN
    if kind == "iam":
        return [u for p in provs for u in _provider_rows(p, "iam")]
    if kind == "apikeys":
        return [k for p in provs for k in _provider_rows(p, "apikeys")]     # AI 密钥元数据（GCP Gemini/Vertex key、AWS Bedrock key）
    if kind == "messaging":
        return [m for p in provs for m in _provider_rows(p, "messaging")]   # SQS/SNS/SES/EventBridge
    if kind == "billing":
        return [b for p in provs for b in _provider_rows(p, "billing")]     # AWS Cost Explorer（每请求计费）
    if kind == "credits":
        # 账号级账务/额度状态（信用余额·免费额度·超额保护·预算），与 billing 的花费流水是两回事。
        # 各云内部已自排，这里按云序再排一次，保证跨云混排的顺序稳定。
        return sorted([c for p in provs for c in _provider_rows(p, "credits")],
                      key=lambda c: (CLOUD_RANK.get(c.get("cloud"), 99), c.get("account") or ""))
    if kind == "scheduled":
        return schedule_ops.list_all()           # 计划任务（EventBridge Scheduler 的 noc- 调度）
    if kind == "runbooks":
        return scan_runbooks()
    if kind == "ops_log":
        return ops_log.tail()
    raise KeyError(kind)


def _billing_with_cache(prev_data, max_age_hours=12):
    """全量 sync 用：成本数据每天最多更新几次、且 Cost Explorer 每请求计费 $0.01，
    故若上次拉取距今 < max_age_hours 就沿用旧行，不重复调 CE。看板单刷 / `ops sync --kind=billing`
    走 _collect_kind 直拉，不受此缓存约束（强制刷新）。"""
    prev = (prev_data or {}).get("billing") or []
    stamps = [b.get("fetched_at") for b in prev if b.get("fetched_at")]
    if stamps:
        try:
            newest = max(datetime.fromisoformat(s) for s in stamps)
            age_h = (datetime.now(newest.tzinfo) - newest).total_seconds() / 3600
            if age_h < max_age_hours:
                print(f"  💰 成本 {len(prev)} 行（缓存 {age_h:.0f}h 内，未重拉 CE；强制刷新用 `ops sync --kind=billing`）")
                return prev
        except ValueError:
            pass
    rows = _collect_kind("billing")
    print(f"  💰 成本 {len(rows)} 行（Cost Explorer 实拉）")
    return rows


def _records_from_rows(rows):
    """data.json 里的 AWS 实例行 → 渲染用 record（_instances_payload 的逆向）。

    `--account=` 单账号刷新时其它 AWS 账号本轮不重扫，但 ~/.ssh/config 的托管段是**整段重写**的：
    必须把沿用的实例也还原成 record 一起渲染，否则别的账号的 Host 会被整段抹掉。
    """
    out = []
    for i in rows:
        direct = i.get("connect") == "direct" and i.get("public_ip")
        out.append({
            "_retained_row": i,
            "scope": {"profile": i["profile"], "region": i["region"],
                      "region_display": i.get("region_display") or i["region"],
                      "flag": i.get("flag") or "", "jump_alias": None, "jump_instance": None},
            "inst": {k: i.get(k) for k in
                     ("id", "name", "type", "state", "az", "vpc", "private_ip", "public_ip", "key_name")},
            "alias": i.get("ssh_alias"), "keyfile": i.get("key_file"),
            "mode": i.get("connect"), "hostname": i["public_ip"] if direct else i.get("private_ip"),
            "proxyjump": i.get("proxyjump"), "user": i.get("ssh_user"),
            "verified": i.get("verified", False),
        })
    return out


def _sort_records_like_config(records):
    """按 ops-config.toml 的账号×区域顺序原地重排 record，产出与全量 sync 同序（稳定排序，组内不动）。

    从 data.json 还原的行是按 IP 排的，直接渲染会把 ssh config 的分组顺序搅乱——纯排序抖动却让
    diff 满屏。`--account=` 单刷与 `--probe=<实例>` 单探都走这里，三条路径产出一致。
    """
    acct_ix = {a["profile"]: n for n, a in enumerate(deploy_config.all_aws_accounts())}
    reg_ix = {(a["profile"], r): n for a in deploy_config.all_aws_accounts()
              for n, r in enumerate(regions_for(a))}
    records.sort(key=lambda r: (acct_ix.get(r["scope"]["profile"], 99),
                                reg_ix.get((r["scope"]["profile"], r["scope"]["region"]), 99)))


def _refresh_instances_into(data, only=None):
    """重扫 AWS 账号×区域 + 非 AWS 实例，用**缓存**用户名重写 SSH config（不做探测），
    把同构实例列表写回 data['instances'] 并返回。

    与全量 sync 的区别（为了「快」）：不探测未知用户名、不更新快照、不重写 inventory.md。
    公网 IP 变化（停/启机）会经 SSH config 重写跟上；登录用户名沿用上次全量探明的缓存。

    only=(cloud, 账号)（`--account=`）：deploy_config 已把**该云**收窄到这一个账号（AWS 见
    build_scopes，其余云见各 provider 的 _accounts()），此处额外把**其余账号 + 其余所有云**
    从 data.json 原样沿用（本轮完全不扫），再一起渲染。只刷非 AWS 账号时连 AWS API 都不问。
    """
    user_cache = _load_json(USER_CACHE_FILE, {})
    prev = data.get("instances") or []
    only_cloud, only_acct = only or (None, None)
    records = []
    if only_cloud in (None, "aws"):        # 单刷别的云时 AWS 一台都不重扫，全部从 prev 还原
        for scope, insts in build_scopes():
            for inst in insts:
                if "_retained_row" in inst:
                    records.extend(_records_from_rows([inst["_retained_row"]]))
                    continue
                cached = user_cache.get(inst["id"], {})
                mode, hostname, pj = connection_of(scope, inst)
                records.append({
                    "scope": scope, "inst": inst, "alias": make_alias(scope, inst),
                    "keyfile": resolve_keyfile(scope["profile"], inst["key_name"]),
                    "mode": mode, "hostname": hostname, "proxyjump": pj,
                    "user": cached.get("user"), "verified": cached.get("verified", False),
                })
    if only_cloud:
        records += _records_from_rows([i for i in prev if i.get("cloud") == "aws"
                                       and not (only_cloud == "aws" and i.get("profile") == only_acct)])
        # 沿用的账号是**追加**在新扫账号之后的，直接渲染会把 ssh config 的分组顺序搅乱（纯排序抖动，
        # 却让 diff 满屏）。按 ops-config.toml 的账号×区域顺序重排，产出与全量 sync 同序（排序稳定，
        # 组内实例原顺序不动）。
        _sort_records_like_config(records)
        # 非 AWS 侧：只有被点名的那朵云重扫（provider 已收窄到该账号），同云的其它账号 + 其余云沿用
        extra = [i for i in prev if i.get("cloud") != "aws"
                 and not (i.get("cloud") == only_cloud and i.get("profile") == only_acct)]
        if only_cloud != "aws":
            extra += [r for p in _providers([only_cloud]).values() for r in _provider_rows(p, "instances")]
    else:
        # 非公网机器（local/sangfor/vmware/synology）不在此扫描——它们要内网/VPN 或 Tailscale，
        # 连不上时不该被清掉（与全量 sync 一致）；沿用 data.json 里上次的记录，
        # 要刷新用 `ops sync --local`（内网）/ `ops sync --tailscale`（Tailscale）。
        extra = [r for c, p in _providers().items() if c != "aws" and c not in LOCAL_NET_CLOUDS
                 for r in _provider_rows(p, "instances")]
        extra += [i for i in prev if i.get("cloud") in LOCAL_NET_CLOUDS]
    write_ssh_config(_render_ssh_config(records, extra))
    write_local_ssh_config([i for i in extra if i.get("ssh_alias") and i.get("private_ip") and i.get("ssh_user")])
    data["instances"] = _instances_payload(records, extra)
    return data["instances"]


def resolve_instance_row(rows, target):
    """按实例 ID 或名字在 data.json 行里定位**一台**实例。返回 (row, candidates)。

    ID 精确命中优先，其次名字精确；都没有则按名字子串给候选——找不到或不唯一时 row=None，
    candidates 供调用方列出来让用户挑（绝不猜）。
    """
    from .resources import resolve
    row, candidates = resolve(target, rows)
    return row, [row] if row else candidates


def refresh_probe(target):
    """`ops sync --probe=<实例名|ID>`：只对这一台探登录用户名，回写缓存 + SSH config + data.json。

    全量 sync 的探测挂在整轮扫云之后（一两分钟，大头是重扫八朵云），`--account=` 快路径又刻意
    不探（只用缓存）——于是「这台机器用户名探错/没探到」没有秒级修法。这里零云 API：目标从
    data.json 定位，探测只对该别名发 `echo`（PROBE_USERS 逐个试），成功即写 ssh-users.json，
    再用 data.json 的行整段重渲染 ~/.ssh/config（托管段只能整段写）。不重写 inventory.md
    （全量产物，与 `--account=` 同样的取舍）。返回退出码。
    """
    paths.ensure_workspace()
    data = _load_json(NOC_DATA, None)
    if not data or not data.get("instances"):
        print("✗ 尚无 data.json，先跑一次完整 `ops sync`。", file=sys.stderr)
        return 2
    rows = data["instances"]
    inst, cands = resolve_instance_row(rows, target)
    if inst is None:
        if cands:
            print(f"✗ 「{target}」不唯一/未精确命中，候选（用 ID 或完整名字再来）：", file=sys.stderr)
            for c in cands[:10]:
                print(f"   {c.get('name')}  {c.get('id')}  {c.get('cloud')}/{c.get('region')}", file=sys.stderr)
        else:
            print(f"✗ 清单里没有「{target}」（按 ID 与名字都没找到；新机器先 `ops sync`）。", file=sys.stderr)
        return 2
    label = f"{inst.get('name')} ({inst.get('id')})"
    if inst.get("cloud") != "aws":
        print(f"✗ {label} 是 {inst.get('cloud')} 实例：登录用户名来自 ops-config.toml 的 ssh_user / "
              f"local-servers.toml，不走探测。", file=sys.stderr)
        return 2
    if inst.get("connect") == "ssm" or not inst.get("ssh_alias"):
        print(f"✗ {label} 仅 SSM 可达（无密钥对/无公网），没有 SSH 用户名可探：\n"
              f"   aws ssm start-session --target {inst.get('id')} --profile {inst.get('profile')} "
              f"--region {inst.get('region')}", file=sys.stderr)
        return 2
    if inst.get("state") != "running":
        print(f"✗ {label} 未开机（{inst.get('state')}），探不了；开机后再来。", file=sys.stderr)
        return 2
    started = time.perf_counter()
    alias = inst["ssh_alias"]
    print(f"· 探测 {label} 的登录用户名：别名 {alias}，依次试 {' → '.join(PROBE_USERS)} ...")
    u = probe_user(alias)
    if not u:
        print(f"  ✗ {len(PROBE_USERS)} 种用户名都连不上（保持 {inst.get('ssh_user') or '未知'}）。"
              f"常见原因：安全组未放行本机出口、私钥不对（{inst.get('key_file') or '未知'}）、"
              f"AMI 用了别的登录名（可先手工 `ssh -i <pem> <用户>@{inst.get('public_ip') or inst.get('private_ip')}` 验证）。",
              file=sys.stderr)
        return 1
    user_cache = _load_json(USER_CACHE_FILE, {})
    user_cache[inst["id"]] = {"user": u, "verified": True}
    _save_json(USER_CACHE_FILE, user_cache)
    inst["ssh_user"], inst["verified"] = u, True
    records = _records_from_rows([i for i in rows if i.get("cloud") == "aws"])
    _sort_records_like_config(records)
    extra = [i for i in rows if i.get("cloud") != "aws"]
    write_ssh_config(_render_ssh_config(records, extra))
    write_local_ssh_config([i for i in extra if i.get("ssh_alias") and i.get("private_ip") and i.get("ssh_user")])
    _save_json(NOC_DATA, data)
    print(f"  ✓ {alias}: {u}")
    print(f"✅ 已写入用户名缓存并重写 ~/.ssh/config 托管段 + data.json，用时 {time.perf_counter() - started:.0f}s"
          f"（零云 API；`ssh {alias}` 可直接登）。")
    print("   inventory.md 是全量产物，本次未重写——需要它更新请跑完整 `ops sync`。")
    return 0


# 看板支持「按 tab 单刷」的资源键（其余 tab：ops_config 纯读 toml、走前端；不在此）
SINGLE_REFRESH_KINDS = ("instances", "domains", "databases", "storage", "serverless",
                        "cdn", "iam", "apikeys", "messaging", "billing", "credits", "scheduled",
                        "runbooks", "ops_log")


def _update_instance_totals(data):
    """实例是云快照：重算 totals 并把 generated_at 推到现在（体现新鲜度）。"""
    insts = data["instances"]
    data.setdefault("totals", {}).update({
        "instances": len(insts),
        "accounts": len({i["profile"] for i in insts}),
        "regions": len({i["region"] for i in insts}),
        "running": sum(1 for i in insts if i["state"] == "running"),
    })
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")


@_collection_session
def refresh_kind(kind):
    """只刷新某一类资源并 patch 进现有 data.json，不重扫其它类型（看板「按 tab 单刷」）。

    云类资源各只调对应 provider 接口；instances 复用全量同步的实例扫描（含 SSH config
    重写）但跳过其它资源；runbooks/ops_log 为纯本地，秒级。需先有一份 data.json。返回退出码。
    """
    paths.ensure_workspace()
    if kind not in SINGLE_REFRESH_KINDS:
        print(f"✗ 不支持单刷的资源类型：{kind}（可用：{', '.join(SINGLE_REFRESH_KINDS)}）", file=sys.stderr)
        return 2
    data = _load_json(NOC_DATA, None)
    if not data:
        print("✗ 尚无 data.json，先跑一次完整 `ops sync`。", file=sys.stderr)
        return 2
    started = time.perf_counter()
    if kind == "ops_log":
        ops_log.backfill_accounts()              # 历史日志补 account（幂等），与全量一致
    if kind == "instances":
        rows = _refresh_instances_into(data)
        _update_instance_totals(data)
    else:
        rows = _collect_kind(kind)
        data[kind] = rows
        data.setdefault("totals", {})[kind] = len(rows)
    _save_json(NOC_DATA, data)
    outcome = "⚠️ 部分采集失败，已保留旧数据；" if collection.current().failures() else "✅ "
    print(f"{outcome}「{kind}」共 {len(rows)} 条，用时 {time.perf_counter() - started:.0f}s（未重扫其它类型）。")
    if kind == "credits":
        # credits 没有看板 tab，唯一的人读出口是 inventory.md 的「云账务 / 额度」节；而 inventory.md
        # 是全量产物，单刷不重写——不提示的话用户会去读一份旧表。
        print("   最新值在 data.json 的 `credits`；inventory.md 的「云账务 / 额度」节要完整 `ops sync` 才会重写。")
    return 0


# --------------------------------------------------------------------------
# 按云账号单刷（`--account=<账号>`，可与 `--kind=` 组合）
# --------------------------------------------------------------------------
# 支持按账号收窄的云 → 能按账号刷的资源键（= 该云 provider 真会产出、且行里带账号维度的）。
# 加一朵云到这里的前提：它的账号来自 `[[<cloud>.accounts]]`（走 deploy_config.cloud_accounts，
# 才会跟随 set_account_scope 收窄），且资源行里带 account/profile 列。
# 不在此：scheduled（EventBridge 调度，账号来自 [scheduler] 配置，与 aws.accounts 不同维度）、
#         runbooks / ops_log（纯本地，无账号维度）。
ACCOUNT_SCOPED_KINDS = {
    "aws": ("instances", "domains", "databases", "storage", "serverless",
            "cdn", "iam", "apikeys", "messaging", "billing", "credits"),
    "aliyun": ("instances", "storage", "domains", "databases", "serverless", "cdn", "iam", "billing", "credits"),   # AliyunProvider 产出的九类（BssOpenApi 账单不计费）
    "tencent": ("instances", "domains", "databases", "storage", "serverless", "cdn", "iam", "billing", "credits"),   # TencentProvider 产出的九类（storage 走 coscli）
    "volcengine": ("instances", "domains", "databases", "storage", "serverless", "cdn", "iam", "billing", "credits"),   # VolcengineProvider 产出的九类（storage 走 tosutil）
    "oracle": ("instances", "domains", "databases", "storage", "serverless", "cdn", "iam", "billing"),
}
# 不带 --kind 时默认刷的类型：**排除 billing**——Cost Explorer 每请求计费（$0.01）；AWS 的 credits 也含一次 CE 调用，同样排除，
# 不该被一句 `--account=X` 顺手带上；要刷成本请显式 `--account=X --kind=billing`。
ACCOUNT_DEFAULT_KINDS = {c: tuple(k for k in ks if k != "billing" and not (c == "aws" and k == "credits"))
                         for c, ks in ACCOUNT_SCOPED_KINDS.items()}


def _row_account(kind, row):
    """一行资源所属账号（instances 里这一列叫 profile，其余资源叫 account）。"""
    return row.get("profile") if kind == "instances" else row.get("account")


def _row_cloud(kind, row):
    """一行资源所属云（domains 多注册商混排、行里没有 cloud 键，用 registrar 反查）。"""
    return REGISTRAR_CLOUD.get(row.get("registrar"), "") if kind == "domains" else row.get("cloud")


def resolve_account(name):
    """`--account=<名>` → (cloud, 该账号在资源行里的标识)。认不出返回 (None, None)。

    AWS 账号名就是 profile（= 账号 ID）；非 AWS 云既认 CLI profile 名（阿里云的 <profile>）
    也认账号 ID（1234567890123456）——但行里存的一律是账号 ID，故统一还原成账号 ID 返回。
    """
    for a in deploy_config.all_aws_accounts():
        if name == a["profile"]:
            return "aws", a["profile"]
    for cloud in ACCOUNT_SCOPED_KINDS:
        if cloud == "aws":
            continue
        for a in deploy_config.all_cloud_accounts(cloud):
            if name in (a.get("profile"), a.get("account")):
                return cloud, (a.get("account") or a.get("profile"))
    return None, None


def _known_accounts_hint():
    """报错时列出 ops-config.toml 里所有可用于 `--account=` 的账号，按云分组。"""
    parts = []
    for cloud in ACCOUNT_SCOPED_KINDS:
        if cloud == "aws":
            names = [a["profile"] for a in deploy_config.all_aws_accounts()]
        else:
            names = [a.get("profile") or a.get("account")
                     for a in deploy_config.all_cloud_accounts(cloud)]
        if names:
            parts.append(f"{PROVIDERS[cloud].display} {'/'.join(names)}")
    return "；".join(parts)


def _sort_rows(kind, rows):
    """合并（本轮新扫的单账号行 + 沿用的其它账号/其它云行）后重排，还原全量 sync 的展示顺序。"""
    if kind == "domains":
        return sorted(rows, key=lambda d: (REGISTRAR_RANK.get(d["registrar"], 9),
                                           d["days_left"] if d["days_left"] is not None else 99999))
    if kind == "messaging":
        order = {"SQS": 0, "SNS": 1, "SES": 2, "EventBridge": 3}
        return sorted(rows, key=lambda x: (order.get(x["kind"], 9), x.get("account") or "",
                                           x.get("region") or "", x.get("name") or ""))
    if kind == "billing":
        return sorted(rows, key=lambda x: (x.get("account") or "", x.get("month") or "",
                                           -(x.get("amount") or 0)))
    return sorted(rows, key=lambda x: (CLOUD_RANK.get(x.get("cloud"), 9), x.get("account") or "",
                                       x.get("region") or "", x.get("name") or ""))


@_collection_session
def refresh_account(name, kind=None):
    """只重扫某个云账号的资源，patch 进 data.json（其它账号 / 其它云的行原样沿用）。

    支持哪些云见 ACCOUNT_SCOPED_KINDS：AWS 用账号 ID，阿里云用 CLI profile 名或账号 ID。

    收窄发生在 deploy_config.set_account_scope 一处——被点名那朵云的所有采集路径（AWS 的
    build_scopes 与 providers/aws.py 的 list_*，其余云各 provider 的 _accounts()）都从
    aws_accounts()/route53_profiles()/cloud_accounts() 取账号，故设一次即全链路生效；
    其余云的 provider 本轮压根不问（_collect_kind(clouds={cloud})）。

    合并语义：每个资源键 = 本轮新扫的该账号行 + data.json 里**非**该账号的行（同云其它账号、
    其它云）。故不会像全量 sync 那样整份覆盖。**inventory.md 与实例快照是全量产物，本路径不重写**
    （与「按 tab 单刷」一致）——要它们更新请跑完整 `ops sync`。需先有一份 data.json。返回退出码。
    """
    paths.ensure_workspace()
    cloud, acct = resolve_account(name)
    if not cloud:
        print(f"✗ 未知账号：{name}（ops-config.toml 配置的是：{_known_accounts_hint()}）", file=sys.stderr)
        return 2
    scoped_kinds = ACCOUNT_SCOPED_KINDS[cloud]
    display = PROVIDERS[cloud].display
    if kind and kind not in scoped_kinds:
        print(f"✗ 「{kind}」没有{display}账号维度，不能与 --account={name} 组合"
              f"（{display}可按账号刷的是：{', '.join(scoped_kinds)}；直接用 `ops sync --kind={kind}`）",
              file=sys.stderr)
        return 2
    data = _load_json(NOC_DATA, None)
    if not data:
        print("✗ 尚无 data.json，先跑一次完整 `ops sync` 再用 --account。", file=sys.stderr)
        return 2

    kinds = (kind,) if kind else ACCOUNT_DEFAULT_KINDS[cloud]
    started = time.perf_counter()
    deploy_config.set_account_scope(cloud, name)      # ← 该云全链路只扫这一个账号
    print(f"· 只扫{display}账号 {name}（其它账号 / 其它云本轮不重扫，沿用 data.json）")
    try:
        for k in kinds:
            if k == "instances":
                _refresh_instances_into(data, only=(cloud, acct))
                _update_instance_totals(data)
                mine = [i for i in data["instances"]
                        if _row_cloud(k, i) == cloud and _row_account(k, i) == acct]
                kept = len(data["instances"]) - len(mine)
                print(f"  ✓ instances：本账号 {len(mine)} 台（沿用其它 {kept} 台；SSH config 已整段重写）")
                continue
            prev = data.get(k) or []
            kept = [r for r in prev if not (_row_cloud(k, r) == cloud and _row_account(k, r) == acct)]
            fresh = _collect_kind(k, clouds={cloud})
            data[k] = _sort_rows(k, kept + fresh)
            data.setdefault("totals", {})[k] = len(data[k])
            print(f"  ✓ {k}：本账号 {len(fresh)} 条（沿用其它 {len(kept)} 条）")
    finally:
        deploy_config.set_account_scope(cloud, None)

    _save_json(NOC_DATA, data)
    outcome = "⚠️ 部分采集失败，已保留旧数据；" if collection.current().failures() else "✅ "
    print(f"{outcome}{display}账号 {name} 的「{'/'.join(kinds)}」同步结束，用时 {time.perf_counter() - started:.0f}s。")
    if not kind and "billing" in scoped_kinds:
        print("   （成本未刷：Cost Explorer 每请求计费，需显式 "
              f"`ops sync --account={name} --kind=billing`）")
    print("   inventory.md / 实例快照是全量产物，本次未重写——需要它们更新请跑完整 `ops sync`。")
    return 0


SYNC_USAGE = """\
用法: ops sync [选项]

  ops sync                    全量同步：扫所有账号×区域的实例 + 只读资源，
                              对比快照报告 新增/变更/删除，重写 inventory 与 ~/.ssh/config
  ops sync --dry-run          只比对、只报告，不落盘（不写 inventory / config / 快照）
  ops sync --probe=all        重新探测所有实例的登录用户名（默认只探未知的）
  ops sync --probe=none       跳过登录用户名探测（不做任何 SSH 连接）
  ops sync --probe=<实例名|ID> 只探**这一台** AWS 实例的登录用户名并回写 SSH config / data.json
                              （零云 API，几秒；用户名探错/没探到时用它，别为此跑全量）
  ops sync --local            只重扫**内网**机器（VMware/Sangfor/内网群晖，需在内网，不触云）
                              别名: --no-cloud
  ops sync --tailscale        只重扫挂在 **Tailscale 组网**上的机器（人在哪都能刷，不触云）
                              别名: --ts
  ops sync --index            只把 runbooks/ 与操作日志重新索引进 data.json（零网络，秒级）
                              别名: --runbooks / --logs
  ops sync --kind=<键>        只刷一类资源（instances/domains/billing/credits/storage…），扫所有账号
  ops sync --account=<账号>   只刷一个云账号的资源；可加 --kind= 再收窄到一类
                              AWS 用账号 ID，阿里云用 profile 名（<profile>）或账号 ID
  ops sync -h | --help        显示本帮助
"""


@_collection_session
def main():
    if any(a in sys.argv for a in ("-h", "--help", "help")):
        print(SYNC_USAGE)
        raise SystemExit(0)
    paths.ensure_workspace()
    probe_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--probe=")), None)
    if probe_arg and probe_arg not in ("all", "none", "new"):   # --probe=<实例>：单机探用户名，不扫云
        raise SystemExit(refresh_probe(probe_arg))
    kind_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--kind=")), None)
    acct_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--account=")), None)
    if acct_arg:                          # 只刷一个云账号（可与 --kind 组合），patch 式合并写回
        raise SystemExit(refresh_account(acct_arg, kind_arg))
    if kind_arg:                          # 看板「按 tab 单刷」：只扫该类资源，不全量
        raise SystemExit(refresh_kind(kind_arg))
    if any(a in sys.argv for a in ("--index", "--runbooks", "--logs")):
        raise SystemExit(refresh_index())
    if any(a in sys.argv for a in ("--local", "--no-cloud")):
        raise SystemExit(refresh_local())
    if any(a in sys.argv for a in ("--tailscale", "--ts")):
        raise SystemExit(refresh_tailscale())
    started = time.perf_counter()
    probe_mode = "new"  # new=只探未知, all=全部重新探测, none=不探测
    if "--probe=all" in sys.argv:
        probe_mode = "all"
    elif "--probe=none" in sys.argv:
        probe_mode = "none"
    dry = "--dry-run" in sys.argv

    user_cache = _load_json(USER_CACHE_FILE, {})
    snapshot = _load_json(SNAPSHOT_FILE, {})

    # 1) 并发扫描所有 账号×区域
    print(f"· 扫描 {len(deploy_config.aws_accounts())} 个账号的实例（global {len(deploy_config.scan_regions('global'))} 区 + china）...")
    scoped = build_scopes()  # [(scope, [inst,...])]，仅含有实例的区域
    current = {}
    for scope, insts in scoped:
        for inst in insts:
            current[inst["id"]] = {**{k: inst.get(k) for k in WATCH_FIELDS},
                                   "profile": scope["profile"], "region": scope["region"], "cloud": "aws"}
        print(f"  {scope['flag']} {scope['region_display']} ({scope['profile']}): {len(insts)} 台")
    if not scoped:
        print("  （未发现任何实例）")

    # 多云：遍历非 AWS provider 拉实例（AWS 走上面的 build_scopes），并入 current 参与 diff
    extra_cloud = [r for c, p in _providers().items() if c != "aws" and c not in LOCAL_NET_CLOUDS
                   for r in _provider_rows(p, "instances")]
    for t in extra_cloud:
        current[t["id"]] = {**{k: t.get(k) for k in WATCH_FIELDS},
                            "profile": t["profile"], "region": t["region"], "cloud": t["cloud"]}
    for t in extra_cloud:
        print(f"  ☁️ {CLOUD_SECTION.get(t['cloud'], t['cloud'])} {t['region_display']} ({t['profile']})")

    # 非公网机器（local/sangfor/vmware/synology）要内网/VPN 或 Tailscale：全量刷新**完全不参与**——
    # 不扫描、不进 diff、不进快照，只把上次 data.json 里的记录原样沿用到渲染（执行前后一模一样，
    # 不报它们的新增/变更/删除）。要刷新用 `ops sync --local` / `ops sync --tailscale`。
    prev_data = _load_json(NOC_DATA, {})
    prev_local_net = [i for i in (prev_data.get("instances") or []) if i.get("cloud") in LOCAL_NET_CLOUDS]
    if prev_local_net:
        print(f"  ⏭  非公网机器（local/sangfor/vmware/synology）不参与全量刷新，"
              f"原样沿用 {len(prev_local_net)} 台"
              f"（内网的用 `ops sync --local`，Tailscale 的用 `ops sync --tailscale`）")
    extra = extra_cloud + prev_local_net   # 传给 write_inventory/write_noc_data 渲染（含本地网络机器）

    # 只读资源清单（域名/数据库/存储/函数/IAM/消息/计划任务 + 本地剧本/日志）。
    # 单类采集逻辑集中在 _collect_kind，看板「按 tab 单刷」复用同一份，避免两处漂移。
    ops_log.backfill_accounts()                 # 历史日志缺 account 的反查补入（幂等）
    resources = {k: _collect_kind(k) for k in
                 ("domains", "databases", "storage", "serverless", "cdn", "iam", "apikeys",
                  "messaging", "credits", "scheduled", "ops_log", "runbooks")}
    resources["billing"] = _billing_with_cache(prev_data)   # CE 计费，走 12h 缓存（单刷强制重拉）
    if resources["domains"]:
        print(f"  🌐 域名 {len(resources['domains'])} 个（多注册商：Route53 / 阿里云 / Cloudflare）")
    for label, key in [("🗄  数据库", "databases"), ("🪣 存储桶", "storage"),
                       ("ƛ 函数", "serverless"), ("🌐 CDN", "cdn"), ("🔑 IAM 用户", "iam"),
                       ("🔐 AI 密钥", "apikeys"), ("📨 消息/事件", "messaging"), ("💳 账务/额度", "credits"),
                       ("⏰ 计划任务", "scheduled"), ("📖 剧本", "runbooks")]:
        if resources[key]:
            print(f"  {label} {len(resources[key])} 个")

    # 2) 与快照对比（current 已只含云账号实例；旧快照可能残留本地网络机器，剔除后再比，
    #    保证本地网络机器不冒出"删除"——它们不参与全量刷新的 diff）
    local_net_ids = {i["id"] for i in prev_local_net}
    snap_cloud = {k: v for k, v in snapshot.items() if k not in local_net_ids}
    session = collection.current()
    if session:
        for failure in session.failures():
            if failure["kind"] != "instances":
                continue
            for iid, old in snap_cloud.items():
                old_cloud = old.get("cloud") or next((r.get("cloud") for r in prev_data.get("instances", [])
                                                     if r.get("id") == iid), None)
                if old_cloud not in (None, failure["cloud"]):
                    continue
                if old.get("profile") == failure["account"] and (
                        not failure["region"] or old.get("region") == failure["region"]):
                    current[iid] = old
    added = [i for i in current if i not in snap_cloud]
    removed = [i for i in snap_cloud if i not in current]
    changed = {}
    for i in current:
        if i in snap_cloud:
            diffs = {f: (snap_cloud[i].get(f), current[i].get(f))
                     for f in WATCH_FIELDS if snap_cloud[i].get(f) != current[i].get(f)}
            if diffs:
                changed[i] = diffs

    # 3) 解析每台的连接信息 + 用户名
    records = []   # 给 inventory/config 用
    unknown_key = []
    to_probe = []
    skip_probe = []   # (alias, 原因)：想探但探不了的，列出来而不是去撞超时
    for scope, insts in scoped:
        for inst in insts:
            if "_retained_row" in inst:
                records.extend(_records_from_rows([inst["_retained_row"]]))
                continue
            mode, hostname, pj = connection_of(scope, inst)
            keyfile = resolve_keyfile(scope["profile"], inst["key_name"])
            if keyfile is None and mode != "ssm":
                unknown_key.append((inst["name"], inst["key_name"]))
            alias = make_alias(scope, inst)
            cached = user_cache.get(inst["id"], {})
            user = cached.get("user")
            verified = cached.get("verified", False)
            need_probe = (probe_mode == "all") or (probe_mode == "new" and user is None)
            # 探测是真去 SSH：没开机的、无公网又无跳板的，连 3 个用户名只会各等 8s 超时，
            # 还刷出"连不上"的误导信息（重建换 ID 后的停机实例就是典型）。这类直接跳过，
            # 保持"未知"，等它开机 / 有路可达后下一次 sync 自然补探。
            # SSM 实例永远没有 SSH，不是"这轮探不了"，静默不探、也不进跳过清单（清单只列会变的状态）
            if mode == "ssm":
                need_probe = False
            if need_probe and inst["state"] != "running":
                skip_probe.append((alias, f"未开机（{inst['state']}）"))
                need_probe = False
            elif need_probe and not inst["public_ip"] and not scope["jump_alias"]:
                skip_probe.append((alias, "无公网 IP 且本区无跳板"))
                need_probe = False
            if need_probe and not dry:
                to_probe.append(inst["id"])
            records.append({
                "scope": scope, "inst": inst, "alias": alias, "keyfile": keyfile,
                "mode": mode, "hostname": hostname, "proxyjump": pj,
                "user": user, "verified": verified,
            })

    # 4) 先写一版 config（用缓存或默认 ec2-user 占位），让探测能用上 ProxyJump
    local_extra = [i for i in extra if i.get("ssh_alias") and i.get("private_ip") and i.get("ssh_user")]
    if not dry:
        write_ssh_config(_render_ssh_config(records, extra))
        write_local_ssh_config(local_extra)

    # 5) 探测未知用户名
    newly = {}
    if skip_probe and not dry:
        print(f"· 跳过探测 {len(skip_probe)} 台（保持 未知，开机/可达后下次 sync 再探）：")
        for alias, why in skip_probe:
            print(f"  ⏭  {alias}: {why}")
    if to_probe and not dry:
        print(f"· 探测登录用户名（{len(to_probe)} 台）...")
        for r in records:
            if r["inst"]["id"] in to_probe:
                u = probe_user(r["alias"])
                if u:
                    r["user"], r["verified"] = u, True
                    user_cache[r["inst"]["id"]] = {"user": u, "verified": True}
                    newly[r["alias"]] = u
                    print(f"  ✓ {r['alias']}: {u}")
                else:
                    r["verified"] = False
                    print(f"  ✗ {r['alias']}: {len(PROBE_USERS)} 种用户名都连不上（保持 {r['user'] or '未知'}）")
        write_ssh_config(_render_ssh_config(records, extra))  # 用最终用户名重写

    # 6) 重新生成 inventory + NOC 看板数据
    if not dry:
        write_inventory(scoped, records, extra, resources)
        write_noc_data(scoped, records, extra, resources)

    # 7) 保存快照与缓存（current 只含云账号实例 → 快照从此只跟踪云账号，本地网络机器不入快照）
    if not dry:
        _save_json(SNAPSHOT_FILE, current)
        _save_json(USER_CACHE_FILE, user_cache)

    # 8) 报告
    print("\n===== 同步报告 =====")
    if not (added or removed or changed):
        if collection.current() and collection.current().failures():
            print("· 已完成采集的范围未发现实例变更；失败范围沿用旧数据。")
        else:
            print("✅ 无变更（实例清单与上次一致）")
    if added:
        print(f"🆕 新增 {len(added)} 台：")
        for i in added:
            print(f"   + {current[i]['name']} ({i}) {current[i]['region']}")
    if removed:
        print(f"➖ 删除 {len(removed)} 台：")
        for i in removed:
            print(f"   - {snapshot[i]['name']} ({i})")
    if changed:
        print(f"✏️  变更 {len(changed)} 台：")
        for i, diffs in changed.items():
            for f, (old, new) in diffs.items():
                print(f"   ~ {current[i]['name']} {f}: {old} → {new}")
    if newly:
        print(f"🔑 新探明用户名 {len(newly)} 台：" + ", ".join(f"{a}={u}" for a, u in newly.items()))
    if unknown_key:
        print("⚠️  以下实例找不到私钥（约定 ~/.ssh/<profile>/<KeyName>.pem），请放置或在 KEYFILE_OVERRIDE 补充：")
        for name, kn in unknown_key:
            print(f"   {name}: KeyName={kn}")
    if dry:
        print("(--dry-run：未写入任何文件)")
    print(f"⏱  全量同步耗时 {time.perf_counter() - started:.0f} 秒（多云全量扫描，属正常；只登记剧本用 `ops sync --index`）")
    print(f"\n已更新：{INVENTORY}  和  {SSH_CONFIG}（marker 段）" if not dry else "")


# --------------------------------------------------------------------------
# NOC 看板数据生成（前端读取）
# --------------------------------------------------------------------------
def _instances_payload(records, extra):
    """records（AWS）+ extra（非 AWS 实例，已是同构 dict）→ data.json 用的实例列表（已排序）。"""
    instances = []
    for r in records:
        if "_retained_row" in r:
            instances.append(dict(r["_retained_row"]))
            continue
        s, inst = r["scope"], r["inst"]
        instances.append({
            "name": inst["name"], "id": inst["id"], "profile": s["profile"],
            "region": s["region"], "region_display": s["region_display"], "flag": s["flag"],
            "type": inst["type"], "state": inst["state"], "az": inst["az"],
            "private_ip": inst["private_ip"], "public_ip": inst["public_ip"],
            "key_name": inst["key_name"], "vpc": inst.get("vpc"),
            "ssh_alias": None if r["mode"] == "ssm" else r["alias"], "ssh_user": r["user"],
            "key_file": (r["keyfile"] or "").replace(str(Path.home()), "~"),
            "connect": r["mode"], "proxyjump": r["proxyjump"], "verified": r["verified"],
            "cloud": "aws",
        })
    instances.extend(extra)  # 腾讯云/阿里云实例已是同构 dict
    notes = deploy_config.instance_notes()   # 人工维护的用途备注（配置里，跨 sync 存活）
    for i in instances:
        i["note"] = notes.get(i["id"], "")

    def _ip_key(x):
        try:
            return tuple(int(p) for p in (x.get("private_ip") or "").split("."))
        except Exception:
            return (0, 0, 0, 0)

    instances.sort(key=lambda x: (
        CLOUD_RANK.get(x["cloud"], 9), x["profile"], x["region"],
        _ip_key(x) if x.get("cloud") == "local" else (0, 0, 0, 0),
        x["name"],
    ))
    return instances


def _prev_local_network():
    """取上一次 `ops netprobe` 的本机出口快照。

    全量 sync **不重探**它（那是本机状态、几秒就能单独刷，没必要拖慢两分钟的云扫描），
    只负责原样带过去并渲染——所以这个键必须显式保留：write_noc_data 是整份重建 data.json 的，
    不显式带就会被每次 sync 抹掉。快照自带 probed_at，过期与否读的人一眼可见。
    """
    return (_load_json(NOC_DATA, {}) or {}).get("local_network") or {}


def write_noc_data(scoped, records, extra, resources):
    instances = _instances_payload(records, extra)
    local_network = _prev_local_network()
    totals = {
        "instances": len(instances),
        "accounts": len({i["profile"] for i in instances}),
        "regions": len({i["region"] for i in instances}),
        "running": sum(1 for i in instances if i["state"] == "running"),
    }
    totals.update({k: len(v) for k, v in resources.items()})
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": totals,
        "instances": instances,
        **resources,   # domains/databases/storage/serverless/iam 各自成键
    }
    if local_network:                  # 本机出口快照由 `ops netprobe` 产出，此处只带过、不重探
        data["local_network"] = local_network
    NOC_DATA.parent.mkdir(parents=True, exist_ok=True)
    _save_json(NOC_DATA, data)


# --------------------------------------------------------------------------
# inventory 生成
# --------------------------------------------------------------------------
def write_inventory(scoped, records, extra, resources):
    domains = resources.get("domains", [])
    databases = resources.get("databases", [])
    storage = resources.get("storage", [])
    serverless = resources.get("serverless", [])
    iam = resources.get("iam", [])
    apikeys = resources.get("apikeys", [])
    credits = resources.get("credits", [])
    rec_by_id = {r["inst"]["id"]: r for r in records}
    notes = deploy_config.instance_notes()   # 实例 ID → 用途；某区/某云一台都没写就不加「用途」列
    today = date.today().isoformat()

    def _table(cols, rows):
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))
        for r in rows:
            L.append("| " + " | ".join(r) + " |")
    L = []
    L.append("# 云实例知识库（AWS + 腾讯云 + 阿里云）\n")
    L.append(f"> 更新时间：{today}（由 `ops sync` 生成）")
    L.append("> 连接信息见末尾「SSH 连接」节；私钥仅存于 `~/.ssh`，密码存 Keychain，均不在本文档。\n")
    if collection.current() and collection.current().failures():
        L.append("> ⚠️ 本轮部分采集失败，相关资源沿用旧数据；各范围上次成功时间见 data.json 的 collection_status。\n")
    total = sum(len(insts) for _, insts in scoped) + len(extra)

    for scope, insts in scoped:
        L.append("\n---\n")
        L.append(f"## {scope['flag']} {scope['region_display']} ｜ 账号 {scope['profile']}"
                 f"（{scope['region']}）— {len(insts)} 台\n")
        noted = any(notes.get(i["id"]) for i in insts)
        _table(["Name", "类型", "实例 ID", "私网 IP", "公网 IP", "状态"] + (["用途"] if noted else []),
               [[inst["name"], inst["type"], inst["id"], inst["private_ip"] or "—",
                 inst["public_ip"] or "—", inst["state"]] + ([notes.get(inst["id"]) or "—"] if noted else [])
                for inst in sorted(insts, key=lambda x: x["name"])])

    # 其它云（腾讯/阿里…），按 (cloud, account) 分组
    cloud_groups = {}
    for t in extra:
        cloud_groups.setdefault((t["cloud"], t["profile"]), []).append(t)
    for (cloud, profile), insts in cloud_groups.items():
        flag = insts[0]["flag"]
        label = CLOUD_SECTION.get(cloud, cloud)
        L.append("\n---\n")
        L.append(f"## {flag} {label} ｜ 账号 {profile} — {len(insts)} 台\n")
        noted = any(notes.get(i["id"]) for i in insts)
        _table(["Name", "类型", "实例 ID", "内网 IP", "公网 IP", "区域", "状态"] + (["用途"] if noted else []),
               [[t["name"], t["type"], t["id"], t["private_ip"] or "—", t["public_ip"] or "—",
                 t["region_display"], t["state"]] + ([notes.get(t["id"]) or "—"] if noted else [])
                for t in sorted(insts, key=lambda x: x["name"])])

    # 域名（多注册商：阿里云 + AWS Route53）
    if domains:
        L.append("\n---\n")
        L.append(f"## 🌐 域名 — {len(domains)} 个（按临期排序）\n")
        L.append("| 域名 | 注册商 | 类型 | 账号 | 到期日期 | 剩余天数 | 备注 |")
        L.append("|---|---|---|---|---|---|---|")
        for d in domains:
            dl = d["days_left"]
            warn = " ⚠️" if (dl is not None and dl <= 30) else ""
            L.append(f"| {d['domain']} | {d['registrar']} | {d['type']} | {d['account']} | "
                     f"{d['expire_date'] or '—'} | {dl if dl is not None else '—'}{warn} | {d['note']} |")

    # 数据库（RDS 等）
    if databases:
        L.append("\n---\n")
        L.append(f"## 🗄 数据库 — {len(databases)} 个\n")
        noted = any(d.get("note") for d in databases)   # 一个都没写就不加「用途」列
        L.append("| 名称 | 云 | 账号 | 区域 | 引擎 | 规格 | 存储 | 状态 | 端点 |" + (" 用途 |" if noted else ""))
        L.append("|---|---|---|---|---|---|---|---|---|" + ("---|" if noted else ""))
        for d in databases:
            L.append(f"| {d['name']} | {d['cloud']} | {d['account']} | {d['region_display']} | "
                     f"{d['engine']} | {d['class']} | {d['storage']} | {d['state']} | {d['endpoint']} |"
                     + (f" {d.get('note') or '—'} |" if noted else ""))

    # 存储桶（S3 等）
    if storage:
        L.append("\n---\n")
        L.append(f"## 🪣 存储桶 — {len(storage)} 个\n")
        L.append("| 桶名 | 云 | 账号 | 区域 | 域名 | 创建日期 |")
        L.append("|---|---|---|---|---|---|")
        for s in storage:
            L.append(f"| {s['name']} | {s['cloud']} | {s['account']} | {s['region_display']} "
                     f"| {s.get('domain', '—')} | {s['created']} |")

    # 函数（Lambda 等）
    if serverless:
        L.append("\n---\n")
        L.append(f"## ƛ 函数 — {len(serverless)} 个\n")
        L.append("| 函数名 | 云 | 账号 | 区域 | 运行时 | 内存(MB) | 超时(s) | 代码(MB) | 最后修改 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for f in serverless:
            L.append(f"| {f['name']} | {f['cloud']} | {f['account']} | {f['region_display']} | "
                     f"{f['runtime']} | {f['memory']} | {f['timeout']} | {f['code_mb']} | {f['last_modified']} |")

    # IAM 用户（访问管理）
    if iam:
        L.append("\n---\n")
        L.append(f"## 🔑 IAM 用户 — {len(iam)} 个\n")
        L.append("| 用户 | 云 | 账号 | 创建 | 上次登录 | 密钥数 | 最老密钥(天) |")
        L.append("|---|---|---|---|---|---|---|")
        for u in iam:
            ok = u["oldest_key_days"]
            warn = " ⚠️" if (ok is not None and ok > 365) else ""
            kc = u["key_count"] if u["key_count"] is not None else "—"   # GCP 无密钥概念 → 显示 —（非 None）
            L.append(f"| {u['name']} | {u['cloud']} | {u['account']} | {u['created']} | "
                     f"{u['last_used']} | {kc} | {ok if ok is not None else '—'}{warn} |")

    # AI 密钥（Cloud API Keys 元数据：Gemini / Vertex AI key 盘点；只有元数据，没有密钥串）
    if apikeys:
        L.append("\n---\n")
        L.append(f"## 🔐 AI 密钥（API key）— {len(apikeys)} 个\n")
        L.append("| 名称 | 云 | 账号/项目 | 限定 API | 类型 | 状态 | 绑定身份 | 客户端限制 | 创建 | 过期 | Key ID |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for k in apikeys:
            purpose = "⚠️ 无限制" if k.get("unrestricted") else (k.get("purpose") or "—")
            L.append(f"| {k['name']} | {k['cloud']} | {k['account']} | {purpose} | {k.get('key_type') or '—'} | "
                     f"{k.get('status') or '—'} | {k.get('service_account') or '—'} | {k.get('client_restriction') or '无'} | "
                     f"{k.get('created') or '—'} | {k.get('expires') or '—'} | `{k.get('id')}` |")

    # 云账务 / 额度（信用余额 · 免费额度 · 超额保护 · 预算告警）
    if credits:
        L.append("\n---\n")
        L.append(f"## 💳 云账务 / 额度 — {len(credits)} 个账号\n")
        L.append("| 云 | 账号 | 名称 | 付费类型 | 状态 | 信用余额 | 免费额度 | 剩余天数 | 超额保护 | 预算告警 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in credits:
            bal = ("—" if c.get("credit") is None
                   else f"{c['credit']:.2f} {c.get('credit_currency') or ''}".strip())
            # 有到期日（Azure 12 个月免费）显示到期；只有名目没到期日（GCP Always Free）标「无到期」
            ft = (f"{c['free_tier']} · 至 {c['free_tier_end']}" if c.get("free_tier_end")
                  else f"{c['free_tier']} · 无到期" if c.get("free_tier") else "—")
            fd = c.get("free_tier_days")
            days = "—" if fd is None else f"{fd}{' ⚠️' if fd <= 30 else ''}"
            sl = {"On": "有（超额停服）", "Off": "无（超额扣款）"}.get(c.get("spending_limit"), "—")
            # 超额停服是唯一的**硬**兜底；没有它又没设预算告警 = 超额完全无感知，标出来
            budget = c.get("budget") or ("⚠️ 未设" if c.get("spending_limit") != "On" else "—")
            L.append(f"| {c['cloud']} | {c['account']} | {c.get('account_name') or '—'} | "
                     f"{c.get('plan') or '—'} | {c.get('state') or '—'} | {bal} | {ft} | "
                     f"{days} | {sl} | {budget} |")
        L.append("\n> 「信用余额」是**送的钱**（用完即止）；「免费额度」是**按品类的月度配额**"
                 "（如 Azure B1s 750h/月），额度内不计费、超出即按量扣款——两者不是一回事，"
                 "余额 0 不代表不能白嫖，额度在也不代表不会扣钱。")
        L.append("> 预算告警**只告警不断供**，不会在超额时停掉服务。花费流水（账号×服务×月）"
                 "不在本文件，看 NOC 看板「成本」tab 或 data.json 的 `billing`。")

    # 本机出口链路节（`ops netprobe` 的快照，本函数只渲染、不探测）
    from . import egress_probe
    L.extend(egress_probe.render_lines(_prev_local_network()))

    # SSH 连接节
    L.append("\n---\n")
    L.append("## SSH 连接\n")
    L.append("用 `~/.ssh/config` 别名直连（已配好 user/key/跳板），如 `ssh <别名>`。"
             "Agent 经 `ssh_exec` 以这些别名为 target。✅=已实测；推断=未探测到/连不上。"
             "连接列为 **SSM** 的实例设计上没有 SSH（无密钥对/无公网/SG 零入站），要进机器用 "
             "`aws ssm start-session --target <实例ID> --profile <账号> --region <区域>`。\n")
    for scope, insts in scoped:
        L.append(f"\n### {scope['flag']} {scope['region_display']} {scope['profile']}\n")
        L.append("| 实例 | SSH 别名 | 连接 | user | key 文件 | 验证 |")
        L.append("|---|---|---|---|---|---|")
        for inst in sorted(insts, key=lambda x: x["name"]):
            r = rec_by_id[inst["id"]]
            if r["mode"] == "ssm":
                L.append(f"| {inst['name']} | — | SSM（无密钥对） | — | — | — |")
                continue
            conn = "直连" if r["mode"] == "direct" else f"经 {r['proxyjump']}"
            keyfile = (r["keyfile"] or "⚠️未知").replace(str(Path.home()), "~")
            mark = "✅" if r["verified"] else "推断"
            L.append(f"| {inst['name']} | `{r['alias']}` | {conn} | {r['user'] or '?'} | "
                     f"{Path(keyfile).name if r['keyfile'] else '⚠️未知'} | {mark} |")

    # 非 AWS（其它云 + 本地网络机器）——别名同样由 `ops sync` 写进 ~/.ssh/config；
    # ⚠️ 这一段以前不渲染，备注却写着"见上表 SSH 连接"，导致 agent 按 inventory 解析不出别名（只有 data.json 有）。
    for (cloud, profile), insts in cloud_groups.items():
        ssh_insts = [t for t in sorted(insts, key=lambda x: x["name"]) if t.get("ssh_alias")]
        if not ssh_insts:
            continue
        flag = insts[0]["flag"]
        label = CLOUD_SECTION.get(cloud, cloud)
        gaps = len(insts) - len(ssh_insts)
        tail = f"（另有 {gaps} 台无 SSH 登录信息：Windows/RDP 或未在 local-servers.toml 登记）" if gaps else ""
        L.append(f"\n### {flag} {label} ｜ 账号 {profile}{tail}\n")
        if cloud in LOCAL_NET_CLOUDS:
            L.append("> 非公网机器：要先进内网/VPN 或 Tailscale 才连得上"
                     "（是哪一种看上方实例表的「区域」列）；连不上先确认网络，别当成机器故障。\n")
        L.append("| 实例 | SSH 别名 | 连接 | user | key 文件 | 验证 |")
        L.append("|---|---|---|---|---|---|")
        for t in ssh_insts:
            host = t.get("public_ip") or t.get("private_ip") or "?"
            port = t.get("ssh_port")
            target = f"{host}:{port}" if port and int(port) != 22 else host
            conn = f"经 {t['proxyjump']}" if t.get("proxyjump") else f"直连 {target}"
            kf = t.get("key_file")
            keyfile = Path(kf).name if kf else "默认密钥/口令"
            mark = "✅" if t.get("verified") else "推断"
            L.append(f"| {t['name']} | `{t['ssh_alias']}` | {conn} | {t.get('ssh_user') or '?'} | "
                     f"{keyfile} | {mark} |")

    L.append("\n---\n")
    L.append("## 备注\n")
    L.append("- 本文件由 `ops sync` 自动生成；新增实例后运行 `ops sync` 即可同步。")
    L.append("- 账号/区域/别名等部署配置在 `ops-config.toml` 维护；每个账号自动扫描其 partition 下的常用区域。")
    L.append("- 表里的「用途」列取自 `ops-config.toml` 的 `[instance_notes]`（键=实例 ID，改名不失效）"
             "与 `[database_notes]`（键=数据库名）；直接改本文件的备注会被下次 sync 抹掉，要长期留就写进那里。")
    def _present(pairs):
        """[(显示键, 台数)] → 去重合并成「键×台数」串：同一区域多账号、同一本地网络多台
        以前是逐条展开，45 台 sangfor 就刷 45 遍同样的字。"""
        agg = {}
        for key, n in pairs:
            agg[key] = agg.get(key, 0) + n
        return " ".join(f"{k}×{n}" for k, n in agg.items())

    present = _present([(f"{s['flag']}{s['region_display']}", len(insts)) for s, insts in scoped])
    if extra:
        present += "  ｜ " + _present([
            (f"{t['flag']}{t['region_display']}({CLOUD_SECTION.get(t['cloud'], t['cloud']).split()[0]})", 1)
            for t in extra])
    L.append(f"- 共 {total} 台（含非 AWS {len(extra)} 台），分布于：{present}。")
    # 各云怎么动它：公有云有 CLI，本地网络机器只有 SSH（深信服另有管控 API，但没进 `ops` CLI）
    _CLOUD_TOOL = {"tencent": "`tccli`", "aliyun": "`aliyun`", "volcengine": "`ve`", "gcp": "`gcloud`", "azure": "`az`",
                   "oracle": "`oci`",
                   "sangfor": "深信服 SCP HCI API（provider 直调或 MCP 工具，`ops` 没有重启子命令）",
                   "vmware": "SSH（无管控 API 纳管，开关机去 vCenter）",
                   "synology": "SSH（无管控 API 纳管）", "local": "SSH（无管控 API 纳管）"}
    for c in sorted({t["cloud"] for t in extra}):
        label = CLOUD_SECTION.get(c, c).split()[0]
        cinsts = [t for t in extra if t["cloud"] == c]
        n_ssh = sum(1 for t in cinsts if t.get("ssh_alias"))
        where = ("见上表「SSH 连接」对应小节" if n_ssh
                 else "无一台有 SSH 别名——Windows/RDP 或未登记登录信息")
        net = "；**非公网，要先进内网/VPN 或 Tailscale 才连得上**" if c in LOCAL_NET_CLOUDS else ""
        L.append(f"- {label} 实例 {n_ssh}/{len(cinsts)} 台有 SSH 别名（{where}，sync 已写入 ssh config）；"
                 f"操作经 {_CLOUD_TOOL.get(c, c)}{net}。")
    L.append("- 各云写操作用对应 CLI：AWS `aws_cli`、腾讯 `tccli`、阿里 `aliyun`；实例重启免确认，其余写操作确认。")
    INVENTORY.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
