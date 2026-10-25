"""Oracle Cloud Infrastructure（OCI）驱动（Compute / Object Storage / DNS / Database / Functions / IAM，shell 出 oci CLI）。

展示（只读）：list_instances（Compute 实例 + VNIC 取公网/内网 IP）/ list_storage（对象存储桶）/
list_domains（OCI DNS zone，并入「域名」tab）/ list_databases（Autonomous Database + DB System）/
list_serverless（OCI Functions：application → function，并入「函数」tab）/ list_iam（租户 IAM 用户）/
list_billing（Usage API 按服务×月的成本，并入「成本」tab）。
生命周期：reboot_argv（`oci compute instance action --action SOFTRESET` 优雅重启，免确认自动跑）/
status_argv；启停（STOP/START）经通用 oci 通道走"确认后执行"（is_read/is_reboot 分类器交给 agent/MCP 闸门）。

多账号：见 ops-config.toml 的 [[oracle.accounts]]（每个含 profile=~/.oci/config 的 profile 名、
compartment=扫描根隔间 OCID(通常即 tenancy OCID)、可选 regions/ssh_user/ssh_key）。
OCI 的"账号"= 租户（tenancy），但 noc 记录里 account/profile 一律填 **OCI profile 名**（人读、与
~/.oci/config 对齐）；资源按 compartment + region 维度拉取。
认证沿用本机已配好的 oci（`oci setup config` 写的 ~/.oci/config + API 签名密钥），noc 不碰任何密钥。
OCI CLI 输出统一包了一层 {"data": ...}，本模块的 _oci 已自动拆封。
"""
import configparser
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..collection import observed, optional
from .. import deploy_config
from ..aws_safety import is_read_only_oci, is_reboot_oci
from .base import Provider, resolve_keyfile

_FLAG = "🔶"   # OCI 多区域，用 Oracle 橙；具体 region 显示在 region 列
# OCI 实例 SSH 私钥的目录段（云级，一把 key 管所有 OCI 实例）：~/.ssh/oraclecloud/<ssh_key>.pem。
# 区别于其它云的「按账号 ID 分目录」——OCI 用云级目录因 ssh_key 是 OCI 通用、且 profile 名(DEFAULT)不适合做目录名。
_SSH_KEY_DIR = "oraclecloud"
# oci CLI 自己的配置（`oci setup config` 写的）；只在账号未显式配 tenancy、compartment 又不是租户 OCID 时
# 读其 [profile] 的 tenancy 字段兜底（只读 tenancy 这一项，不碰 key_file/fingerprint）。
_OCI_CONFIG = Path("~/.oci/config")


@observed(result="nullable")
def _oci_raw(account, args, region=None, timeout=60, soft=False):
    """跑一条 oci 命令（带 --profile，可选 --region），返回**整个**解析后的 JSON（通常 {"data": ...}），不拆封。

    失败返回 None（soft=True 时静默——用于「某服务未订阅/隔间无此资源」等预期空结果，避免刷屏），
    否则打一行诊断到 stderr（不静默吞）——一次临时的 oci 掉认证会让看板上 OCI 资源"凭空消失"，
    要看得出是认证问题。空输出（rc=0 但什么都没打）返回 {}。
    绝大多数 list_* 用下面的 _oci（拆封成 list）；只有 data 不是 list 的接口（如 usage-api 的
    {"data": {"group-by": [...], "items": [...]}}）直接用本函数。
    """
    prof = account.get("profile", "DEFAULT")
    action = " ".join(a for a in args[1:4] if not a.startswith("-"))   # 如 "compute instance" / "os bucket"
    cmd = list(args) + ["--profile", prof]
    if region:
        cmd += ["--region", region]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        if not soft:
            print(f"  ⚠️ OCI {prof} {action}：跑 oci 失败（{e}）", file=sys.stderr)
        return None
    if r.returncode != 0:
        if soft:
            return None
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        low = (r.stderr or "").lower()
        hint = ("（oci 未配置/认证失败？检查 ~/.oci/config 或跑 `oci setup config`）"
                if ("notauthenticated" in low or "config" in low or "authorization" in low
                    or "could not find config" in low) else "")
        print(f"  ⚠️ OCI {prof} {action}：oci 失败——{tail}{hint}", file=sys.stderr)
        return None
    out = (r.stdout or "").strip()
    if not out:                       # 空列表时 oci 可能不输出任何内容（rc=0）
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        if not soft:
            print(f"  ⚠️ OCI {prof} {action}：oci 输出非 JSON，跳过", file=sys.stderr)
        return None


