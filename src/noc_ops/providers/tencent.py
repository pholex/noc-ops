"""腾讯云驱动（Lighthouse + 域名/DNSPod + 云数据库 MySQL/PostgreSQL + 云函数 SCF + CDN + CAM 子用户 + 账单/余额，tccli）。"""
import calendar
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..collection import ThreadPoolExecutor, observed, optional
from .. import deploy_config
from ..aws_safety import is_read_only_tccli, is_reboot_tccli
from .base import Provider, resolve_keyfile

# account / regions / ssh_user / ssh_key（私钥文件名）见 ops-config.toml 的 [tencent]


@observed(result="nullable")
def _tc_json(args, timeout=60, on_error=None):
    """跑 `tccli <args> --output json`，返回响应 dict；失败 → None 并在 stderr 打一行原因。

    tccli 出错时通常 exit 255 且 stderr 带 `[TencentCloudSDKException] code:... message:...`（比 aliyun / ve 老实，
    可直接按退出码判）；但也防一手「exit 0 却在 JSON 里带 Response.Error / Error」（部分版本/代理会这样），
    非 JSON 输出也算失败。tccli 默认已把外层 `Response` 剥掉，若没剥（老版本）这里再剥一次。

    on_error(action, msg)：自定义失败回调（多区域遍历时用 _WarnOnce 只提示一次，别刷屏）；None = 直接打一行。
    """
    action = " ".join(a for a in args[:2] if not a.startswith("-"))          # 如 "billing DescribeAccountBalance"

    def fail(msg):
        if on_error is None:
            print(f"  ⚠️ 腾讯云 {action}：{msg}", file=sys.stderr)
        else:
            on_error(action, msg)
        return None

    try:
        r = subprocess.run(["tccli", *args, "--output", "json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        # FileNotFoundError（没装 tccli）也落在这里：返回 None，调用方按空列表处理，绝不让 ops sync 崩
        return fail(f"跑 tccli 失败（{e}）" + ("（未安装 tccli？`pip install tccli`）" if isinstance(e, FileNotFoundError) else ""))
    data = None
    if r.returncode == 0 and r.stdout.strip() and "{" in r.stdout:
        try:
            data = json.loads(r.stdout[r.stdout.index("{"):])
        except (json.JSONDecodeError, ValueError):
            data = None
    if not isinstance(data, dict):
        lines = [ln for ln in (r.stderr or r.stdout or "").strip().splitlines() if ln.strip()]
        sdk = next((ln for ln in lines if "TencentCloudSDKException" in ln), None)
        tail = (sdk or (lines[-1] if lines else f"exit {r.returncode}"))[:160]
        return fail(f"tccli 失败（exit {r.returncode}）——{tail}"
                    + ("（未配置密钥？跑 `tccli configure`）" if "credential" in tail.lower() or "secretid" in tail.lower() else ""))
    if isinstance(data.get("Response"), dict) and len(data) == 1:
        data = data["Response"]                                              # 老版本未剥外层
    err = data.get("Error")
    if isinstance(err, dict) and (err.get("Code") or err.get("Message")):     # exit 0 但业务层报错
        code = err.get("Code") or "?"
        hint = ("（该产品未开通/无权限，按未纳管处理）"
                if any(k in code for k in ("UnauthorizedOperation", "ServiceNotActivated", "NotActivated", "AuthFailure")) else "")
        return fail(f"{code} {err.get('Message') or ''}{hint}".rstrip())
    return data


class _WarnOnce:
    """同一账号同一类资源的 tccli 失败只在 stderr 提示**一次**（按 regions 遍历时同一个错误别刷 N 行）。"""

    def __init__(self, account, kind):
        self.account, self.kind, self.done = account, kind, False

    def __call__(self, action, msg):
        if self.done:
            return
        self.done = True
        print(f"  ⚠️ 腾讯云 {self.account} {self.kind}（{action}）：{msg}（本类采集失败）", file=sys.stderr)


def _date(s):
    """腾讯云各产品的时间串统一是 `YYYY-MM-DD HH:mm:ss`（北京时间）→ 取日期部分；空/非串 → ""。"""
    return str(s)[:10] if s else ""


def _days_from_today(s):
    """`YYYY-MM-DD…` → 距今天数（负=已过期）；解析不了 → None。只比日期，8 小时时区差不影响天数粒度。"""
    try:
        d = datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d - datetime.now(timezone.utc).date()).days


def _paged(args, items_key, page_size=100, on_error=None, id_key=None):
    """走 Offset/Limit 分页的 Describe*/List* 接口 → 全部条目。按 TotalCount 或「本页不足一页」收口；
    某页失败即停（已拿到的照常返回）。id_key 给了就按它去重（防某些产品把 Offset 当页码用导致重叠）。"""
    out, seen, offset = [], set(), 0
    while True:
        res = _tc_json([*args, "--Offset", str(offset), "--Limit", str(page_size)], on_error=on_error)
        if res is None:
            break
        items = res.get(items_key) or []
        fresh = []
        for it in items:
            k = it.get(id_key) if id_key else None
            if k is not None:
                if k in seen:
                    continue
                seen.add(k)
            fresh.append(it)
        out.extend(fresh)
        total = res.get("TotalCount")
        if not items or len(items) < page_size or (isinstance(total, int) and len(out) >= total) or (id_key and not fresh):
            break
        offset += len(items)
    return out


def _fen(v):
    """DescribeAccountBalance 全部金额字段单位是**分**（Balance / RealBalance / OweAmount / CreditAmount …）→ 元。"""
    try:
        return round(float(v or 0) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


class TencentProvider(Provider):
    cloud = "tencent"
    cli = "tccli"
    display = "腾讯云"
    flag = "🇨🇳"
    order = 2

    is_read = staticmethod(is_read_only_tccli)
    is_reboot = staticmethod(is_reboot_tccli)

    def reboot_argv(self, inst):
        return ["tccli", "lighthouse", "RebootInstances", "--InstanceIds", f'["{inst["id"]}"]',
                "--region", inst["region"], "--output", "json"]

    def status_argv(self, inst):
        return ["tccli", "lighthouse", "DescribeInstances", "--InstanceIds", f'["{inst["id"]}"]',
                "--region", inst["region"], "--output", "json"]

    def list_instances(self, network=None):
        out = []
        cfg = deploy_config.cloud("tencent")
        # 未配置腾讯云（出厂模板整段注释掉）或没写 regions → 不纳管，静默跳过，不能让整次 sync 崩掉
        if not cfg or not cfg.get("regions"):
            return out
        for region, (disp, flag) in cfg["regions"].items():
            data = _tc_json(["lighthouse", "DescribeInstances", "--region", region])
            if data is None:
                continue
            kf = resolve_keyfile(cfg.get("account", ""), cfg.get("ssh_key", ""))
            for i in data.get("InstanceSet", []):
                pub = (i.get("PublicAddresses") or [None])[0]
                prv = (i.get("PrivateAddresses") or [None])[0]
                cpu, mem = i.get("CPU"), i.get("Memory")
                name = i.get("InstanceName") or i.get("InstanceId")
                alias = "tc-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name)
                out.append({
                    "name": name, "id": i.get("InstanceId"),
                    "profile": cfg.get("account", ""), "region": region, "region_display": disp, "flag": flag,
                    "type": f"Lighthouse {cpu}C{mem}G" if cpu else "Lighthouse",
                    "state": (i.get("InstanceState") or "").lower(),
                    "az": i.get("Zone"), "private_ip": prv, "public_ip": pub,
                    "key_name": cfg.get("ssh_key", ""), "ssh_alias": alias, "ssh_user": cfg.get("ssh_user", ""),
                    "key_file": kf or "", "connect": "direct", "proxyjump": None,
                    # verified 专指"已真实 SSH 探测"；非 AWS 云 sync 不探测，统一 False（UI 显示为"推断"）
                    "verified": False, "cloud": "tencent",
                })
        return out

    # —— 域名（只读）：腾讯云注册的域名（domain DescribeDomainNameList）+ DNSPod 托管的解析域（dnspod DescribeDomainList）——
    # 两边都有的域名只出注册那一行，把 DNSPod 记录数并进备注；只在 DNSPod 托管、注册在别家的出 "DNS zone" 行（无到期日）。
    # 域名注册 / DNSPod 都是全局产品，不带 --region；两个产品任一没开通/无权限 → 该半边按空处理，另一半照常出。
    _GTLD = {"com", "net", "org", "info", "biz", "name", "pro", "mobi"}

    def _domain_type(self, name, tld):
        """经典 gTLD → "gTLD"；两位国家码 → "ccTLD"；其余（新 gTLD）直接给后缀如 ".xyz"（看板"类型"列窄，够看）。"""
        t = (tld or (name.rsplit(".", 1)[-1] if name and "." in name else "") or "").lower().lstrip(".")
        if not t:
            return "—"
        if t in self._GTLD:
            return "gTLD"
        if len(t) == 2:
            return "ccTLD"
        return f".{t}"

    def list_domains(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg:
            return []                                        # 未配置腾讯云 → 不纳管，静默
        account = cfg.get("account", "")
        out, by_name = [], {}
        for x in _paged(["domain", "DescribeDomainNameList"], "DomainSet",
                        on_error=_WarnOnce(account, "域名注册")):
            name = (x.get("DomainName") or "").lower()
            if not name:
                continue
            expd = _date(x.get("ExpirationDate"))
            notes = ["自动续费 ✓" if x.get("AutoRenew") == 1 else "未自动续费 ⚠"]
            status = x.get("BuyStatus")
            if status and status != "ok":                    # AboutToExpire / RegisterPending / TransferPending …
                notes.append({"AboutToExpire": "即将到期 ⚠", "RegisterPending": "注册中", "RegisterDoing": "注册中",
                              "RegisterFailed": "注册失败 ⚠", "TransferPending": "转入中", "TransferDoing": "转入中",
                              "RenewPending": "续费中", "RenewDoing": "续费中", "AboutToRedeem": "赎回期 ⚠"}.get(status, status))
            if x.get("IsPremium"):
                notes.append("溢价域名")
            row = {
                "domain": name, "type": self._domain_type(name, x.get("Tld")),
                "registrar": "腾讯云", "account": account,
                "reg_date": _date(x.get("CreationDate")), "expire_date": expd,
                "days_left": _days_from_today(expd) if expd else None,
                "note": " · ".join(notes),
            }
            out.append(row)
            by_name[name] = row
        # DNSPod：Limit 默认 3000，一般一页就完；Status ENABLE 正常 / PAUSE 暂停 / SPAM 封禁，DNSStatus DNSERROR = NS 未指过来
        for z in _paged(["dnspod", "DescribeDomainList"], "DomainList", page_size=3000,
                        on_error=_WarnOnce(account, "DNSPod 解析")):
            name = (z.get("Name") or z.get("Punycode") or "").lower()
            if not name:
                continue
            rc = z.get("RecordCount")
            parts = [f"{rc} 条记录"] if rc is not None else []
            st = z.get("Status")
            if st and st != "ENABLE":
                parts.append({"PAUSE": "解析已暂停 ⚠", "SPAM": "已封禁 ⚠"}.get(st, st))
            if z.get("DNSStatus") == "DNSERROR":
                parts.append("NS 未指向 DNSPod ⚠")
            if str(z.get("IsVip")).lower() in ("yes", "true", "1") and z.get("GradeTitle"):
                parts.append(f"套餐 {z['GradeTitle']}")
            if name in by_name:                              # 同一域名两边都有 → 并进注册行的备注
                by_name[name]["note"] = " · ".join(p for p in [by_name[name]["note"], *parts] if p)
                continue
            out.append({
                "domain": name, "type": "DNS zone", "registrar": "腾讯云", "account": account,
                "reg_date": _date(z.get("CreatedOn")), "expire_date": "", "days_left": None,
                "note": " · ".join(parts),
            })
        return out

    # —— 数据库（只读）：云数据库 MySQL（cdb）+ PostgreSQL（postgres），按账号 regions 遍历 ——
    # cdb Status 是数字：0 创建中 / 1 运行中 / 4 隔离中 / 5 已隔离；Memory 单位 MB（1000/2000/4000 十进制档）、Volume 单位 GB。
    # postgres 的 Memory/Storage 都是 GB，状态是字串（running / isolated …），端点在 DBInstanceNetInfo 里按 NetType 挑。
    _CDB_STATE = {0: "creating", 1: "running", 4: "isolating", 5: "isolated"}
    _CDB_ROLE = {1: "", 2: "灾备实例", 3: "只读实例"}

    def _cdb_in(self, account, region, disp, flag, warn):
        out = []
        for i in _paged(["cdb", "DescribeDBInstances", "--region", region], "Items", on_error=warn, id_key="InstanceId"):
            cpu, mem, vol = i.get("Cpu"), i.get("Memory"), i.get("Volume")
            spec = f"{cpu}C{mem / 1000:g}G" if cpu and mem else (f"{mem}MB" if mem else "")   # cdb 内存规格是 1000/2000/4000 MB 这种十进制档
            dev = i.get("DeviceType") or ""
            notes = [self._CDB_ROLE.get(i.get("InstanceType"), "")]
            if i.get("WanStatus") == 1 and i.get("WanDomain"):
                notes.append(f"外网 {i['WanDomain']}:{i.get('WanPort')}")
            out.append({
                "name": i.get("InstanceName") or i.get("InstanceId"), "id": i.get("InstanceId"), "cloud": "tencent",
                "account": account, "region": region, "region_display": disp, "flag": flag,
                "engine": f"MySQL {i.get('EngineVersion') or ''}".strip(),
                "class": " ".join(p for p in (spec, dev) if p) or "—",
                "storage": f"{vol}GB {i.get('DiskType') or ''}".strip() if vol is not None else "—",
                "state": self._CDB_STATE.get(i.get("Status"), str(i.get("Status"))),
                "endpoint": f"{i['Vip']}:{i.get('Vport')}" if i.get("Vip") else "—",
                "created": _date(i.get("CreateTime")),
                "note": " · ".join(n for n in notes if n),
            })
        return out

    def _pg_in(self, account, region, disp, flag, warn):
        out = []
        for d in _paged(["postgres", "DescribeDBInstances", "--region", region], "DBInstanceSet",
                        on_error=warn, id_key="DBInstanceId"):
            nets = d.get("DBInstanceNetInfo") or []
            pick = (next((n for n in nets if n.get("NetType") in ("private", "inner") and (n.get("Address") or n.get("Ip"))), None)
                    or next((n for n in nets if n.get("Address") or n.get("Ip")), None))
            cpu, mem, sto = d.get("DBInstanceCpu"), d.get("DBInstanceMemory"), d.get("DBInstanceStorage")
            engine = "PostgreSQL" if (d.get("DBEngine") or "postgresql") == "postgresql" else f"PostgreSQL({d.get('DBEngine')})"
            role = d.get("DBInstanceType")
            out.append({
                "name": d.get("DBInstanceName") or d.get("DBInstanceId"), "id": d.get("DBInstanceId"), "cloud": "tencent",
                "account": account, "region": region, "region_display": disp, "flag": flag,
                "engine": f"{engine} {d.get('DBVersion') or d.get('DBMajorVersion') or ''}".strip(),
                "class": f"{cpu}C{mem}G" if cpu and mem else (d.get("DBInstanceClass") or "—"),
                "storage": f"{sto}GB {d.get('DBInstanceStorageType') or ''}".strip() if sto is not None else "—",
                "state": d.get("DBInstanceStatus") or "—",
                "endpoint": f"{pick.get('Address') or pick.get('Ip')}:{pick.get('Port')}" if pick else "—",
                "created": _date(d.get("CreateTime")),
                "note": {"readonly": "只读实例", "guard": "灾备实例", "temp": "临时实例"}.get(role, ""),
            })
        return out

    def list_databases(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg or not cfg.get("regions"):
            return []
        account = cfg.get("account", "")
        warn_cdb, warn_pg = _WarnOnce(account, "云数据库 MySQL"), _WarnOnce(account, "云数据库 PostgreSQL")
        out = []
        for region, (disp, flag) in cfg["regions"].items():
            out.extend(self._cdb_in(account, region, disp, flag, warn_cdb))
            out.extend(self._pg_in(account, region, disp, flag, warn_pg))
        out.sort(key=lambda d: (d["region"], d["engine"], d["name"] or ""))
        return out

    # —— 云函数 SCF（只读）：按 regions × 命名空间 ListFunctions；内存/超时/代码大小列表里没有，再 GetFunction 逐个补 ——
    # ListFunctions 的 Function 只有名字/运行时/状态/时间；MemorySize / Timeout / CodeSize 只在 GetFunction 里。
    # 函数一般不多，GetFunction 小并发补齐；补不到就留 None（看板显 —），不影响函数本身出行。
    @optional
    def _scf_detail(self, region, ns, name):
        d = _tc_json(["scf", "GetFunction", "--region", region, "--Namespace", ns, "--FunctionName", name],
                     on_error=lambda *a: None)              # 失败静默：列表行已在，细节缺就缺
        if not d:
            return None, None, None
        cs = d.get("CodeSize")
        return d.get("MemorySize"), d.get("Timeout"), (round(cs / 1048576, 1) if isinstance(cs, (int, float)) else None)

    def list_serverless(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg or not cfg.get("regions"):
            return []
        account = cfg.get("account", "")
        warn = _WarnOnce(account, "云函数 SCF")
        rows = []
        for region, (disp, flag) in cfg["regions"].items():
            ns_list = [n.get("Name") for n in _paged(["scf", "ListNamespaces", "--region", region], "Namespaces", on_error=warn)
                       if n.get("Name")] or ["default"]
            for ns in ns_list:
                for f in _paged(["scf", "ListFunctions", "--region", region, "--Namespace", ns], "Functions",
                                on_error=warn, id_key="FunctionId"):
                    rows.append({
                        "name": f.get("FunctionName"), "id": f.get("FunctionId"), "namespace": ns, "cloud": "tencent",
                        "account": account, "region": region, "region_display": disp, "flag": flag,
                        "runtime": f.get("Runtime") or f.get("Type") or "—",
                        "memory": None, "timeout": None, "code_mb": None,
                        "last_modified": _date(f.get("ModTime")),
                        "state": f.get("Status") or "",
                    })
        if rows:
            with ThreadPoolExecutor(max_workers=4) as ex:
                details = list(ex.map(lambda r: self._scf_detail(r["region"], r["namespace"], r["name"]), rows))
            for r, (mem, to, mb) in zip(rows, details):
                r["memory"], r["timeout"], r["code_mb"] = mem, to, mb
        rows.sort(key=lambda x: (x["region"], x["namespace"], x["name"] or ""))
        return rows

    # —— CAM 子用户（只读）：cam ListUsers（不分页，一次全量）+ 每用户 cam ListAccessKeys --TargetUin ——
    # 主账号自己不出行（与 AWS 不列 root 一致）。CAM 没有「上次登录」字段，last_used 统一 "—"。
    # ListAccessKeys 只给密钥 ID/状态/创建时间，**不含密钥串**；这里也只记数量与最老密钥年龄。
    @optional
    def _cam_keys(self, uin, warn):
        d = _tc_json(["cam", "ListAccessKeys", "--TargetUin", str(uin)], on_error=warn)
        keys = (d or {}).get("AccessKeys") or []
        ages = [a for a in (_days_from_today(k.get("CreateTime")) for k in keys) if a is not None]
        inactive = sum(1 for k in keys if (k.get("Status") or "").lower() == "inactive")
        return len(keys), (-min(ages) if ages else None), inactive      # 创建日期在过去 → 距今天数取负再反号

    def list_iam(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg:
            return []
        account = cfg.get("account", "")
        d = _tc_json(["cam", "ListUsers"], on_error=_WarnOnce(account, "CAM 子用户"))
        if not d:
            return []
        users = [u for u in (d.get("Data") or []) if u.get("Uin") is not None]
        warn = _WarnOnce(account, "CAM 访问密钥")
        with ThreadPoolExecutor(max_workers=4) as ex:
            keyinfo = list(ex.map(lambda u: self._cam_keys(u["Uin"], warn), users))
        out = []
        for u, (kc, oldest, inactive) in zip(users, keyinfo):
            detail = [u.get("Remark") or ""]
            if u.get("ConsoleLogin") == 1:
                detail.append("可登录控制台")
            if inactive:
                detail.append(f"{inactive} 把密钥已停用")
            out.append({
                "name": u.get("Name") or str(u.get("Uin")), "id": str(u.get("Uin")), "cloud": "tencent", "account": account,
                "created": _date(u.get("CreateTime")), "last_used": "—",
                "key_count": kc, "oldest_key_days": oldest,
                "detail": " · ".join(p for p in detail if p),
            })
        out.sort(key=lambda x: x["name"] or "")
        return out

    # —— 成本（billing DescribeBillSummaryByProduct：账号 × 产品 × 月）——
    # 不计费。金额取 RealTotalCost（优惠后实付 = 现金 + 赠金 + 代金券 + 分成），不取原价 TotalCost。
    # ⚠️ 汇总是**懒生成**的：某月第一次请求可能 Ready=0 / 数据为空（或 SummaryDataNotReady），几秒后再请求就有——
    # 所以 Ready=0 要短暂重试，仍不就绪则跳过该月（不能当成「0 花费」）。
    def list_billing(self, months=3):
        today = datetime.now(timezone.utc).date()
        y, m = today.year, today.month - (months - 1)
        while m <= 0:
            m += 12
            y -= 1
        periods = []
        for _ in range(months):
            periods.append((y, m))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        cur_month = today.strftime("%Y-%m")
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        account = deploy_config.cloud("tencent").get("account", "")
        out = []
        for y, m in periods:
            period = f"{y:04d}-{m:02d}"
            last = calendar.monthrange(y, m)[1]
            args = ["billing", "DescribeBillSummaryByProduct",
                    "--BeginTime", f"{period}-01 00:00:00", "--EndTime", f"{period}-{last:02d} 23:59:59"]
            res = None
            for attempt in range(3):                       # 懒生成：最多等 2 次 × 3s
                res = _tc_json(args)
                if res is None or res.get("Ready") == 1:
                    break
                time.sleep(3)
            if res is None:
                continue
            if res.get("Ready") != 1:
                print(f"  ⚠️ 腾讯云 {account} 账单 {period}：汇总仍在生成（Ready=0），本次跳过，稍后 `ops sync --kind=billing` 再刷",
                      file=sys.stderr)
                continue
            for b in res.get("SummaryOverview") or []:
                try:
                    amt = float(b.get("RealTotalCost") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                if amt <= 0:
                    continue                                   # 实付 0（全额券抵/免费额度）不出行
                out.append({
                    "cloud": "tencent", "account": account,
                    "service": b.get("BusinessCodeName") or b.get("BusinessCode") or "Other",
                    "month": period, "amount": round(amt, 2), "currency": "CNY",
                    "period_start": f"{period}-01", "partial": period == cur_month,
                    "fetched_at": fetched,
                })
        out.sort(key=lambda x: (x["month"], -x["amount"]))
        return out

    # —— 账务 / 额度（billing DescribeAccountBalance：可用余额 · 欠费 · 信用额度 · 是否允许欠费）——
    # 腾讯云也是「余额 + 后付费」模型。credit 列放 RealBalance（真实可用余额，含现金+赠金，单位分→元）。
    # IsAllowArrears=false 且无信用额度 → 余额用尽即停服 → spending_limit="On"；允许欠费则 Off。
    # 预算：tccli billing 没有预算列表接口，budget=None。
    def list_credits(self):
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        account = deploy_config.cloud("tencent").get("account", "")
        bal = _tc_json(["billing", "DescribeAccountBalance"])
        if bal is None:
            return []                                        # 查不到余额就不出行，别造假行
        owe, credit_amt = _fen(bal.get("OweAmount")), _fen(bal.get("CreditAmount"))
        allow_arrears = bool(bal.get("IsAllowArrears"))
        return [{
            "cloud": "tencent", "account": account,
            "account_name": f"腾讯云 {account}",
            "plan": "Balance+PostPaid",
            "state": "Arrears" if owe > 0 else "Normal",
            "credit": _fen(bal.get("RealBalance", bal.get("Balance"))), "credit_currency": "CNY",
            "free_tier": None, "free_tier_end": None, "free_tier_days": None,
            "spending_limit": "Off" if (allow_arrears or credit_amt > 0) else "On",
            "budget": None,
            "currency": "CNY",
            "fetched_at": fetched,
        }]

    # —— 对象存储 COS（官方 coscli；tccli 不覆盖 COS）——
    @staticmethod
    @observed(result="pair")
    def _coscli_ls(timeout=60):
        """跑 `coscli ls`（列桶），返回 (ok, 文本)。未装 → (False, "missing")；未配置 → (False, "unconfigured")。
        stdin 必须关掉：coscli 第一次运行没配置会进交互式初始化（还会顺手写一份模板 ~/.cos.yaml）。"""
        try:
            r = subprocess.run(["coscli", "ls"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return False, "missing"
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        text = (r.stdout or "") + (r.stderr or "")
        if "Input Your Mode" in text or "generate the default configuration" in text:
            return False, "unconfigured"
        if r.returncode != 0:
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            return False, (lines[-1][:160] if lines else f"exit {r.returncode}")
        return True, r.stdout

    @staticmethod
    def _parse_coscli_ls(text):
        """表格行 `  name | ap-nanjing | 2026-10-25T05:14:57Z` → dict 列表（跳过表头 / 分隔线 / 合计行）。"""
        out = []
        for line in text.splitlines():
            if "|" not in line or set(line.strip()) <= set("-+| "):
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 3 or not cells[0] or cells[0] in ("BUCKET NAME",) or "TOTAL BUCKETS" in line:
                continue
            out.append({"name": cells[0], "region": cells[1], "created": cells[2]})
        return out

    def list_storage(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg:
            return []                                        # 未配置腾讯云 → 不纳管，静默
        ok, text = self._coscli_ls()
        if not ok:
            hint = {"missing": "未安装 coscli（腾讯云 COS 官方工具，tccli 不覆盖 COS），COS 桶不纳管",
                    "unconfigured": "coscli 未配置（`coscli config init` 填 SecretId/SecretKey），COS 桶不纳管"}.get(text, f"coscli ls 失败——{text}")
            print(f"  ⚠️ 腾讯云 COS：{hint}", file=sys.stderr)
            return []
        account, regions = cfg.get("account", ""), cfg.get("regions", {}) or {}
        out = []
        for b in self._parse_coscli_ls(text):
            region = b["region"]
            disp, flag = regions.get(region, (region, self.flag))
            out.append({
                "name": b["name"], "cloud": "tencent", "account": account,
                "region": region, "region_display": disp, "flag": flag,
                "domain": f"{b['name']}.cos.{region}.myqcloud.com",
                "created": _date(b["created"]),
            })
        out.sort(key=lambda x: (x["region"], x["name"]))
        return out

    # —— CDN 加速域名（只读；`tccli cdn DescribeDomains`，全局服务不带 --region，Offset/Limit 翻页）——
    # 列表项是 BriefDomain：Domain / Status / ServiceType(web|download|media) / Origin.Origins[] / UpdateTime / ResourceId，
    # 列表接口不包含证书配置，https 使用缺省显示值。
    # 总数键叫 TotalNumber（不是 TotalCount），_paged 靠「本页不足一页」收口即可。
    def list_cdn(self):
        cfg = deploy_config.cloud("tencent")
        if not cfg:
            return []                                        # 未配置腾讯云 → 不纳管，静默
        account = cfg.get("account", "")
        out = []
        for d in _paged(["cdn", "DescribeDomains"], "Domains", on_error=_WarnOnce(account, "CDN 加速域名"), id_key="ResourceId"):
            status = d.get("Status") or ""
            origins = (d.get("Origin") or {}).get("Origins") or []
            out.append({
                "id": d.get("ResourceId") or d.get("Domain") or "", "domain": d.get("Domain") or "", "aliases": "",
                "origin": ", ".join(str(o) for o in origins if o),
                "status": status, "enabled": status == "online",
                "cert": "—",
                "price_class": d.get("ServiceType") or "",
                "last_modified": _date(d.get("UpdateTime")),
                "account": account, "cloud": "tencent", "partition": "cn", "comment": "",
            })
        out.sort(key=lambda x: x["domain"])
        return out
