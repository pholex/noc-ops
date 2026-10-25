"""火山引擎驱动（ECS / DNS / veFaaS / CDN / IAM，shell 出官方 CLI `ve`）。

展示（只读）：list_instances（ECS 云服务器）/ list_domains（云解析 DNS 的 zone）/ list_serverless（veFaaS 函数）
/ list_cdn（CDN 加速域名，`cdn ListCdnDomains`，未开通 → ServiceStopped 按未纳管处理）
/ list_iam（IAM 用户）/ list_billing（月度账单按产品，`billing ListBillOverviewByProd`，不按请求计费）
/ list_credits（账户余额 `billing QueryBalanceAcct` + 预算 `billing ListBudget`）。生命周期：reboot_argv（`ecs RebootInstance`，免确认自动跑）/ status_argv；
启停等经通用 `ve` 通道走"确认后执行"（is_read/is_reboot 分类器交给 agent/MCP 闸门）。
账号：见 ops-config.toml 的 [[volcengine.accounts]]（account=主账号 ID，可选 regions、ssh_user/ssh_key）。
认证沿用本机 `ve configure` 的**当前** profile（~/.volcengine/config.json），noc 不碰任何密钥。
⚠️ `ve` 没有 `--profile` 全局参数（切 profile 是有状态的 `ve configure profile <名>`），所以目前只支持一个账号；
   要第二个账号得等 ve 支持按命令指定 profile，或用环境变量注入 AK（未验证）。
坑：① `ve` 的 API 级错误也走 exit 0 + ResponseMetadata.Error（如 CDN 未开通 OperationDenied.ServiceStopped），
   要单独判；② 列表参数是 `--InstanceIds.1 <id>` 点号下标写法；③ 部分服务（dns / vefaas）参数走 `--body '<json>'`；
④ 对象存储 TOS 不在 `ve` 里：list_storage 走官方 `tosutil ls`（单独配置 ~/.tosutilconfig，同样 noc 不碰密钥）；
⑤ 云数据库：list_databases 走 `rdsmysqlv2` / `rdspostgresql` 的 DescribeDBInstances（`rdsmysql` 老版没有列表接口）。
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

from ..collection import observed
from .. import deploy_config
from ..aws_safety import is_read_only_ve, is_reboot_ve
from .base import Provider, resolve_keyfile

_FLAG = "🇨🇳"


@observed(result="nullable")
def _ve_json(account, args, timeout=60, quiet=False):
    """跑一条 `ve <args>`，解析 JSON，返回 Result（dict）。失败返回 None 并**打一行诊断到 stderr**（quiet=True 不打，由调用方汇总）。

    三态：成功有数据 / 成功空（返回 {} 或空列表的 Result）/ 失败（exit≠0、非 JSON、或 ResponseMetadata.Error）→ None。
    """
    acct = account.get("account", "?")
    action = " ".join(a for a in args[:2] if not a.startswith("-"))          # 如 "ecs DescribeInstances"
    cmd = ["ve", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        if not quiet:
            print(f"  ⚠️ 火山引擎 {acct} {action}：跑 ve 失败（{e}）", file=sys.stderr)
        return None
    data = None
    if r.stdout.strip():
        try:
            data = json.loads(r.stdout[r.stdout.index("{"):]) if "{" in r.stdout else None
        except (json.JSONDecodeError, ValueError):
            data = None
    # API 级错误先看：stdout 里有 ResponseMetadata.Error 就按错误码报（带「未开通」提示）。
    # ⚠️ 它**不一定** exit 0——如 CDN 未开通的 OperationDenied.ServiceStopped 实测 exit 1（2026-10-25），
    # 若先按退出码一刀切，错误码就被埋进「ve 失败（exit 1）」里，调用方无从区分「未开通」与「AK 过期」。
    err = (data.get("ResponseMetadata") or {}).get("Error") if isinstance(data, dict) else None
    if err:
        code = err.get("Code") or "?"
        hint = "（该服务未开通/已停用，按未纳管处理）" if "ServiceStopped" in code or "NotOpen" in code else ""
        if not quiet:
            print(f"  ⚠️ 火山引擎 {acct} {action}：{code} {err.get('Message') or ''}{hint}".rstrip(), file=sys.stderr)
        return None
    if r.returncode != 0:                     # 与 _gcloud/_aws_json 同款：退出码非 0 一律算失败，不管 stdout 长啥样
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        if not quiet:
          print(f"  ⚠️ 火山引擎 {acct} {action}：ve 失败（exit {r.returncode}）——{tail}"
              + ("（未配置 ve profile？跑 `ve configure`）" if "profile" in tail.lower() or "credential" in tail.lower() else ""),
              file=sys.stderr)
        return None
    if not isinstance(data, dict):
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        if not quiet:
          print(f"  ⚠️ 火山引擎 {acct} {action}：ve 失败——{tail}"
              + ("（未配置 ve profile？跑 `ve configure`）" if "profile" in tail.lower() or "credential" in tail.lower() else ""),
              file=sys.stderr)
        return None
    res = data.get("Result")
    return res if isinstance(res, dict) else {}


def _date(v):
    """火山各服务时间格式不一：ISO（2026-03-12T02:30:43Z）/ 紧凑（20260312T023043Z）/ 秒级时间戳 → YYYY-MM-DD。"""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(v)
    if re.fullmatch(r"\d{8}T\d{6}Z?", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


class VolcengineProvider(Provider):
    cloud = "volcengine"
    cli = "ve"
    display = "火山引擎"
    flag = _FLAG
    order = 3                      # AWS=0 阿里=1 腾讯=2 火山=3 GCP=4 Azure=5 Oracle=6

    is_read = staticmethod(is_read_only_ve)
    is_reboot = staticmethod(is_reboot_ve)

    @staticmethod
    def _accounts():
        return deploy_config.cloud_accounts("volcengine")

    def reboot_argv(self, inst):
        # ECS RebootInstance = 优雅重启（OS 内 reboot，不带 ForceStop）；按实例 ID + region（ve 无 --profile，用当前 profile）
        return ["ve", "ecs", "RebootInstance", "--InstanceId", inst["id"], "--region", inst["region"]]

    def status_argv(self, inst):
        return ["ve", "ecs", "DescribeInstances", "--InstanceIds.1", inst["id"], "--region", inst["region"]]

    # —— ECS 云服务器 ——
    def list_instances(self, network=None):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            kf = resolve_keyfile(account, a.get("ssh_key")) if a.get("ssh_key") else None
            for region, (disp, flag) in a.get("regions", {}).items():
                token = None
                while True:
                    args = ["ecs", "DescribeInstances", "--region", region, "--MaxResults", "100"]
                    if token:
                        args += ["--NextToken", token]
                    res = _ve_json(a, args)
                    if res is None:
                        break
                    for i in res.get("Instances") or []:
                        nics = i.get("NetworkInterfaces") or []
                        primary = next((n for n in nics if (n.get("Type") or "").lower() == "primary"), nics[0] if nics else {})
                        name = i.get("InstanceName") or i.get("InstanceId")
                        mem = i.get("MemorySize")
                        out.append({
                            "name": name, "id": i.get("InstanceId"),
                            "profile": account, "region": region, "region_display": disp, "flag": flag,
                            "type": i.get("InstanceTypeId") or "ECS",
                            "state": (i.get("Status") or "").lower(),         # RUNNING → running
                            "az": i.get("ZoneId"),
                            "private_ip": primary.get("PrimaryIpAddress"),
                            "public_ip": (i.get("EipAddress") or {}).get("IpAddress"),
                            "key_name": i.get("KeyPairName") or a.get("ssh_key"),
                            "ssh_alias": "ve-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name),
                            "ssh_user": a.get("ssh_user"), "key_file": kf or "",
                            "connect": "direct", "proxyjump": None,
                            # verified 专指"已真实 SSH 探测"；非 AWS 云 sync 不探测，统一 False（UI 显示为"推断"）
                            "verified": False, "cloud": "volcengine",
                            "cpu_mem": f"{i.get('Cpus')}C{int(mem) // 1024}G" if i.get("Cpus") and mem else "",
                        })
                    token = res.get("NextToken") or None
                    if not token:
                        break
        return out

    # —— 云解析 DNS 的 zone（进「域名」tab，与 Cloudflare zone 同款：无注册到期）——
    def list_domains(self):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            for z in self._paged(a, ["dns", "ListZones"], "Zones"):
                rc = z.get("RecordCount")
                out.append({
                    "domain": z.get("ZoneName"), "type": "DNS zone", "registrar": "火山引擎",
                    "account": account, "reg_date": _date(z.get("CreatedAt")),
                    "expire_date": "", "days_left": None,            # DNS zone 无注册到期（域名注册另在域名服务，暂未纳管）
                    "note": (f"{rc} 条记录" if rc is not None else "") + ("（子域）" if z.get("IsSubDomain") else ""),
                })
        return out

    @staticmethod
    def _paged(account, args, items_key, page_size=100, page_key="PageNumber"):
        """走 `--body` 的 PageNumber/PageSize 分页接口（dns ListZones / vefaas ListFunctions）→ 全部条目。
        按 Total 或「本页不足一页」收口；某页失败即停（已拿到的照常返回）。
        page_key：页码字段名——多数服务叫 PageNumber，CDN 的 ListCdnDomains 叫 PageNum。"""
        out, page = [], 1
        while True:
            res = _ve_json(account, [*args, "--body", json.dumps({"PageSize": page_size, page_key: page})])
            if res is None:
                break
            items = res.get(items_key) or []
            out.extend(items)
            total = res.get("Total")
            if len(items) < page_size or (isinstance(total, int) and len(out) >= total):
                break
            page += 1
        return out

    # —— veFaaS 函数 ——
    def list_serverless(self):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            for region, (disp, flag) in a.get("regions", {}).items():
                for f in self._paged(a, ["vefaas", "ListFunctions", "--region", region], "Items"):
                    size = f.get("CodeSize")
                    out.append({
                        "name": f.get("Name"), "id": f.get("Id"), "cloud": "volcengine", "account": account,
                        "region": region, "region_display": disp, "flag": flag,
                        "runtime": f.get("Runtime") or "", "memory": f.get("MemoryMB"),
                        "timeout": f.get("RequestTimeout"),
                        "code_mb": round(size / 1e6, 1) if isinstance(size, (int, float)) and size else "",
                        "last_modified": _date(f.get("LastUpdateTime")),
                    })
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— 对象存储 TOS（官方 tosutil；`ve` 不覆盖 TOS）——
    @staticmethod
    @observed(result="pair")
    def _tosutil_ls(timeout=60):
        """跑 `tosutil ls`（列桶），返回 (ok, 文本)。tosutil 未装 → (False, "missing")；未配置 → (False, "unconfigured")。"""
        try:
            r = subprocess.run(["tosutil", "ls"], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return False, "missing"
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        text = (r.stdout or "") + (r.stderr or "")
        if "Please set" in text and "configuration file" in text:
            return False, "unconfigured"
        if r.returncode != 0:
            return False, text.strip().splitlines()[0][:160] if text.strip() else f"exit {r.returncode}"
        return True, r.stdout

    @staticmethod
    def _parse_tosutil_ls(text):
        """表格行形如 `tos://name   2026-10-25T04:50:37Z   cn-beijing   fns` → dict 列表。"""
        out = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].startswith("tos://"):
                out.append({"name": parts[0][len("tos://"):], "created": parts[1], "location": parts[2],
                            "bucket_type": parts[3] if len(parts) > 3 else ""})
        return out

    def list_storage(self):
        accounts = self._accounts()
        if not accounts:
            return []
        ok, text = self._tosutil_ls()
        if not ok:
            hint = {"missing": "未安装 tosutil（火山引擎 TOS 官方工具，`ve` 不覆盖 TOS），TOS 桶不纳管",
                    "unconfigured": "tosutil 未配置（`tosutil config -interactive` 填 endpoint/region/AK/SK），TOS 桶不纳管"}.get(text, f"tosutil ls 失败——{text}")
            print(f"  ⚠️ 火山引擎 TOS：{hint}", file=sys.stderr)
            return []
        a = accounts[0]                       # ve / tosutil 都只有一个当前身份；桶归到配置里的第一个账号
        account, regions = a.get("account", ""), a.get("regions", {})
        out = []
        for b in self._parse_tosutil_ls(text):
            loc = b["location"]
            disp = regions.get(loc, (loc, self.flag))[0]
            out.append({
                "name": b["name"], "cloud": "volcengine", "account": account,
                "region": loc, "region_display": disp, "flag": self.flag,
                "domain": f"{b['name']}.tos-{loc}.volces.com",
                "created": _date(b["created"]),
                "bucket_type": b["bucket_type"],
            })
        out.sort(key=lambda x: (x["region"], x["name"]))
        return out

    # —— 云数据库（RDS MySQL v2 + PostgreSQL，按账号 × regions；老 `rdsmysql` 服务没有列表接口）——
    _DB_SERVICES = (("rdsmysqlv2", "MySQL"), ("rdspostgresql", "PostgreSQL"))

    def _db_pages(self, a, service, region):
        """DescribeDBInstances 按 PageNumber/PageSize 翻页取全 Instances[]；首页失败 → None。"""
        items, page = [], 1
        for _ in range(50):
            # 分页参数要走 --body（JSON 里是整数）；用 --PageNumber 1 这种 flag 写法会被当成字符串拒掉（参数值无效）
            res = _ve_json(a, [service, "DescribeDBInstances", "--body",
                               json.dumps({"RegionId": region, "PageNumber": page, "PageSize": 100})], quiet=True)
            if res is None:
                return None if page == 1 else items
            got = res.get("Instances") or res.get("InstancesInfo") or []
            items.extend(got)
            total = res.get("Total")
            if not got or not isinstance(total, int) or len(items) >= total:
                break
            page += 1
        return items

    def list_databases(self):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            failed = []                                   # (service, region)：该区域没开 RDS 时接口直接报参数错，按账号汇总一行
            for region, (disp, flag) in a.get("regions", {}).items():
                for service, engine in self._DB_SERVICES:
                    insts = self._db_pages(a, service, region)
                    if insts is None:
                        failed.append(f"{service}@{region}")
                        continue
                    for i in insts:
                        storage = i.get("StorageSpace") or i.get("StorageSpaceGB")
                        st_type = (i.get("StorageType") or "").replace("LocalSSD", "本地 SSD")
                        out.append({
                            "name": i.get("InstanceName") or i.get("InstanceId"), "id": i.get("InstanceId"),
                            "cloud": "volcengine", "account": account,
                            "region": region, "region_display": disp, "flag": flag,
                            "engine": f"{engine} {i.get('DBEngineVersion') or ''}".replace("MySQL_", "").replace("PostgreSQL_", "").strip(),
                            "class": i.get("NodeSpec") or "—",
                            "storage": (f"{storage} GB {st_type}".strip() if storage else "—"),
                            "state": (i.get("InstanceStatus") or "").lower() or "unknown",
                            "endpoint": "—",                      # 连接地址在 DescribeDBInstanceDetail，按需再补
                            "created": _date(i.get("CreateTime")),
                        })
            if failed:
                print(f"  ⚠️ 火山引擎 {account}：{len(failed)} 个 区域×引擎 的 RDS DescribeDBInstances 失败（该区域未开通 RDS？）：{', '.join(failed)}", file=sys.stderr)
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— 成本（月度账单按产品；与 AWS Cost Explorer 行结构一致，币种 CNY 与 aws-cn 同款、前端按币种分列合计）——
    # ListBillOverviewByProd 不按请求计费，但全量 sync 仍走 sync._billing_with_cache 的 12h 缓存（整类一起缓存）。
    # 金额取 PayableAmount（应付=折扣/代金券后实际要付的），不取 OriginalBillAmount（原价）。
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
            account = a.get("account", "")
            for period in periods:
                offset = 0
                while True:
                    res = _ve_json(a, ["billing", "ListBillOverviewByProd", "--body",
                                       json.dumps({"BillPeriod": period, "Limit": 100, "Offset": offset,
                                                   "IgnoreZero": 1, "NeedRecordNum": 1})])
                    if res is None:
                        break
                    items = res.get("List") or []
                    for b in items:
                        try:
                            amt = float(b.get("PayableAmount") or 0)
                        except ValueError:
                            amt = 0.0
                        if amt <= 0:
                            continue
                        svc = b.get("ProductZh") or b.get("Product") or "Other"
                        out.append({
                            "cloud": "volcengine", "account": account, "service": svc,
                            "month": period, "amount": round(amt, 2),
                            "currency": b.get("Currency") or "CNY",
                            "period_start": f"{period}-01", "partial": period == cur_month,
                            "fetched_at": fetched,
                        })
                    offset += len(items)
                    total = res.get("Total")
                    if len(items) < 100 or (isinstance(total, int) and offset >= total):
                        break
        out.sort(key=lambda x: (x["account"], x["month"], -x["amount"]))
        return out

    # —— 账务 / 额度（账户余额 · 欠费 · 信用额度 · 预算）——
    # 火山是「余额 + 后付费」模型：余额用尽即欠费，信用额度 0 时欠费会停服（有宽限期）→ spending_limit="On"。
    # credit 列放**可用余额**（现金余额，充值进去的钱），不是赠金；代金券 ListCoupons 没有剩余金额字段，不并入。
    @staticmethod
    def _summarize_budgets(res):
        """`billing ListBudget` → 一句话。None（失败）→ None；空列表 → ""（渲染层标「未设」）。"""
        if res is None:
            return None
        parts = []
        for b in res.get("BudgetList") or res.get("List") or []:      # 实测键名是 BudgetList（2026-10-24）
            name = b.get("BudgetName") or b.get("Name") or "预算"
            amt = b.get("BudgetAmount") or b.get("Amount")
            parts.append(f"{name} {amt}" if amt not in (None, "") else name)
        return "；".join(parts)

    def list_credits(self):
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            bal = _ve_json(a, ["billing", "QueryBalanceAcct"])
            if bal is None:
                continue                                   # 连余额都查不到（缺权限/未登录）→ 该账号不出行，别造假行

            def num(k):
                try:
                    return float(bal.get(k) or 0)
                except ValueError:
                    return 0.0
            arrears, credit_limit = num("ArrearsBalance"), num("CreditLimit")
            budget = self._summarize_budgets(_ve_json(a, ["billing", "ListBudget", "--body", json.dumps({"Limit": 50})]))
            out.append({
                "cloud": "volcengine", "account": account,
                "account_name": f"火山引擎 {account}",
                "plan": "Balance+PostPaid",                 # 余额 + 后付费（按量先用后扣余额）
                "state": "Arrears" if arrears > 0 else "Normal",
                "credit": num("AvailableBalance"), "credit_currency": bal.get("Currency") or "CNY",
                "free_tier": None, "free_tier_end": None, "free_tier_days": None,
                # 信用额度 0 → 余额用尽即欠费停服（宽限期后）；有信用额度则可先欠着 → 视为超额继续扣
                "spending_limit": "On" if credit_limit <= 0 else "Off",
                "budget": budget,
                "currency": bal.get("Currency") or "CNY",
                "fetched_at": fetched,
            })
        return out

    # —— IAM 用户 ——
    def list_iam(self):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            offset = 0
            while True:
                res = _ve_json(a, ["iam", "ListUsers", "--Limit", "100", "--Offset", str(offset)])
                if res is None:
                    break
                users = res.get("UserMetadata") or []
                for u in users:
                    bits = [b for b in (u.get("DisplayName"), u.get("Email"), u.get("Status")) if b]
                    out.append({
                        "name": u.get("UserName"), "cloud": "volcengine", "account": account,
                        "created": _date(u.get("CreateDate")), "last_used": "—",
                        "key_count": None, "oldest_key_days": None,     # IAM AK 明细暂未拉（ListAccessKeys 逐用户），先 —
                        "detail": " · ".join(bits),
                    })
                offset += len(users)
                if not users or offset >= int(res.get("Total") or 0):
                    break
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    # —— CDN 加速域名（只读；`cdn ListCdnDomains --body '{"PageNum":n,"PageSize":100}'`，全局服务不带 --region）——
    # 分页**只认 --body JSON**（页码字段叫 PageNum，不是 PageNumber；写成 --PageNum 旗标会被当字符串拒掉）。
    # 响应 Result: {Data: [{Domain, Cname, Status, ServiceType, HTTPS(bool), PrimaryOrigin[], BackupOrigin[], UpdateTime, ...}], Total}。
    # 未开通 CDN → OperationDenied.ServiceStopped（实测 exit 1 + stdout JSON），_ve_json 打一行「按未纳管处理」、这里得空列表。
    def list_cdn(self):
        out = []
        for a in self._accounts():
            account = a.get("account", "")
            for d in self._paged(a, ["cdn", "ListCdnDomains"], "Data", page_key="PageNum"):
                status = d.get("Status") or ""
                origins = []
                for v in (d.get("PrimaryOrigin"), d.get("BackupOrigin")):       # 文档给的是字符串数组，防一手单串
                    origins.extend([v] if isinstance(v, str) else list(v or []))
                https = d.get("HTTPS")
                if https is None:
                    https = d.get("HTTPSSwitch") or d.get("HttpsSwitch")
                out.append({
                    "id": d.get("Domain") or "", "domain": d.get("Domain") or "", "aliases": "",   # 无独立资源 ID，用域名
                    "origin": ", ".join(str(o) for o in origins),
                    "status": status, "enabled": status == "online",
                    "cert": "—" if https is None else ("HTTPS 开" if https in (True, "on", "On", "ON") else "无 HTTPS"),
                    "price_class": d.get("ServiceType") or "",             # web / download / video
                    "last_modified": _date(d.get("UpdateTime")),
                    "account": account, "cloud": "volcengine", "partition": "cn", "comment": "",
                })
        out.sort(key=lambda x: (x["account"], x["domain"]))
        return out