def _oci(account, args, region=None, timeout=60, soft=False):
    """_oci_raw 的拆封版：解析并拆封 {"data": [...]}，保证返回 list。

    三态：成功有数据 / 成功空列表（rc=0 但 data 为空，返回 []）/ 调用失败（返回 None [+诊断]）。
    """
    parsed = _oci_raw(account, args, region=region, timeout=timeout, soft=soft)
    if parsed is None:
        return None
    # OCI 把列表包成 {"data":[...]}；空结果却是 {"opc-total-items":"0"}（无 data 键）。
    # 本模块所有 list_* 消费方都要 list，故统一保证返回 list（非 list/无 data → []）。
    data = parsed.get("data", []) if isinstance(parsed, dict) else parsed
    return data if isinstance(data, list) else []


@observed(result="nullable")
def _tenancy(account):
    """账号的租户 OCID（usage-api 的 --tenant-id）：显式 tenancy 键 > compartment（通常即租户 OCID）>
    ~/.oci/config 该 profile 的 tenancy 字段。都拿不到返回 None。"""
    t = account.get("tenancy")
    if t:
        return t
    comp = account.get("compartment") or ""
    if comp.startswith("ocid1.tenancy."):
        return comp
    cfg = configparser.ConfigParser()
    try:
        cfg.read(_OCI_CONFIG.expanduser())
        return cfg.get(account.get("profile", "DEFAULT"), "tenancy", fallback=None) or None
    except (OSError, configparser.Error):
        return None


