"""阿里云驱动（ECS + OSS + 域名 + RAM 用户 + RDS/PolarDB + FC + CDN + 账单/余额，aliyun CLI）。"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

from ..collection import ThreadPoolExecutor, observed, failed, optional
from .. import deploy_config
from ..aws_safety import is_read_only_aliyun, is_reboot_aliyun
from .base import Provider, resolve_keyfile

# 多账号：见 ops-config.toml 的 [[aliyun.accounts]]（每个含 profile/account，可选 regions/ssh_user/ssh_key）。
# 只有域名、没有服务器的账号（如 <domains-only-profile>）只需 profile + account，省略 regions/ssh_* 即可。


@observed(result="nullable")
def _ali_json(account, args, timeout=60, quiet=False):
    """跑 `aliyun <args> --profile <p>`，返回响应 dict；失败 → None 并在 stderr 打一行原因。

    ⚠️ aliyun CLI 对「未知 profile / API 不存在 / 服务端错误」**一律 exit 0**（2026-10-25 实测），
    所以不能靠退出码，得看 JSON：非 JSON 输出、带 error_code / status_code、或 BssOpenApi 自己的
    `Success: false` 都算失败。与 volcengine._ve_json 同款三态（有数据 / 空 / None）。

    quiet=True：失败时**不**往 stderr 打——给「按用户/按实例扇出」的调用用，调用方自己汇总成
    每账号一行（否则缺一个权限就刷 N 行同样的话）。
    """
    acct, profile = account.get("account", "?"), account.get("profile")
    action = " ".join(a for a in args[:2] if not a.startswith("-"))          # 如 "bssopenapi QueryAccountBalance"
    cmd = ["aliyun", *args, "--profile", profile]

    def warn(text):
        if not quiet:
            print(f"  ⚠️ 阿里云 {acct} {action}：{text}".rstrip(), file=sys.stderr)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"跑 aliyun 失败（{e}）")
        return None
    data = None
    if r.stdout.strip() and "{" in r.stdout:
        try:
            data = json.loads(r.stdout[r.stdout.index("{"):])
        except (json.JSONDecodeError, ValueError):
            data = None
    if not isinstance(data, dict) or r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        warn(f"aliyun 失败——{tail}"
             + ("（profile 未配置？跑 `aliyun configure --profile <名>`）" if "profile" in tail.lower() else ""))
        return None
    # CLI 层错误（API 不存在 / 参数错 / 4xx）：{"schemaVersion":..., "message":..., "error_code"?: ...}
    if "error_code" in data or "status_code" in data or ("schemaVersion" in data and "message" in data):
        warn(f"{data.get('error_code') or 'CLI'} {data.get('message') or ''}")
        return None
    # BssOpenApi 业务层：Success=false（Code 如 NotApplicable / Forbidden）
    if data.get("Success") is False:
        warn(f"{data.get('Code') or '?'} {data.get('Message') or ''}")
        return None
    return data


def _days_since(iso):
    """阿里云时间戳（"2026-05-03T17:44:36Z"）→ 距今天数；空/不可解析 → None。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _gb(v, unit=1):
    """存储量 → "<n> GB"；v 为 None/非数 → "—"。unit：v 的单位换成 GB 的除数（字节传 2**30）。"""
    try:
        n = float(v) / unit
    except (TypeError, ValueError):
        return "—"
    return f"{n:.0f} GB" if n >= 10 or n == int(n) else f"{n:.1f} GB"


def _amount(v):
    """BssOpenApi 金额有两种形态：QueryBillOverview 给数值，QueryAccountBalance 给**带千分位的字符串**
    （"1,475.05"）——去逗号再转；None/空/不可解析 → 0.0。"""
    if v in (None, ""):
        return 0.0
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


