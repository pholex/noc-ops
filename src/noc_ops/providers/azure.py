"""Microsoft Azure 驱动（Compute / Storage / DNS / Database / Functions / Cost / IAM / CDN，shell 出 az CLI）。

展示（只读）：list_instances（虚拟机 VM）/ list_storage（存储账户 Blob）/ list_domains（Azure DNS
zone，并入「域名」tab）/ list_databases（PostgreSQL·MySQL flexible-server + SQL Server 逻辑服务器）/
list_serverless（Function App）/ list_credits（账务·额度：信用余额 / 免费额度到期 /
超额保护 / 预算告警，见该方法注释里的 API scope 坑）/ list_billing（近 N 月按服务成本）/
list_iam（RBAC 角色分配按主体聚合）/ list_cdn（Front Door 与经典 CDN 端点）。
生命周期：reboot_argv（`az vm restart` 优雅重启，免确认自动跑）/ status_argv；启停经通用 az 通道走
"确认后执行"（is_read/is_reboot 分类器交给 agent/MCP 闸门）。

多订阅：见 ops-config.toml 的 [[azure.accounts]]（每个含 subscription=订阅 id）。
Azure 的"账号"= 订阅（subscription id），故记录里 account/profile 一律填 subscription id。
认证沿用本机已配好的 az（`az login` / service principal），noc 不碰任何密钥。
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone

from ..collection import observed, failed, optional, try_fallback
from .. import deploy_config
from ..aws_safety import is_read_only_azure, is_reboot_azure
from .base import Provider

_FLAG = "🌐"   # Azure 多区域，用地球；具体 region 显示在 region 列


@observed(result="nullable")
def _az(account, args, timeout=60, soft=False):
    """跑一条 az 命令（带 --subscription），解析 JSON。

    失败返回 None（soft=True 时静默——用于「资源提供程序未注册/无此服务」等预期空结果，避免刷屏），
    否则打一行诊断到 stderr（不静默吞）——一次临时的 az 掉登录会让看板上 Azure 资源"凭空消失"，
    要看得出是认证问题。三态：成功有数据 / 成功空列表（rc=0，返回 []）/ 调用失败（返回 None [+诊断]）。
    """
    sub = account.get("subscription", "?")
    action = " ".join(a for a in args[1:4] if not a.startswith("-"))   # 如 "vm list" / "storage account"
    # costmanagement / afd / cdn 等命令组住在 az **extension** 里；本机没装时 az 默认会问"要不要装"——
    # 在 sync 的子进程里既不能交互、也不该动本机（装 extension 是写操作）。关 stdin + 禁用动态安装，
    # 让缺 extension 直接 rc=2 "'xxx' is misspelled or not recognized"，由调用方当成"不可用"退回别的路。
    env = {**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"}
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        if not soft:
            print(f"  ⚠️ Azure {sub} {action}：跑 az 失败（{e}）", file=sys.stderr)
        return None
    if r.returncode != 0:
        if soft:
            return None
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        low = (r.stderr or "").lower()
        hint = "（az 未登录/令牌过期？跑 `az login`）" if ("az login" in low or "aadsts" in low or "credential" in low or "token" in low) else ""
        print(f"  ⚠️ Azure {sub} {action}：az 失败——{tail}{hint}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        if not soft:
            print(f"  ⚠️ Azure {sub} {action}：az 输出非 JSON，跳过", file=sys.stderr)
        return None


class AzureProvider(Provider):
    cloud = "azure"
    cli = "az"
    display = "Azure"
    flag = _FLAG
    order = 5                       # IaaS 同组：AWS=0 阿里=1 腾讯=2 火山=3 GCP=4 Azure=5 Oracle=6，只读纳管接其后 Cloudflare=7

    is_read = staticmethod(is_read_only_azure)
    is_reboot = staticmethod(is_reboot_azure)

    @staticmethod
    def _accounts():
        return deploy_config.cloud_accounts("azure")

    # —— 生命周期：VM 操作需 name + resource group + subscription ——
    def reboot_argv(self, inst):
        # Azure VM 有优雅重启 API：vm restart（区别于 GCP 的硬 reset）。
        return ["az", "vm", "restart", "--name", inst["name"],
                "--resource-group", inst.get("resource_group", ""),
                "--subscription", inst["profile"]]

    def status_argv(self, inst):
        return ["az", "vm", "show", "--name", inst["name"],
                "--resource-group", inst.get("resource_group", ""),
                "--subscription", inst["profile"], "--show-details",
                "--query", "{name:name,state:powerState,location:location}", "-o", "tsv"]

    # —— 虚拟机 VM ——
    def list_instances(self, network=None):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            # --show-details(-d) 才带 powerState / publicIps / privateIps（多一次聚合查询，但拿到运行态与 IP）
            data = _az(a, ["az", "vm", "list", "--subscription", sub,
                           "--show-details", "--output", "json"])
            if not data:
                continue
            for i in data:
                loc = i.get("location") or ""
                rg = i.get("resourceGroup") or ""
                vmsize = (i.get("hardwareProfile") or {}).get("vmSize") or ""
                power = (i.get("powerState") or "").replace("VM ", "").strip().lower()   # "VM running" → "running"
                state = {"running": "running", "deallocated": "stopped",
                         "stopped": "stopped"}.get(power, power or "unknown")
                pub = (i.get("publicIps") or "").split(",")[0].strip() or None
                prv = (i.get("privateIps") or "").split(",")[0].strip() or None
                zones = i.get("zones") or []
                az = f"{loc}-{zones[0]}" if zones else ""
                name = i.get("name") or i.get("vmId")
                out.append({
                    "name": name, "id": i.get("vmId") or name,
                    "profile": sub, "region": loc, "region_display": loc, "flag": self.flag,
                    "type": vmsize or "VM", "state": state, "az": az,
                    "private_ip": prv, "public_ip": pub,
                    "key_name": None, "ssh_alias": "azure-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name),
                    "ssh_user": None, "key_file": "",
                    # verified 专指"已真实 SSH 探测"；只读展示不探测，统一 False
                    "connect": "direct", "proxyjump": None, "verified": False, "cloud": "azure",
                    "resource_group": rg,    # Azure 专属：reboot/status argv 需要它
                })
        return out

    # —— 存储账户（Blob）——
    def list_storage(self):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            data = _az(a, ["az", "storage", "account", "list", "--subscription", sub, "-o", "json"])
            if not data:
                continue
            for b in data:
                name = b.get("name")
                loc = b.get("location") or ""
                created = (b.get("creationTime") or "")[:10]
                blob = (b.get("primaryEndpoints") or {}).get("blob") or ""
                host = re.sub(r"^https?://", "", blob).rstrip("/")
                out.append({
                    "name": name, "cloud": "azure", "account": sub,
                    "region": loc, "region_display": loc.title(),
                    "domain": host or f"{name}.blob.core.windows.net",
                    "created": created,
                })
        out.sort(key=lambda s: (s["account"], s["name"] or ""))
        return out

    # —— Azure DNS zone → 「域名」tab ——
    def list_domains(self):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            # soft：新订阅未注册 Microsoft.Network 资源提供程序时，subscription 级 zone list 会报
            # 误导性的 "subscription does not exist"（同订阅 vm list 却正常）——属预期，别刷诊断。
            data = _az(a, ["az", "network", "dns", "zone", "list", "--subscription", sub, "-o", "json"], soft=True)
            if not data:
                continue
            for z in data:
                n = z.get("numberOfRecordSets")
                out.append({
                    "domain": z.get("name"), "type": "DNS zone", "registrar": "Azure DNS",
                    "account": sub, "reg_date": "", "expire_date": "", "days_left": None,
                    "note": (f"{n} 条记录" if n is not None else "active"),
                })
        return out

    # —— 托管数据库：PostgreSQL / MySQL flexible-server + SQL Server 逻辑服务器 ——
    def list_databases(self):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            for engine, port, cmd in (
                ("PostgreSQL", "5432", ["az", "postgres", "flexible-server", "list"]),
                ("MySQL", "3306", ["az", "mysql", "flexible-server", "list"]),
            ):
                # soft：某订阅没启用该数据库服务/资源提供程序时 az 会报错，属预期，别刷屏
                data = _az(a, cmd + ["--subscription", sub, "-o", "json"], soft=True)
                for s in (data or []):
                    sku = s.get("sku") or {}
                    st = s.get("storage") or {}
                    ver = s.get("version") or ""
                    fqdn = s.get("fullyQualifiedDomainName") or ""
                    size = st.get("storageSizeGb") or st.get("storageSizeGB")
                    out.append({
                        "name": s.get("name"), "cloud": "azure", "account": sub,
                        "region": s.get("location", ""), "region_display": s.get("location", ""),
                        "flag": self.flag, "engine": (engine + " " + str(ver)).strip(),
                        "class": sku.get("name") or sku.get("tier") or "—",
                        "storage": (f"{size} GB" if size else "—"),
                        "state": (s.get("state") or "").lower() or "—",
                        "endpoint": (f"{fqdn}:{port}" if fqdn else "—"),
                    })
            data = _az(a, ["az", "sql", "server", "list", "--subscription", sub, "-o", "json"], soft=True)
            for s in (data or []):
                fqdn = s.get("fullyQualifiedDomainName") or ""
                out.append({
                    "name": s.get("name"), "cloud": "azure", "account": sub,
                    "region": s.get("location", ""), "region_display": s.get("location", ""),
                    "flag": self.flag, "engine": "SQL Server", "class": "—", "storage": "—",
                    "state": (s.get("state") or "").lower() or "—",
                    "endpoint": (f"{fqdn}:1433" if fqdn else "—"),
                })
        out.sort(key=lambda d: (d["account"], d["name"] or ""))
        return out

    # —— Function App（无服务器）——
    def list_serverless(self):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            data = _az(a, ["az", "functionapp", "list", "--subscription", sub, "-o", "json"])
            if not data:
                continue
            for f in data:
                loc = f.get("location") or ""
                runtime = (f.get("siteConfig") or {}).get("linuxFxVersion") or ""
                out.append({
                    "name": f.get("name"), "cloud": "azure", "account": sub,
                    "region": loc, "region_display": loc, "flag": self.flag,
                    "runtime": runtime or "—", "memory": None, "timeout": None, "code_mb": None,
                    "last_modified": (f.get("lastModifiedTimeUtc") or "")[:10],
                })
        out.sort(key=lambda s: (s["account"], s["name"] or ""))
        return out

    # —— 账务 / 额度（信用余额 · 免费额度 · 超额保护 · 预算告警）——
    #
    # ⚠️ MCA（Microsoft 客户协议）下这些数据**不在订阅 scope**，照按量付费的老文档打订阅 scope
    # 会拿到误导性错误，别重踩（2026-08-27 实测）：
    #   · 信用余额只能走 billingProfile scope 的 Consumption/credits/balanceSummary；
    #     同 scope 的 `availableBalance?api-version=2020-05-01` 对 MCA Individual 直接返 400。
    #   · CostManagement/query 和 Consumption/usageDetails 打**订阅** scope 会报
    #     "doesn't have valid WebDirect/AIRS offer type"（404）——这是 MCA 的正常表现，
    #     不是订阅坏了，也不必去"修"。
    #   · 免费额度（freetier 促销）不在计费 API 里，在订阅本身的 promotions[]。
    # 这些接口都不计费（区别于 AWS Cost Explorer 每请求 $0.01），故全量 sync 直接拉、不做缓存；
    # 但 CostManagement 系列有 429 限流，所以额度这里一律避开它，只打上面几个轻量端点
    # （成本走 billingProfile scope 的 CostManagement/query，见 list_billing 头注）。
    _MGMT = "https://management.azure.com"

    def _rest(self, account, url, soft=False):
        """az rest GET（带 --subscription，让 token 落到该订阅所在租户）。失败返回 None。"""
        return _az(account, ["az", "rest", "--method", "get", "--url", url,
                             "--subscription", account.get("subscription", ""), "-o", "json"],
                   soft=soft)

    def _billing_profile(self, account, cache):
        """该订阅所属的 (billing_account_id, billing_profile) —— 拿不到返回 (None, None)。

        计费账户/档案是**租户级**的，多个订阅共享同一份，故按租户缓存，免得逐订阅重复查。
        这里取第一个有读权限的账户与其第一个档案：MCA Individual（个人订阅）就是一账户一档案，
        企业多档案场景需要按 `/subscriptions/<id>/billingAccount` 精确定位，届时再扩。
        """
        key = "billing"
        if key in cache:
            return cache[key]
        cache[key] = (None, None)
        accounts = self._rest(account, f"{self._MGMT}/providers/Microsoft.Billing/billingAccounts"
                                       f"?api-version=2020-05-01", soft=True)
        for ba in ((accounts or {}).get("value") or []):
            props = ba.get("properties") or {}
            if not props.get("hasReadAccess"):
                continue
            profiles = self._rest(account, f"{self._MGMT}/{ba['id'].lstrip('/')}/billingProfiles"
                                           f"?api-version=2020-05-01", soft=True)
            for bp in ((profiles or {}).get("value") or []):
                cache[key] = (ba["name"], bp)
                return cache[key]
        return cache[key]

    def _credit_balance(self, account, ba_id, bp):
        """billingProfile 的信用余额（= 送的钱，非免费额度）。返回 (数值, 币种)，拿不到 (None, None)。"""
        if not (ba_id and bp):
            return None, None
        url = (f"{self._MGMT}/providers/Microsoft.Billing/billingAccounts/{ba_id}"
               f"/billingProfiles/{bp['name']}/providers/Microsoft.Consumption/credits"
               f"/balanceSummary?api-version=2023-05-01")
        data = self._rest(account, url, soft=True)
        cur = (((data or {}).get("properties") or {}).get("balanceSummary") or {}).get("currentBalance") or {}
        if cur.get("value") is None:
            return None, None
        return float(cur["value"]), cur.get("currency")

    def _budgets(self, account):
        """订阅上的预算告警，渲染成一句话（如 `noc-monthly-guard ¥100/Monthly`）。无则 None。

        ⚠️ 预算**只告警不断供**：它不会在超额时停掉服务。信用余额为 0 且 spendingLimit=Off 时，
        它是唯一的费用兜底，全靠人看邮件——所以这一列为空要当成风险，不是"没配置而已"。
        """
        sub = account.get("subscription")
        data = self._rest(account, f"{self._MGMT}/subscriptions/{sub}/providers"
                                   f"/Microsoft.Consumption/budgets?api-version=2023-05-01", soft=True)
        grains = {"Monthly": "月", "Quarterly": "季", "Annually": "年", "BillingMonth": "账期月"}
        signs = {"CNY": "¥", "USD": "$", "EUR": "€"}
        parts = []
        for b in ((data or {}).get("value") or []):
            props = b.get("properties") or {}
            amt = props.get("amount")
            if amt is None:
                parts.append(str(b.get("name")))
                continue
            # 预算本身不带币种字段，币种在 currentSpend.unit（= 计费档案的结算币种）
            unit = (props.get("currentSpend") or {}).get("unit") or ""
            grain = props.get("timeGrain") or ""
            parts.append(f"{b.get('name')} {signs.get(unit, unit)}{amt:g}/{grains.get(grain, grain)}")
        return "；".join(parts) or None

    def list_credits(self):
        out = []
        today = date.today()
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cache = {}                       # 租户级计费账户/档案，多订阅共享一份
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            info = self._rest(a, f"{self._MGMT}/subscriptions/{sub}?api-version=2022-12-01")
            if not info:                 # 掉登录/无权限：_az 已打诊断，跳过该订阅
                continue
            pol = info.get("subscriptionPolicies") or {}
            # quotaId 形如 PayAsYouGo_2014-09-01 / FreeTrial_2014-09-01，取前半段当付费类型
            plan = (pol.get("quotaId") or "").split("_")[0] or None
            # 免费额度不在计费 API 里，而在订阅的 promotions[]（category=freetier 即"12 个月免费服务"）
            ft_name = ft_end = None
            ft_days = None
            for promo in (info.get("promotions") or []):
                end = (promo.get("endDateTime") or "")[:10]
                cat = promo.get("category") or "promo"
                if not end:
                    continue
                if ft_end and end <= ft_end:      # 多条促销取**最晚**到期的那条
                    continue
                ft_end, ft_name = end, ("12 个月免费服务" if cat == "freetier" else cat)
            if ft_end:
                try:
                    ft_days = (date.fromisoformat(ft_end) - today).days
                except ValueError:
                    ft_days = None
            ba_id, bp = self._billing_profile(a, cache)
            credit, credit_cur = self._credit_balance(a, ba_id, bp)
            out.append({
                "cloud": "azure", "account": sub,
                "account_name": info.get("displayName") or sub,
                "plan": plan, "state": info.get("state"),
                "credit": credit, "credit_currency": credit_cur,
                "free_tier": ft_name, "free_tier_end": ft_end, "free_tier_days": ft_days,
                "spending_limit": pol.get("spendingLimit"),
                "budget": self._budgets(a),
                "currency": ((bp or {}).get("properties") or {}).get("currency"),
                "fetched_at": fetched,
            })
        out.sort(key=lambda c: c["account"])
        return out

    # —— 成本（近 N 个自然月 × 服务；行结构与 AWS Cost Explorer 一致，前端按币种分列合计）——
    #
    # 三条路，按顺序退：
    #   ① **billingProfile scope 的 CostManagement/query**（`az rest --method post`，核心 az 自带）。MCA
    #      （Microsoft 客户协议）订阅的成本数据只在这一层：订阅 scope 直接 429/404（见 list_credits 头注）。
    #      2026-10-25 实测 200、5 秒返回，列是 Cost/BillingMonth/ServiceName/SubscriptionId/Currency。
    #      ⚠️ 这是整条 Azure 驱动里唯一的 POST——query 是「查询」语义，不改任何资源（Cost Management
    #      的查询接口就是 POST 带 body），用户 2026-10-25 明确放开。body 只有时间窗/分组，不含秘密。
    #      档案是**租户级**的、覆盖该档案下**所有**订阅，所以：按档案只查一次（多订阅共享），行里的
    #      SubscriptionId 直接当 account 列——配置里没写的订阅在花钱也会被看到，这是想要的。
    #   ② `az costmanagement query`（订阅 scope；在 **extension** 里，本机没装就 rc=2；而且 extension 1.0.0
    #      只剩 `export` 子组、没有 `query`）。留着是为装了老 extension / 企业协议（EA）订阅的机器。
    #   ③ `az consumption usage list --start-date --end-date`（核心 az 自带，预览状态）→ 逐条 usage detail
    #      按 consumedService（Microsoft.Compute 这一级；有 meterCategory 优先用，没有再退 product /
    #      instanceName）× 月聚合。⚠️ 别加 `--include-meter-details`：2026-10-25 实测带它直接挂死 4 分钟
    #      超时（不带 2 秒返回），meterCategory 不值这个代价。
    # 三条都不计费，但全量 sync 仍走 sync._billing_with_cache 的 12h 缓存（整类一起缓存）。
    # 免费订阅/无账单读权限：都返回空或失败 → 该订阅不出行，stderr 一行提示（别静默，否则看板上
    # "Azure 没成本"分不清是真没花钱还是没权限）。
    # 行过滤：四舍五入到分后 ≤ 0 的不出行——档案层会给出「信用抵扣」负数行（ServiceName 空）和
    # 0.00002 CNY 这种存储零头，都是噪音，不是「花费」。
    @staticmethod
    def _month_window(months):
        """(起始日 YYYY-MM-01, 截止日=今天, 本月 YYYY-MM)。含当月，往前共 months 个自然月。"""
        today = datetime.now(timezone.utc).date()
        y, m = today.year, today.month - (months - 1)
        while m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}-01", today.isoformat(), today.strftime("%Y-%m")

    @staticmethod
    def _parse_cost_table(data):
        """CostManagement/query 的 columns/rows 表 → [(service, month, amount, currency, subscription|None)]。
        形态不认识返回 None（调用方当失败退下一路）。列顺序以 columns 为准，别写死下标。"""
        if not isinstance(data, dict):
            return None
        body = data.get("properties") if isinstance(data.get("properties"), dict) else data
        cols = [(c.get("name") or "") for c in (body.get("columns") or [])]
        rows = body.get("rows") or []

        def idx(*names):
            for n in names:
                if n in cols:
                    return cols.index(n)
            return None
        i_cost, i_svc, i_cur = idx("Cost", "PreTaxCost", "CostUSD"), idx("ServiceName"), idx("Currency")
        i_month, i_sub = idx("BillingMonth", "UsageDate", "BillingPeriod"), idx("SubscriptionId")
        if i_cost is None or i_month is None:
            return None

        def col(r, i):
            return r[i] if i is not None and i < len(r) else None
        out = []
        for r in rows:
            try:
                amt = float(r[i_cost])
                month = str(r[i_month])[:7]
            except (TypeError, ValueError, IndexError):
                continue
            out.append((col(r, i_svc) or "Other", month, amt, col(r, i_cur) or "USD", col(r, i_sub) or None))
        return out

    def _cost_profile(self, account, ba_id, bp, start, end):
        """路 ①：billingProfile scope 的 CostManagement/query（POST）。
        返回 [(service, month, amount, currency, subscription)]；档案拿不到/失败返回 None。"""
        if not (ba_id and bp):
            return None
        url = (f"{self._MGMT}/providers/Microsoft.Billing/billingAccounts/{ba_id}"
               f"/billingProfiles/{bp['name']}/providers/Microsoft.CostManagement/query?api-version=2023-11-01")
        body = json.dumps({
            "type": "ActualCost", "timeframe": "Custom", "timePeriod": {"from": start, "to": end},
            "dataset": {"granularity": "Monthly",
                        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ServiceName"},
                                     {"type": "Dimension", "name": "SubscriptionId"}]},
        }, separators=(",", ":"))
        data = _az(account, ["az", "rest", "--method", "post", "--url", url,
                             "--headers", "Content-Type=application/json", "--body", body,
                             "--subscription", account.get("subscription", ""), "-o", "json"],
                   timeout=120, soft=True)
        return self._parse_cost_table(data)

    def _cost_query(self, account, start, end):
        """路 ②：costmanagement query（订阅 scope）。返回 [(service, month, amount, currency)]；不可用/失败返回 None。"""
        sub = account.get("subscription", "")
        data = _az(account, [
            "az", "costmanagement", "query", "--type", "ActualCost", "--timeframe", "Custom",
            "--time-period", f"from={start}", f"to={end}",
            "--dataset-granularity", "Monthly",
            "--dataset-aggregation", '{"totalCost":{"name":"Cost","function":"Sum"}}',
            "--dataset-grouping", "name=ServiceName", "type=Dimension",
            "--scope", f"/subscriptions/{sub}", "--subscription", sub, "-o", "json",
        ], timeout=120, soft=True)
        rows = self._parse_cost_table(data)
        return None if rows is None else [r[:4] for r in rows]

    def _cost_consumption(self, account, start, end):
        """路 ③：consumption usage list 聚合。返回 [(service, month, amount, currency)]；失败返回 None。"""
        sub = account.get("subscription", "")
        data = _az(account, [
            "az", "consumption", "usage", "list", "--subscription", sub,
            "--start-date", start, "--end-date", end, "-o", "json",
        ], timeout=180, soft=True)
        if not isinstance(data, list):
            return None
        agg, currency = {}, {}
        for u in data:
            try:
                amt = float(u.get("pretaxCost") or u.get("cost") or 0)
            except (TypeError, ValueError):
                continue
            md = u.get("meterDetails") or {}
            svc = (md.get("meterCategory") or u.get("consumedService") or u.get("product")
                   or u.get("instanceName") or "Other")
            month = (u.get("usageStart") or u.get("date") or "")[:7]
            if not month:
                continue
            agg[(svc, month)] = agg.get((svc, month), 0.0) + amt
            currency.setdefault((svc, month), u.get("currency") or u.get("billingCurrency") or "USD")
        return [(svc, month, amt, currency[(svc, month)]) for (svc, month), amt in agg.items()]

    def list_billing(self, months=3):
        start, end, cur_month = self._month_window(months)
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out, cache, seen_profiles = [], {}, set()

        def emit(rows, default_account, label):
            n = 0
            for svc, month, amt, cur, sub in rows:
                amt = round(amt, 2)
                if amt <= 0:                            # 只保留有实际花费的服务（抵扣负数、零头一并略过）
                    continue
                out.append({
                    "cloud": "azure", "account": sub or default_account, "service": svc,
                    "month": month, "amount": amt, "currency": cur,
                    "period_start": f"{month}-01", "partial": month == cur_month,
                    "fetched_at": fetched,
                })
                n += 1
            if not n:
                why = "该档案下所有订阅都没有花费" if label.startswith("计费档案") else "免费订阅，或账号无账单读权限"
                print(f"  ℹ️ Azure {label} 成本：{start[:7]}～{cur_month} 无成本记录（{why}）", file=sys.stderr)

        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            ba_id, bp = try_fallback(self._billing_profile, a, cache)
            if ba_id and bp:                            # 路 ①：档案级，租户内多订阅共享同一档案 → 只查一次
                key = (ba_id, bp["name"])
                if key in seen_profiles:
                    continue
                rows = try_fallback(self._cost_profile, a, ba_id, bp, start, end)
                if rows is not None:
                    seen_profiles.add(key)
                    emit(rows, sub, f"计费档案 {bp.get('name')}")
                    continue
            rows = try_fallback(self._cost_query, a, start, end)
            if rows is None:
                rows = try_fallback(self._cost_consumption, a, start, end)
            if rows is None:
                failed()
                print(f"  ⚠️ Azure {sub} 成本：计费档案查询不可用、costmanagement query 不可用（extension 未装/订阅 scope 不支持）"
                      f"且 consumption usage list 失败（无账单读权限或 az 未登录？），跳过", file=sys.stderr)
                continue
            emit([r + (None,) for r in rows], sub, sub)
        out.sort(key=lambda x: (x["account"], x["month"], -x["amount"]))
        return out

    # —— 访问主体（RBAC 角色分配按主体聚合；Azure 没有 AWS 那种"IAM 用户+访问密钥"模型）——
    # 行语义：一个主体（用户 / 服务主体 / 组 / 托管标识）在该订阅下一行，detail 列承载「类型 · 角色 · 作用域数」。
    # 数据源是 `az role assignment list --all --include-inherited`（ARM，只要订阅 Reader 就能读）。
    # created 列来自 Entra ID（Graph）的 createdDateTime——`az ad user list` 默认不吐这个字段，故走
    # `az rest` GET graph /users?$select=…；没 Graph 读权限（403）就静默留空，别刷屏（多数 RBAC-only
    # 的服务主体登录都没这个权限，那是常态不是故障）。key_count / oldest_key_days 对 Azure 无意义 → None。
    _GRAPH_USERS = ("https://graph.microsoft.com/v1.0/users"
                    "?$select=id,userPrincipalName,createdDateTime&$top=999")

    @optional
    def _graph_user_created(self, account, cache):
        """{用户 id 与 UPN(小写) → 创建日 YYYY-MM-DD}。Graph 不可用返回 {}。租户级数据，按次缓存一份。"""
        if "graph_users" in cache:
            return cache["graph_users"]
        cache["graph_users"] = {}
        url, pages = self._GRAPH_USERS, 0
        while url and pages < 20:                           # nextLink 翻页；20 页×999 足够，别无限跟
            data = self._rest(account, url, soft=True)
            if not isinstance(data, dict):
                break                                       # 403/未授权 Graph → 静默
            for u in data.get("value") or []:
                created = (u.get("createdDateTime") or "")[:10]
                if not created:
                    continue
                for k in (u.get("id"), (u.get("userPrincipalName") or "").lower()):
                    if k:
                        cache["graph_users"][k] = created
            url, pages = data.get("@odata.nextLink"), pages + 1
        return cache["graph_users"]

    def list_iam(self):
        out = []
        cache = {}
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            data = _az(a, ["az", "role", "assignment", "list", "--all", "--include-inherited",
                           "--subscription", sub, "-o", "json"], timeout=120)
            if data is None:                                # 掉登录/无权限：_az 已打诊断
                continue
            agg = {}                                        # principalId → 聚合（保持首次出现顺序）
            for r in data:
                pid = r.get("principalId") or r.get("principalName") or "?"
                e = agg.setdefault(pid, {"name": r.get("principalName") or pid,
                                         "type": r.get("principalType") or "Unknown",
                                         "roles": [], "scopes": set()})
                role = r.get("roleDefinitionName") or (r.get("roleDefinitionId") or "?").rsplit("/", 1)[-1]
                if role not in e["roles"]:
                    e["roles"].append(role)
                e["scopes"].add(r.get("scope") or "")
            users = self._graph_user_created(a, cache) if any(e["type"] == "User" for e in agg.values()) else {}
            for pid, e in agg.items():
                created = (users.get(pid) or users.get(e["name"].lower()) or "") if e["type"] == "User" else ""
                out.append({
                    "name": e["name"], "cloud": "azure", "account": sub,
                    "created": created, "last_used": "—",
                    "key_count": None, "oldest_key_days": None,
                    "detail": f"{e['type']} · 角色: {', '.join(e['roles'])} · 作用域 {len(e['scopes'])} 个",
                })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    # —— CDN：Azure Front Door（Standard/Premium）+ 经典 CDN（Microsoft/Edgio…）的端点 ——
    #
    # `az afd` / `az cdn` 两个命令组都在**预览版 `cdn` extension** 里，默认不装，装它既是改本机又要交互；
    # 两者底层都是 ARM 的 Microsoft.Cdn/profiles 这一棵树，sku.name 区分 Front Door（*_AzureFrontDoor）
    # 与经典 CDN（Standard_Microsoft / Standard_Edgio…），所以这里直接 `az rest` GET 这棵树（核心 az 自带，
    # 纯读），不依赖 extension：
    #   profiles → Front Door: afdEndpoints（对外域名）+ 每端点 routes（→ customDomains 别名、originGroup）
    #                           + customDomains / originGroups/*/origins（按 profile 各查一次做映射）
    #            → 经典 CDN:   endpoints（自带 origins[]）+ 每端点 customDomains
    # 订阅没注册 Microsoft.Cdn 资源提供程序时，list 多半直接返回空 value（2026-10-25 实测），个别情况会报
    # NoRegisteredProviderFound / "subscription does not exist"——当"该订阅没 CDN"处理，一行提示不刷屏。
    _CDN_API = "2024-02-01"

    def _arm_list(self, account, url):
        """ARM 集合 GET（跟 nextLink 翻页）。失败返回 None，空集合返回 []。"""
        out, pages = [], 0
        while url and pages < 50:
            data = self._rest(account, url, soft=True)
            if not isinstance(data, dict):
                return None if pages == 0 else out
            out.extend(data.get("value") or [])
            url, pages = data.get("nextLink"), pages + 1
        return out

    def _cdn_row(self, sub, profile, sku, ep, hostname, aliases, origins, status, enabled):
        return {
            "id": (ep.get("id") or ep.get("name") or "").rsplit("/", 1)[-1] or ep.get("name"),
            "domain": hostname or "", "aliases": ", ".join(a for a in aliases if a),
            "origin": ", ".join(o for o in origins if o),
            "status": status or "—", "enabled": bool(enabled), "cert": "—",
            "price_class": sku or "—", "last_modified": "",
            "account": sub, "cloud": "azure", "partition": "global",
            "comment": profile.get("name") or "",
        }

    def _afd_rows(self, account, sub, profile):
        pid, sku = profile["id"], (profile.get("sku") or {}).get("name") or ""
        base = f"{self._MGMT}{pid}"
        eps = self._arm_list(account, f"{base}/afdEndpoints?api-version={self._CDN_API}") or []
        if not eps:
            return []
        # 自定义域名 id → hostName；源站组 id → [源站 host]（都是 profile 级，各查一次）
        domains = {(d.get("id") or "").lower(): (d.get("properties") or {}).get("hostName") or ""
                   for d in (self._arm_list(account, f"{base}/customDomains?api-version={self._CDN_API}") or [])}
        og_hosts = {}
        for og in (self._arm_list(account, f"{base}/originGroups?api-version={self._CDN_API}") or []):
            ogid = (og.get("id") or "").lower()
            origins = self._arm_list(account, f"{self._MGMT}{og['id']}/origins?api-version={self._CDN_API}") or []
            og_hosts[ogid] = [(o.get("properties") or {}).get("hostName") or "" for o in origins]
        out = []
        for ep in eps:
            p = ep.get("properties") or {}
            routes = self._arm_list(account, f"{self._MGMT}{ep['id']}/routes?api-version={self._CDN_API}") or []
            aliases, origins = [], []
            for rt in routes:
                rp = rt.get("properties") or {}
                for cd in rp.get("customDomains") or []:
                    h = domains.get((cd.get("id") or "").lower())
                    if h and h not in aliases:
                        aliases.append(h)
                for h in og_hosts.get(((rp.get("originGroup") or {}).get("id") or "").lower(), []):
                    if h and h not in origins:
                        origins.append(h)
            enabled = p.get("enabledState") == "Enabled"
            out.append(self._cdn_row(sub, profile, sku, ep, p.get("hostName"), aliases, origins,
                                     p.get("provisioningState") or p.get("enabledState"), enabled))
        return out

    def _classic_cdn_rows(self, account, sub, profile):
        pid, sku = profile["id"], (profile.get("sku") or {}).get("name") or ""
        eps = self._arm_list(account, f"{self._MGMT}{pid}/endpoints?api-version={self._CDN_API}") or []
        out = []
        for ep in eps:
            p = ep.get("properties") or {}
            origins = [(o.get("properties") or {}).get("hostName") or "" for o in (p.get("origins") or [])]
            cds = self._arm_list(account, f"{self._MGMT}{ep['id']}/customDomains?api-version={self._CDN_API}") or []
            aliases = [(d.get("properties") or {}).get("hostName") or "" for d in cds]
            # 经典 CDN 没有 enabledState，用 resourceState（Running/Stopped/Creating…）判启用
            state = p.get("resourceState") or ""
            out.append(self._cdn_row(sub, profile, sku, ep, p.get("hostName"), aliases, origins,
                                     state or p.get("provisioningState"), state == "Running"))
        return out

    def list_cdn(self):
        out = []
        for a in self._accounts():
            sub = a.get("subscription")
            if not sub:
                continue
            profiles = self._arm_list(a, f"{self._MGMT}/subscriptions/{sub}/providers/Microsoft.Cdn/profiles"
                                         f"?api-version={self._CDN_API}")
            if profiles is None:
                print(f"  ℹ️ Azure {sub} CDN：Microsoft.Cdn 列表失败（资源提供程序未注册或无权限），按无 CDN 处理",
                      file=sys.stderr)
                continue
            for prof in profiles:
                if not prof.get("id"):
                    continue
                sku = (prof.get("sku") or {}).get("name") or ""
                if "AzureFrontDoor" in sku:
                    out.extend(self._afd_rows(a, sub, prof))
                else:
                    out.extend(self._classic_cdn_rows(a, sub, prof))
        out.sort(key=lambda c: (c["account"], c["domain"] or ""))
        return out