class OracleProvider(Provider):
    cloud = "oracle"
    cli = "oci"
    display = "Oracle Cloud"
    flag = _FLAG
    order = 6                       # IaaS 同组：AWS=0 阿里=1 腾讯=2 火山=3 GCP=4 Azure=5 Oracle=6，只读纳管接其后

    is_read = staticmethod(is_read_only_oci)
    is_reboot = staticmethod(is_reboot_oci)

    @staticmethod
    def _accounts():
        return deploy_config.cloud_accounts("oracle")

    @staticmethod
    def _regions(account):
        """该账号要扫的区域列表 [(region, 显示名, 旗帜), ...]。
        未配 regions 时返回 [(None, "", flag)] —— region=None 表示不带 --region、用 profile 的 home region。"""
        regions = account.get("regions") or {}
        if not regions:
            return [(None, "", _FLAG)]
        return [(r, disp, flag) for r, (disp, flag) in regions.items()]

    @staticmethod
    def _label(a):
        """清单/看板「账号」列的展示名：优先 account（如租户名 <tenancy-name>），回退 CLI profile 名。"""
        return a.get("account") or a.get("profile")

    def _account_for(self, inst):
        """实例 → 其所属账号 dict（inst['profile'] 存的是展示名 _label）。兜底主账号。
        reboot/status argv 再从该 dict 取真正的 CLI profile（a['profile']）。"""
        for a in self._accounts():
            if self._label(a) == inst.get("profile"):
                return a
        return deploy_config.cloud("oracle")

    # —— 生命周期：实例操作需 instance OCID(=id) + region ——
    def reboot_argv(self, inst):
        # OCI 有优雅重启：instance action --action SOFTRESET（ACPI 关机后开机；区别于硬 RESET=power-cycle）。
        argv = ["oci", "compute", "instance", "action", "--instance-id", inst["id"],
                "--action", "SOFTRESET", "--profile", self._account_for(inst).get("profile", "DEFAULT")]
        if inst.get("region"):
            argv += ["--region", inst["region"]]
        return argv

    def status_argv(self, inst):
        argv = ["oci", "compute", "instance", "get", "--instance-id", inst["id"],
                "--profile", self._account_for(inst).get("profile", "DEFAULT"),
                "--query", 'data."lifecycle-state"', "--raw-output"]
        if inst.get("region"):
            argv += ["--region", inst["region"]]
        return argv

    # —— Compute 实例 ——
    def list_instances(self, network=None):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            label = self._label(a)
            kf = resolve_keyfile(_SSH_KEY_DIR, a.get("ssh_key")) if a.get("ssh_key") else None   # ~/.ssh/oraclecloud/<key>.pem
            for region, disp, flag in self._regions(a):
                data = _oci(a, ["oci", "compute", "instance", "list",
                                "--compartment-id", comp, "--all"], region=region)
                if not data:
                    continue
                for i in data:
                    state = (i.get("lifecycle-state") or "").lower()   # RUNNING/STOPPED/TERMINATED → 小写
                    # 已/正在终止的实例 OCI 还会列一两天，但它不是在管资源——跳过，别污染清单
                    if state in ("terminated", "terminating"):
                        continue
                    reg = i.get("region") or region or ""
                    name = i.get("display-name") or i.get("id")
                    iid = i.get("id")
                    prv, pub = self._vnic_ips(a, iid, region)
                    out.append({
                        "name": name, "id": iid,
                        "profile": label, "region": reg, "region_display": (disp or reg), "flag": flag,
                        "type": i.get("shape") or "OCI", "state": state,
                        "az": i.get("availability-domain") or "",
                        "private_ip": prv, "public_ip": pub,
                        "key_name": a.get("ssh_key"),
                        "ssh_alias": "oci-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name),
                        "ssh_user": a.get("ssh_user"), "key_file": kf or "",
                        # verified 专指"已真实 SSH 探测"；非 AWS 云 sync 不探测，统一 False（密钥是否存在见 key_file，UI 显示为"推断"）
                        "connect": "direct", "proxyjump": None, "verified": False, "cloud": "oracle",
                    })
        return out

    @optional
    def _vnic_ips(self, account, instance_id, region):
        """取实例主 VNIC 的 (内网 IP, 公网 IP)。soft：实例已停/无 VNIC 时报错属预期，不刷屏。"""
        if not instance_id:
            return None, None
        vnics = _oci(account, ["oci", "compute", "instance", "list-vnics",
                               "--instance-id", instance_id], region=region, soft=True)
        if not vnics:
            return None, None
        v = vnics[0]
        return v.get("private-ip"), v.get("public-ip")

    # —— 对象存储桶 ——
    def list_storage(self):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            for region, disp, _flag in self._regions(a):
                # soft：region 未启用对象存储/隔间无桶时报错，属预期
                data = _oci(a, ["oci", "os", "bucket", "list", "--compartment-id", comp],
                            region=region, soft=True)
                for b in (data or []):
                    name = b.get("name")
                    reg = region or ""
                    ns = b.get("namespace") or ""
                    out.append({
                        "name": name, "cloud": "oracle", "account": self._label(a),
                        "region": reg, "region_display": (disp or reg),
                        # 对象端点：https://objectstorage.<region>.oraclecloud.com/n/<ns>/b/<bucket>/o/
                        "domain": (f"objectstorage.{reg}.oraclecloud.com/n/{ns}/b/{name}" if reg and ns
                                   else f"{name} (objectstorage)"),
                        "created": (b.get("time-created") or "")[:10],
                    })
        out.sort(key=lambda s: (s["account"], s["name"] or ""))
        return out

    # —— OCI DNS zone → 「域名」tab ——
    def list_domains(self):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            for region, _disp, _flag in self._regions(a):
                data = _oci(a, ["oci", "dns", "zone", "list", "--compartment-id", comp],
                            region=region, soft=True)
                for z in (data or []):
                    ztype = (z.get("zone-type") or "").title()   # PRIMARY/SECONDARY
                    out.append({
                        "domain": z.get("name"), "type": (f"DNS {ztype}".strip() or "DNS zone"),
                        "registrar": "OCI DNS", "account": self._label(a),
                        "reg_date": (z.get("time-created") or "")[:10], "expire_date": "",
                        "days_left": None, "note": (z.get("lifecycle-state") or "active").lower(),
                    })
        return out

    # —— 托管数据库：Autonomous Database + DB System ——
    def list_databases(self):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            for region, disp, _flag in self._regions(a):
                reg = region or ""
                # Autonomous Database（ADB）
                adb = _oci(a, ["oci", "db", "autonomous-database", "list", "--compartment-id", comp],
                           region=region, soft=True)
                for d in (adb or []):
                    cores = d.get("cpu-core-count")
                    tb = d.get("data-storage-size-in-tbs")
                    out.append({
                        "name": d.get("display-name") or d.get("db-name"), "cloud": "oracle", "account": self._label(a),
                        "region": reg, "region_display": (disp or reg), "flag": self.flag,
                        "engine": ("Autonomous " + str(d.get("db-version") or "")).strip(),
                        "class": (f"{cores} OCPU" if cores else (d.get("db-workload") or "—")),
                        "storage": (f"{tb} TB" if tb else "—"),
                        "state": (d.get("lifecycle-state") or "").lower() or "—",
                        "endpoint": (d.get("connection-strings") or {}).get("high") or "—",
                    })
                # DB System（VM/裸金属上的 Oracle DB）
                sysd = _oci(a, ["oci", "db", "system", "list", "--compartment-id", comp],
                            region=region, soft=True)
                for d in (sysd or []):
                    cores = d.get("cpu-core-count")
                    gb = d.get("data-storage-size-in-gbs")
                    out.append({
                        "name": d.get("display-name") or d.get("hostname"), "cloud": "oracle", "account": self._label(a),
                        "region": reg, "region_display": (disp or reg), "flag": self.flag,
                        "engine": ("DB System " + str(d.get("database-edition") or "")).replace("_", " ").strip(),
                        "class": (f"{cores} OCPU" if cores else (d.get("shape") or "—")),
                        "storage": (f"{gb} GB" if gb else "—"),
                        "state": (d.get("lifecycle-state") or "").lower() or "—",
                        "endpoint": (d.get("scan-dns-name") or d.get("hostname") or "—"),
                    })
        out.sort(key=lambda d: (d["account"], d["name"] or ""))
        return out

    # —— OCI Functions（只读）→「函数」tab ——
    # 两级：application（函数的分组/网络容器）→ function。行名用 "<application>/<function>"，
    # 与 Lambda 行同列（runtime/memory/timeout/code_mb/last_modified）；OCI 函数是容器镜像，
    # 没有 Lambda 的"运行时"与"代码包大小"概念：runtime 列放镜像名（去掉仓库前缀），code_mb 恒 None。
    # 坑：① Functions 服务未开通/无权限时 `fn application list` 报错——soft 吞掉、每账号只提示一次；
    #     ② 空结果 oci 可能不输出任何内容或输出 {"opc-total-items":"0"}（无 data 键），_oci 已统一成 []。
    def list_serverless(self):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            label = self._label(a)
            hinted = False                                  # 该账号的「服务不可用」提示只打一次，别每区域刷一遍
            for region, disp, flag in self._regions(a):
                reg = region or ""
                apps = _oci(a, ["oci", "fn", "application", "list", "--compartment-id", comp, "--all"],
                            region=region, soft=True)
                if apps is None:                            # 调用失败（未开通 Functions / 缺权限 / 区域不支持）
                    if not hinted:
                        print(f"  ⚠️ OCI {prof} fn：application list 失败（Functions 未开通或缺 fn-family 读权限），"
                              f"函数清单留空", file=sys.stderr)
                        hinted = True
                    continue
                for app in apps:
                    app_name = app.get("display-name") or app.get("id") or ""
                    fns = _oci(a, ["oci", "fn", "function", "list", "--application-id", app.get("id"), "--all"],
                               region=region, soft=True)
                    if fns is None:
                        if not hinted:
                            print(f"  ⚠️ OCI {prof} fn：function list 失败（application {app_name}），该应用函数留空",
                                  file=sys.stderr)
                            hinted = True
                        continue
                    for f in fns:
                        image = f.get("image") or ""
                        out.append({
                            "name": f"{app_name}/{f.get('display-name') or f.get('id')}",
                            "cloud": "oracle", "account": label,
                            "region": reg, "region_display": (disp or reg), "flag": flag,
                            # 镜像 "<region>.ocir.io/<ns>/<repo>/<fn>:<tag>" 只留最后一段；无镜像(预构建函数)标 container
                            "runtime": (image.rsplit("/", 1)[-1] if image else "container"),
                            "memory": f.get("memory-in-mbs"), "timeout": f.get("timeout-in-seconds"),
                            "code_mb": None,
                            "last_modified": (f.get("time-updated") or f.get("time-created") or "")[:10],
                        })
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— 租户 IAM 用户（用户在 tenancy 根隔间）——
    def list_iam(self):
        out = []
        for a in self._accounts():
            prof, comp = a.get("profile"), a.get("compartment")
            if not prof or not comp:
                continue
            # IAM 用户挂在租户根隔间；compartment 通常即 tenancy OCID。soft：无权限时不刷屏。
            data = _oci(a, ["oci", "iam", "user", "list", "--compartment-id", comp], soft=True)
            for u in (data or []):
                desc = u.get("description") or ""
                mfa = u.get("is-mfa-activated")
                bits = []
                if desc:
                    bits.append(desc)
                bits.append("MFA✓" if mfa else "MFA✗")
                out.append({
                    "name": u.get("name") or u.get("id"), "cloud": "oracle", "account": self._label(a),
                    "created": (u.get("time-created") or "")[:10], "last_used": "—",
                    # OCI 用户无 access-key 计数概念（用 API 签名密钥/auth token），这些 AWS 专属列留空
                    "key_count": None, "oldest_key_days": None,
                    "detail": "👤 用户 · " + " · ".join(bits),
                    "system": False, "deleted": False,
                })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    # —— 成本（只读）→「成本」tab：Usage API 按服务×月 ——
    # `oci usage-api usage-summary request-summarized-usages`（名字带 request，但它是 POST 形式的**查询**，
    # 不改任何东西）。一次调用覆盖整个 months 窗口（MONTHLY 粒度自动按自然月切，items 每项带
    # time-usage-started），不用逐月调。
    # 坑（2026-10-25 实测）：
    #   ① 它是租户级接口，用 --tenant-id（租户 OCID，非 compartment）；按 profile 的默认 region
    #      （即 home region）调即可，不传 --region；
    #   ② 返回 {"data": {"group-by": [...], "items": [...]}}——data 是 dict 不是 list，_oci 会把它拆成 []，
    #      必须走 _oci_raw；
    #   ③ 时间参数接受 YYYY-MM-DDTHH:mm:ssZ 或纯日期（按 UTC 零点），结束时间排他，取「明天」才含今天；
    #   ④ Always Free 租户每服务每月都会出一行，但 computed-amount 全 0、currency 是单个空格 " "——
    #      0 金额一律跳过，别造「花了 0 元」的假行；币种为空时回退 USD；
    #   ⑤ 同服务同月可能多行（按 sku/区域再拆）→ 先按 (service, month) 求和再出行；
    #   ⑥ 无权限（需 `read usage-reports in tenancy`）/ oci 未装 → 该账号留空、提示一行，不抛。
    def list_billing(self, months=3):
        today = datetime.now(timezone.utc).date()
        y, m = today.year, today.month - (months - 1)      # 近 months 个月（含当月）的首日
        while m <= 0:
            m += 12
            y -= 1
        start = f"{y:04d}-{m:02d}-01T00:00:00Z"
        end = (today + timedelta(days=1)).isoformat() + "T00:00:00Z"   # 排他，取明天以含当月至今
        cur_month = today.strftime("%Y-%m")
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in self._accounts():
            prof = a.get("profile")
            if not prof:
                continue
            label = self._label(a)
            tenancy = _tenancy(a)
            if not tenancy:
                print(f"  ⚠️ OCI {prof} usage-api：拿不到租户 OCID（账号未配 tenancy、compartment 也不是租户），成本留空",
                      file=sys.stderr)
                continue
            parsed = _oci_raw(a, ["oci", "usage-api", "usage-summary", "request-summarized-usages",
                                  "--tenant-id", tenancy,
                                  "--time-usage-started", start, "--time-usage-ended", end,
                                  "--granularity", "MONTHLY", "--query-type", "COST",
                                  "--group-by", '["service"]'],
                               timeout=120, soft=True)
            if parsed is None:                               # oci 未装/未配置、或缺 usage-reports 读权限
                print(f"  ⚠️ OCI {prof} usage-api：request-summarized-usages 失败（oci 未装/未配置，"
                      f"或缺 `read usage-reports in tenancy` 权限），成本留空", file=sys.stderr)
                continue
            data = parsed.get("data") if isinstance(parsed, dict) else None
            items = data.get("items") if isinstance(data, dict) else None   # 无 data/items 键 = 合法的空结果
            summed = {}                                      # (service, month) → [amount, currency]
            for it in (items or []):
                try:
                    amt = float(it.get("computed-amount") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                month = (it.get("time-usage-started") or "")[:7]
                if not month:
                    continue
                svc = it.get("service") or "Other"
                cur = (it.get("currency") or "").strip()
                slot = summed.setdefault((svc, month), [0.0, ""])
                slot[0] += amt
                if cur and not slot[1]:
                    slot[1] = cur
            for (svc, month), (amt, cur) in summed.items():
                amt = round(amt, 2)
                if amt <= 0:                                 # 只保留有实际花费的服务（Always Free 全 0 → 不出行）
                    continue
                out.append({
                    "cloud": "oracle", "account": label, "service": svc,
                    "month": month, "amount": amt, "currency": cur or "USD",
                    "period_start": f"{month}-01", "partial": month == cur_month,
                    "fetched_at": fetched,
                })
        out.sort(key=lambda x: (x["account"], x["month"], -x["amount"]))
        return out