class AliyunProvider(Provider):
    cloud = "aliyun"
    cli = "aliyun"
    display = "阿里云"
    flag = "🇨🇳"
    order = 1

    is_read = staticmethod(is_read_only_aliyun)
    is_reboot = staticmethod(is_reboot_aliyun)

    @staticmethod
    def _accounts():
        return deploy_config.cloud_accounts("aliyun")

    def _profile_for(self, inst):
        """实例 → 其所属账号的 CLI profile 名（inst['profile'] 存的是账号 ID）。兜底主账号。"""
        acc = inst.get("profile")
        for a in self._accounts():
            if a.get("account") == acc:
                return a.get("profile")
        return deploy_config.cloud("aliyun").get("profile")

    def reboot_argv(self, inst):
        return ["aliyun", "ecs", "RebootInstance", "--InstanceId", inst["id"],
                "--RegionId", inst["region"], "--profile", self._profile_for(inst)]

    def status_argv(self, inst):
        return ["aliyun", "ecs", "DescribeInstances", "--RegionId", inst["region"],
                "--profile", self._profile_for(inst), "--InstanceIds", f'["{inst["id"]}"]']

    def list_instances(self, network=None):
        out = []
        for a in self._accounts():
            profile, account = a.get("profile"), a.get("account", "")
            if not profile:
                continue
            kf = resolve_keyfile(account, a.get("ssh_key")) if a.get("ssh_key") else None
            for region, (disp, flag) in a.get("regions", {}).items():
                data = _ali_json(a, ["ecs", "DescribeInstances", "--RegionId", region])
                if data is None:
                    continue
                for i in data.get("Instances", {}).get("Instance", []):
                    pub = (i.get("PublicIpAddress", {}).get("IpAddress") or [])
                    eip = i.get("EipAddress", {}).get("IpAddress")
                    prv = (i.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress")
                           or i.get("InnerIpAddress", {}).get("IpAddress") or [])
                    name = i.get("InstanceName") or i.get("InstanceId")
                    out.append({
                        "name": name, "id": i.get("InstanceId"),
                        "profile": account, "region": region, "region_display": disp, "flag": flag,
                        "type": i.get("InstanceType") or "ECS",
                        "state": (i.get("Status") or "").lower(),   # Running -> running
                        "az": i.get("ZoneId"), "private_ip": (prv[0] if prv else None),
                        "public_ip": (pub[0] if pub else eip),
                        "key_name": a.get("ssh_key"), "ssh_alias": "ali-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name),
                        "ssh_user": a.get("ssh_user"), "key_file": kf or "",
                        # verified 专指"已真实 SSH 探测"；非 AWS 云 sync 不探测，统一 False（密钥是否存在见 key_file，UI 显示为"推断"）
                        "connect": "direct", "proxyjump": None, "verified": False, "cloud": "aliyun",
                    })
        return out

    def list_storage(self):
        """阿里云 OSS 桶（走 aliyun 集成的 ossutil：`aliyun --profile <p> oss ls`），逐账号收集。
        ⚠️ `--profile` 必须在 `oss` **之前**——ossutil 不认其后的 `--profile`（会报 Bad flag）。
        domain 列：OSS 暂用对象端点 `<bucket>.<oss-region>.aliyuncs.com`（等同 S3 的原始 Origin 兜底；
        如将来绑 CDN/自定义域名可再补 DescribeUserDomains 反查）。"""
        out = []
        for a in self._accounts():
            profile, account, regions = a.get("profile"), a.get("account", ""), a.get("regions", {})
            if not profile:
                continue
            r = subprocess.run(
                ["aliyun", "--profile", profile, "oss", "ls"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                failed()
                continue
            for line in r.stdout.splitlines():
                parts = line.split()
                # 数据行形如：<CreationTime…> <oss-region> <StorageClass> oss://<bucket>
                if not parts or not parts[-1].startswith("oss://") or len(parts) < 4:
                    continue
                bucket = parts[-1][len("oss://"):].rstrip("/")
                if not bucket:
                    continue
                oss_region = parts[-3]                                   # 如 oss-cn-shenzhen
                region_id = oss_region[4:] if oss_region.startswith("oss-") else oss_region
                disp = regions.get(region_id, (region_id.split("-")[-1].title(), self.flag))[0]
                out.append({
                    "name": bucket, "cloud": "aliyun", "account": account,
                    "region": region_id, "region_display": disp,
                    "domain": f"{bucket}.{oss_region}.aliyuncs.com",
                    "created": parts[0][:10],
                })
        out.sort(key=lambda s: s["name"])
        return out

    def list_domains(self):
        out = []
        now = datetime.now()
        audit = {"SUCCEED": "已实名", "FAILED": "实名失败", "SUBMITTED": "审核中", "NONAUDIT": "未实名"}
        for a in self._accounts():
            profile, account = a.get("profile"), a.get("account", "")
            if not profile:
                continue
            data = _ali_json(a, ["domain", "QueryDomainList", "--PageNum", "1", "--PageSize", "100"])
            if data is None:
                continue
            for x in data.get("Data", {}).get("Domain", []):
                dead = x.get("DeadDate")
                days = None
                if dead:
                    try:
                        days = (datetime.strptime(dead, "%Y-%m-%d %H:%M:%S") - now).days
                    except ValueError:
                        days = None
                out.append({
                    "domain": x.get("DomainName"), "type": x.get("DomainType"),
                    "registrar": "阿里云", "account": account,
                    "reg_date": (x.get("RegDate") or "")[:10], "expire_date": (dead or "")[:10],
                    "days_left": days, "note": audit.get(x.get("DomainAuditStatus"), x.get("DomainAuditStatus") or ""),
                })
        return out

    # —— RAM 用户（只读；账号级无区域，每用户并发查 AccessKey 年龄 + 控制台最后登录）——
    # 行结构与 AWS list_iam 一致。last_used 取 GetUser.LastLoginDate（控制台登录，对应 AWS 的
    # PasswordLastUsed）；从没登录过阿里云给空串 → "—"。key_count 数全部 AccessKey（含 Inactive，与 AWS 同款）。
    # ListUsers 按 Marker/IsTruncated 翻页（MaxItems 上限 1000，这里每页 100）。
    def _ram_users(self, a):
        """全部 RAM 用户（翻页合并）；ListUsers 本身失败 → None（_ali_json 已报过）。"""
        users, marker = [], None
        for _ in range(50):                                      # 翻页上限：别让服务端一直给同一个 Marker 把我们套死
            args = ["ram", "ListUsers", "--MaxItems", "100"]
            if marker:
                args += ["--Marker", marker]
            res = _ali_json(a, args)
            if res is None:
                return None if not users else users              # 后几页失败就先用拿到手的
            users.extend((res.get("Users") or {}).get("User") or [])
            marker = res.get("Marker")
            if not res.get("IsTruncated") or not marker:
                break
        return users

    @optional
    def _ram_user_detail(self, a, username):
        """单个用户：(密钥数, 最老密钥天数, 最后登录 YYYY-MM-DD 或 "—", 失败次数)。
        两个调用都 quiet——失败由 list_iam 汇总成每账号一行。"""
        failed = 0
        keys_res = _ali_json(a, ["ram", "ListAccessKeys", "--UserName", username], quiet=True)
        if keys_res is None:
            failed += 1
        keys = ((keys_res or {}).get("AccessKeys") or {}).get("AccessKey") or []
        ages = [d for d in (_days_since(k.get("CreateDate")) for k in keys) if d is not None]
        user_res = _ali_json(a, ["ram", "GetUser", "--UserName", username], quiet=True)
        if user_res is None:
            failed += 1
        last = ((user_res or {}).get("User") or {}).get("LastLoginDate") or ""
        return len(keys), (max(ages) if ages else None), (last[:10] if last else "—"), failed

    def list_iam(self):
        out = []
        for a in self._accounts():
            if not a.get("profile"):
                continue
            account = a.get("account", "")
            users = self._ram_users(a)
            if users is None:
                continue                                         # ListUsers 都不行（缺权限/profile 失效）→ 该账号为空
            with ThreadPoolExecutor(max_workers=8) as ex:
                details = list(ex.map(lambda u: self._ram_user_detail(a, u.get("UserName") or ""), users))
            failures = sum(d[3] for d in details)
            if failures:
                print(f"  ⚠️ 阿里云 {account} RAM：{failures} 次 ListAccessKeys/GetUser 失败"
                      f"（缺 ram:ListAccessKeys / ram:GetUser 权限？），相应用户密钥数按 0、登录按 — 计", file=sys.stderr)
            for u, (kc, oldest, last, _) in zip(users, details):
                out.append({
                    "name": u.get("UserName"), "cloud": "aliyun", "account": account,
                    "created": (u.get("CreateDate") or "")[:10],
                    "last_used": last, "key_count": kc, "oldest_key_days": oldest,
                })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    # —— 数据库（只读；RDS 实例 + PolarDB 集群，按账号 × 配置里的 regions 并发扫）——
    # 行结构与 AWS list_databases 一致，state 统一小写（Running → running，看板 pill 认 running）。
    # RDS：DescribeDBInstances 列表里**没有**存储大小和端口，要再按实例批量 DescribeDBInstanceAttribute
    #      （--DBInstanceId 逗号拼最多 30 个）取 DBInstanceStorage / ConnectionString / Port。
    # PolarDB：DescribeDBClusters 给 StorageUsed（字节，存储按量弹性，显示"已用"）；连接地址要
    #      DescribeDBClusterEndpoints，取 Cluster 地址（优先内网 Private）。
    # 两者翻页都是 PageNumber/PageSize（上限 100），按 TotalRecordCount 收口。
    _PAGE = "100"

    def _ali_pages(self, a, args, item_key, quiet):
        """PageNumber/PageSize 翻页取全 Items.<item_key>[]；首页失败 → None。"""
        items, page = [], 1
        for _ in range(50):
            res = _ali_json(a, [*args, "--PageSize", self._PAGE, "--PageNumber", str(page)], quiet=quiet)
            if res is None:
                return None if page == 1 else items
            got = (res.get("Items") or {}).get(item_key) or []
            items.extend(got)
            total = res.get("TotalRecordCount")
            if not got or not isinstance(total, int) or len(items) >= total:
                break
            page += 1
        return items

    def _rds_in(self, a, region, quiet):
        """某账号某区域的 RDS 实例行；返回 (rows, 列表调用是否失败, 明细调用失败次数)。"""
        account, (disp, flag) = a.get("account", ""), a.get("regions", {}).get(region, (region, self.flag))
        insts = self._ali_pages(a, ["rds", "DescribeDBInstances", "--RegionId", region], "DBInstance", quiet)
        if insts is None:
            return [], True, 0
        # 补属性（存储 / 端口 / 连接串）：30 个一批
        attrs, failed = {}, 0
        ids = [i.get("DBInstanceId") for i in insts if i.get("DBInstanceId")]
        for n in range(0, len(ids), 30):
            res = _ali_json(a, ["rds", "DescribeDBInstanceAttribute", "--DBInstanceId", ",".join(ids[n:n + 30]),
                                "--RegionId", region], quiet=True)
            if res is None:
                failed += 1
                continue
            for x in (res.get("Items") or {}).get("DBInstanceAttribute") or []:
                attrs[x.get("DBInstanceId")] = x
        rows = []
        for i in insts:
            at = attrs.get(i.get("DBInstanceId"), {})
            host = at.get("ConnectionString") or i.get("ConnectionString") or ""
            port = at.get("Port") or ""
            stype = at.get("DBInstanceStorageType") or i.get("DBInstanceStorageType") or ""
            storage = _gb(at.get("DBInstanceStorage"))
            rows.append({
                "name": i.get("DBInstanceDescription") or i.get("DBInstanceId"), "id": i.get("DBInstanceId"),
                "cloud": "aliyun", "account": account,
                "region": region, "region_display": disp, "flag": flag,
                "engine": f'{i.get("Engine") or ""} {i.get("EngineVersion") or at.get("EngineVersion") or ""}'.strip(),
                "class": at.get("DBInstanceClass") or i.get("DBInstanceClass") or "—",
                "storage": f"{storage} {stype}".strip() if storage != "—" else "—",
                "state": (i.get("DBInstanceStatus") or "").lower() or "—",
                "endpoint": f"{host}:{port}" if host and port else (host or "—"),
                "created": (i.get("CreateTime") or "")[:10],
            })
        return rows, False, failed

    def _polardb_in(self, a, region, quiet):
        """某账号某区域的 PolarDB 集群行；返回 (rows, 列表调用是否失败, 明细调用失败次数)。"""
        account, (disp, flag) = a.get("account", ""), a.get("regions", {}).get(region, (region, self.flag))
        clusters = self._ali_pages(a, ["polardb", "DescribeDBClusters", "--RegionId", region], "DBCluster", quiet)
        if clusters is None:
            return [], True, 0
        rows, failed = [], 0
        for c in clusters:
            cid = c.get("DBClusterId")
            endpoint = "—"
            res = _ali_json(a, ["polardb", "DescribeDBClusterEndpoints", "--DBClusterId", cid, "--RegionId", region],
                            quiet=True) if cid else None
            if res is None:
                failed += 1
            else:
                # 先 Cluster 地址再 Primary；同类里内网优先（Private > 其它 > Public）
                cands = []
                for ep in res.get("Items") or []:
                    etype = ep.get("EndpointType") or ""
                    for ad in ep.get("AddressItems") or []:
                        if ad.get("ConnectionString"):
                            cands.append(({"Cluster": 0, "Primary": 1}.get(etype, 2),
                                          {"Private": 0, "Public": 2}.get(ad.get("NetType"), 1), ad))
                if cands:
                    ad = min(cands, key=lambda t: t[:2])[2]
                    endpoint = f'{ad["ConnectionString"]}:{ad.get("Port") or ""}'.rstrip(":")
            nodes = c.get("DBNodeNumber") or len((c.get("DBNodes") or {}).get("DBNode") or [])
            klass = c.get("DBNodeClass") or ((c.get("DBNodes") or {}).get("DBNode") or [{}])[0].get("DBNodeClass") or "—"
            used = _gb(c.get("StorageUsed"), unit=2 ** 30)
            rows.append({
                "name": c.get("DBClusterDescription") or cid, "id": cid,
                "cloud": "aliyun", "account": account,
                "region": region, "region_display": disp, "flag": flag,
                "engine": f'PolarDB {c.get("DBType") or ""} {c.get("DBVersion") or ""}'.strip(),
                "class": f"{klass} ×{nodes}" if nodes and nodes > 1 else klass,
                "storage": f"{used} 已用" if used != "—" else "—",
                "state": (c.get("DBClusterStatus") or "").lower() or "—",
                "endpoint": endpoint,
                "created": (c.get("CreateTime") or "")[:10],
            })
        return rows, False, failed

    def list_databases(self):
        tasks = [(a, r) for a in self._accounts() if a.get("profile") for r in a.get("regions", {})]
        out, stats = [], {}                       # stats[account] = [列表调用已响亮报过一次, 静默失败次数]

        def scan(a, region):
            # 每账号：第一次列表调用失败让 _ali_json 打出原因，之后同账号一律静默，最后汇总成一行
            st = stats.setdefault(a.get("account", ""), [False, 0])
            rows = []
            for fn in (self._rds_in, self._polardb_in):
                r, list_failed, detail_failed = fn(a, region, st[0])
                rows += r
                if list_failed:
                    if st[0]:
                        st[1] += 1
                    st[0] = True
                st[1] += detail_failed
            return rows

        with ThreadPoolExecutor(max_workers=8) as ex:
            for rows in ex.map(lambda t: scan(*t), tasks):
                out.extend(rows)
        for account, (_, silent) in stats.items():
            if silent:
                print(f"  ⚠️ 阿里云 {account} 数据库：{silent} 次 RDS/PolarDB Describe 调用失败"
                      f"（缺 rds:Describe* / polardb:Describe* 权限？），该账号数据库行可能不全", file=sys.stderr)
        out.sort(key=lambda d: (d["account"], d["region"], d["name"] or ""))
        return out

    # —— 成本（BssOpenApi QueryBillOverview：账号 × 产品 × 月）——
    # 不计费、不限频，可随手 `--kind=billing`。金额取 PretaxAmount（优惠后应付，含欠费部分），
    # 不取 CashAmount（只算已从现金余额扣掉的——后付费未出账时为 0，会把本月花费漏成 0）。
    # 后付费（百炼 token 等）在当月出账前显示在 OutstandingAmount，同样计入 PretaxAmount。
    def list_billing(self, months=3):
        today = datetime.now(timezone.utc).date()
        y, m = today.year, today.month - (months - 1)
        while m <= 0:
            m += 12
            y -= 1
        periods = []
        for _ in range(months):
            periods.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
        cur_month = today.strftime("%Y-%m")
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in self._accounts():
            if not a.get("profile"):
                continue
            account = a.get("account", "")
            for period in periods:
                res = _ali_json(a, ["bssopenapi", "QueryBillOverview", "--BillingCycle", period])
                if res is None:
                    continue
                for b in ((res.get("Data") or {}).get("Items") or {}).get("Item") or []:
                    amt = _amount(b.get("PretaxAmount"))
                    if amt <= 0:
                        continue                   # 0 元（免费额度内 / 四舍五入抹零）不出行
                    out.append({
                        "cloud": "aliyun", "account": account,
                        "service": b.get("ProductName") or b.get("ProductCode") or "Other",
                        "month": period, "amount": round(amt, 2),
                        "currency": b.get("Currency") or "CNY",
                        "period_start": f"{period}-01", "partial": period == cur_month,
                        "fetched_at": fetched,
                    })
        out.sort(key=lambda x: (x["account"], x["month"], -x["amount"]))
        return out

    # —— 账务 / 额度（QueryAccountBalance：可用余额 · 信控额度）——
    # 阿里云是「余额 + 后付费」模型（与火山同款）：AvailableAmount 为可用余额（含现金 + 信控），
    # QuotaLimit 为信控额度——0 时余额用尽即欠费、宽限期后停服 → spending_limit="On"。
    # credit 列放**可用余额**（充值进去的钱），代金券/储值卡（QueryCashCoupons / QueryPrepaidCards）不并入。
    # 预算字段未由此接口提供；budget=None，由渲染层显示未提供状态。
    def list_credits(self):
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in self._accounts():
            if not a.get("profile"):
                continue
            account = a.get("account", "")
            res = _ali_json(a, ["bssopenapi", "QueryAccountBalance"])
            if res is None:
                continue                           # 连余额都查不到（缺权限/profile 失效）→ 不出行，别造假行
            bal = res.get("Data") or {}
            available, quota = _amount(bal.get("AvailableAmount")), _amount(bal.get("QuotaLimit"))
            out.append({
                "cloud": "aliyun", "account": account,
                "account_name": f"阿里云 {a.get('profile')}",
                "plan": "Balance+PostPaid",
                "state": "Arrears" if available < 0 else "Normal",   # 欠费时可用余额为负
                "credit": round(available, 2), "credit_currency": bal.get("Currency") or "CNY",
                "free_tier": None, "free_tier_end": None, "free_tier_days": None,
                "spending_limit": "On" if quota <= 0 else "Off",
                "budget": None,
                "currency": bal.get("Currency") or "CNY",
                "fetched_at": fetched,
            })
        return out

    # —— 函数计算 FC 3.0（只读；`aliyun fc ListFunctions`，RESTful 风格、nextToken 翻页，按账号 × regions）——
    def _fc_in(self, a, region, quiet):
        """某账号某区域的 FC 函数行；返回 (rows, 列表调用是否失败)。"""
        profile, account = a.get("profile"), a.get("account", "")
        disp, flag = a.get("regions", {}).get(region, (region, self.flag))
        rows, token = [], None
        for _ in range(50):
            args = ["fc", "ListFunctions", "--region", region, "--limit", "100"]
            if token:
                args += ["--nextToken", token]
            res = _ali_json(a, args, quiet=quiet)
            if res is None:
                return rows, not rows
            for f in res.get("functions") or []:
                size = f.get("codeSize")
                rows.append({
                    "name": f.get("functionName"), "id": f.get("functionId") or f.get("functionArn") or "",
                    "cloud": "aliyun", "account": account,
                    "region": region, "region_display": disp, "flag": flag,
                    "runtime": f.get("runtime") or "", "memory": f.get("memorySize"),
                    "timeout": f.get("timeout"),
                    "code_mb": round(size / 1e6, 1) if isinstance(size, (int, float)) and size else "",
                    "last_modified": str(f.get("lastModifiedTime") or "")[:10],
                    "note": f.get("description") or "",
                })
            token = res.get("nextToken") or None
            if not token:
                break
        return rows, False

    def list_serverless(self):
        tasks = [(a, r) for a in self._accounts() if a.get("profile") for r in a.get("regions", {})]
        out, stats = [], {}

        def scan(a, region):
            st = stats.setdefault(a.get("account", ""), [False, 0])
            rows, failed = self._fc_in(a, region, st[0])
            if failed:
                if st[0]:
                    st[1] += 1
                st[0] = True
            return rows

        with ThreadPoolExecutor(max_workers=8) as ex:
            for rows in ex.map(lambda t: scan(*t), tasks):
                out.extend(rows)
        for account, (_, silent) in stats.items():
            if silent:
                print(f"  ⚠️ 阿里云 {account}：另有 {silent} 个区域的 FC ListFunctions 失败（未开通/缺权限？）", file=sys.stderr)
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— CDN 加速域名（只读；`aliyun cdn DescribeUserDomains`）——
    # CDN 是**全局**服务：不按 regions 遍历，按账号 PageNumber/PageSize 翻页（PageSize 上限 500，取 50 够用）。
    # 响应形如 {Domains: {PageData: [...]}, TotalCount}（与 RDS 的 Items.<key> 不同，故不复用 _ali_pages）。
    # 阿里云加速域名没有独立资源 ID（DomainId 是内部整数），id 直接用域名。
    _CDN_PAGE = "50"

    def _cdn_domains(self, a):
        """某账号全部加速域名（翻页取全）；首页失败 → []（原因已由 _ali_json 打一行：未开通 CDN / 缺权限）。"""
        items, page = [], 1
        for _ in range(50):
            res = _ali_json(a, ["cdn", "DescribeUserDomains", "--PageSize", self._CDN_PAGE, "--PageNumber", str(page)])
            if res is None:
                break                                                  # 已拿到的页照常进表
            got = (res.get("Domains") or {}).get("PageData") or []
            items.extend(got)
            total = res.get("TotalCount")
            if not got or not isinstance(total, int) or len(items) >= total:
                break
            page += 1
        return items

    @optional
    def _cdn_cert(self, a, d):
        """证书列文案：SslProtocol=on → "HTTPS 开"，再尽量补证书到期日（DescribeDomainCertificateInfo，拿不到就算了，静默）。"""
        if (d.get("SslProtocol") or "").lower() != "on":
            return "无 HTTPS"
        res = _ali_json(a, ["cdn", "DescribeDomainCertificateInfo", "--DomainName", d.get("DomainName") or ""], quiet=True)
        infos = ((res or {}).get("CertInfos") or {}).get("CertInfo") or []
        exp = str((infos[0] if infos else {}).get("CertExpireTime") or "")[:10]
        return f"HTTPS 开 · 到期 {exp}" if exp else "HTTPS 开"

    def list_cdn(self):
        out = []
        for a in self._accounts():
            if not a.get("profile"):
                continue
            account = a.get("account", "")
            for d in self._cdn_domains(a):
                status = d.get("DomainStatus") or ""
                sources = (d.get("Sources") or {}).get("Source") or []
                out.append({
                    "id": d.get("DomainName") or "", "domain": d.get("DomainName") or "", "aliases": "",
                    "origin": ", ".join(s.get("Content") for s in sources if s.get("Content")),
                    "status": status, "enabled": status == "online",
                    "cert": self._cdn_cert(a, d),
                    "price_class": d.get("CdnType") or "",                 # web / download / video（业务类型，当「档」展示）
                    "last_modified": str(d.get("GmtModified") or "")[:10],
                    "account": account, "cloud": "aliyun", "partition": "cn",
                    "comment": d.get("Description") or "",
                })
        out.sort(key=lambda x: (x["account"], x["domain"]))
        return out
