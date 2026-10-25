"""AWS 驱动。
- EC2 实例拉取仍在 sync.build_scopes（重机器：探测/ssh config/快照），但其"账号×区域"配置在此处单一维护，sync 反向 import。
- 这里直接实现：分类、reboot/status argv、Route53 域名、RDS 数据库、S3 存储、Cost Explorer 成本、
  账务/额度（Budgets + Free Tier + CE credits，见 list_credits）等（均只读 describe/list/get）。"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import as_completed
from datetime import datetime, timedelta, timezone

from ..collection import ThreadPoolExecutor, observed, failed, optional
from .. import deploy_config
from ..aws_safety import is_read_only_aws, is_reboot_aws
from .base import Provider

# —— AWS 区域元数据（通用 AWS 知识，留代码）；账号/扫描区域/route53 见 ops-config.toml ——
REGION_META = {  # region → (昵称, 显示名, 旗帜)
    "ap-east-1": ("hongkong", "Hong Kong", "🇭🇰"),
    "ap-east-2": ("taibei", "Taibei", "🇨🇳"),
    "ap-northeast-1": ("tokyo", "Tokyo", "🇯🇵"),
    "ap-northeast-2": ("seoul", "Seoul", "🇰🇷"),
    "ap-northeast-3": ("osaka", "Osaka", "🇯🇵"),
    "ap-southeast-1": ("singapore", "Singapore", "🇸🇬"),
    "eu-central-1": ("frankfurt", "Frankfurt", "🇩🇪"),
    "us-east-1": ("virginia", "Virginia", "🇺🇸"),
    "us-east-2": ("ohio", "Ohio", "🇺🇸"),
    "us-west-1": ("california", "California", "🇺🇸"),
    "us-west-2": ("oregon", "Oregon", "🇺🇸"),
    "cn-northwest-1": ("ningxia", "Ningxia", "🇨🇳"),
}
def regions_for(account):
    """该账号要扫描的区域（partition=china/global 决定哪组，见 ops-config.toml）。"""
    return deploy_config.scan_regions(account["partition"])


@observed(result="nullable")
def _aws_json(profile, region, args):
    """跑 aws <args>（json），失败/未启用区域静默返回 None。"""
    cmd = ["aws", *args, "--profile", profile, "--output", "json"]
    if region:
        cmd += ["--region", region]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "AWS_PAGER": ""})
    except FileNotFoundError:          # 本机没装 aws CLI：当作查不到，由 sync 层统一提示
        return None
    if r.returncode != 0:
        return None
    if not (r.stdout or "").strip():   # rc=0 但不输出任何内容（如 budgets describe-budgets 没预算时）= 成功的空结果，不是失败
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


class AwsProvider(Provider):
    cloud = "aws"
    cli = "aws"
    display = "AWS"
    flag = ""
    order = 0

    is_read = staticmethod(is_read_only_aws)
    is_reboot = staticmethod(is_reboot_aws)

    def reboot_argv(self, inst):
        return ["aws", "ec2", "reboot-instances", "--instance-ids", inst["id"],
                "--profile", inst["profile"], "--region", inst["region"]]

    def status_argv(self, inst):
        return ["aws", "ec2", "describe-instance-status", "--instance-ids", inst["id"],
                "--include-all-instances", "--profile", inst["profile"], "--region", inst["region"],
                "--query", "InstanceStatuses[].{State:InstanceState.Name,"
                           "Instance:InstanceStatus.Status,System:SystemStatus.Status}",
                "--output", "json"]

    # list_instances 不在此实现（AWS EC2 走 sync.build_scopes，含探测/ssh config/快照）。

    # —— RDS 数据库（只读，全账号×区域并发扫描）——
    def _rds_in(self, profile, region):
        data = _aws_json(profile, region, ["rds", "describe-db-instances"])
        if not data:
            return []
        _, disp, flag = REGION_META.get(region, (region, region, ""))
        out = []
        for db in data.get("DBInstances", []):
            ep = db.get("Endpoint") or {}
            out.append({
                "name": db.get("DBInstanceIdentifier"), "cloud": "aws",
                "account": profile, "region": region, "region_display": disp, "flag": flag,
                "engine": f'{db.get("Engine", "")} {db.get("EngineVersion", "")}'.strip(),
                "class": db.get("DBInstanceClass"),
                "storage": f'{db.get("AllocatedStorage", "?")}GB {db.get("StorageType", "")}'.strip(),
                "state": db.get("DBInstanceStatus"),
                "endpoint": f'{ep.get("Address", "")}:{ep.get("Port", "")}' if ep.get("Address") else "—",
                "multi_az": db.get("MultiAZ", False),
                "created": (db.get("InstanceCreateTime") or "")[:10],
            })
        return out

    def list_databases(self):
        tasks = [(a["profile"], r) for a in deploy_config.aws_accounts() for r in regions_for(a)]
        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for f in as_completed([ex.submit(self._rds_in, p, r) for p, r in tasks]):
                try:
                    out.extend(f.result())
                except Exception:
                    failed("invalid_response")
        out.sort(key=lambda d: (d["account"], d["region"], d["name"] or ""))
        return out

    # —— S3 存储桶（只读；list-buckets 每账号一次，再并发查每桶 region）——
    @optional
    def _bucket_region(self, profile, name):
        data = _aws_json(profile, None, ["s3api", "get-bucket-location", "--bucket", name])
        if data is None:
            return "?"
        loc = data.get("LocationConstraint")
        return loc or "us-east-1"   # null 表示 us-east-1

    @staticmethod
    def _origin_bucket(domain):
        """CloudFront origin DomainName → 源 S3 桶名（仅 S3 源）；非 S3 源返回 None。
        匹配 `<bucket>.s3.<region>.amazonaws.com` 与 `<bucket>.s3-website.<region>...`。"""
        if "amazonaws" not in domain:
            return None
        m = re.match(r"^(.+?)\.s3[.-]", domain)
        return m.group(1) if m else None

    @staticmethod
    def _cf_region(account):
        """该账号调 CloudFront 要带的 region。

        中国分区（aws-cn）的 CloudFront 是**独立分区**，端点在中国区，必须显式指到 `cn-northwest-1`
        才列得到该账号的分布；全球分区 CloudFront 是 region-less 全球服务，用 None（不带 --region）。
        """
        return "cn-northwest-1" if account.get("partition") == "china" else None

    def _cloudfront_distributions(self, profile, region=None):
        """一次拉该账号所有 CloudFront 分布的 raw 列表。region 见 _cf_region（china 必须带、global 传 None）。
        list 失败（权限不足/无此服务）静默返回空。
        list_cdn 与 _cloudfront_bucket_domains 共用，避免重复调 API。"""
        data = _aws_json(profile, region, ["cloudfront", "list-distributions"])
        return (data or {}).get("DistributionList", {}).get("Items", []) or []

    @optional
    def _cloudfront_bucket_domains(self, profile, region=None):
        """{桶名: 对外域名}：择优（启用优先、有自定义域名 alias 优先；alias > cloudfront 域名）。
        供 list_storage 给 S3 桶配对外访问域名。region 见 _cf_region。"""
        best = {}   # bucket -> (rank, domain)
        for d in self._cloudfront_distributions(profile, region):
            enabled = bool(d.get("Enabled"))
            aliases = (d.get("Aliases") or {}).get("Items") or []
            value = aliases[0] if aliases else d.get("DomainName", "")
            rank = (2 if enabled else 0) + (1 if aliases else 0)
            for o in (d.get("Origins") or {}).get("Items", []):
                b = self._origin_bucket(o.get("DomainName", ""))
                if b and value and rank > best.get(b, (-1, ""))[0]:
                    best[b] = (rank, value)
        return {b: v for b, (_, v) in best.items()}

    def list_storage(self):
        out = []
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            data = _aws_json(profile, None, ["s3api", "list-buckets"])
            if not data:
                continue
            buckets = data.get("Buckets", [])
            cf = self._cloudfront_bucket_domains(profile, self._cf_region(a))   # 桶→对外域名（按账号一次拉）
            with ThreadPoolExecutor(max_workers=8) as ex:
                regions = list(ex.map(lambda b: self._bucket_region(profile, b["Name"]), buckets))
            for b, region in zip(buckets, regions):
                disp = REGION_META.get(region, (region, region, ""))[1]
                name = b["Name"]
                # 域名列：绑定的自定义域名 > CloudFront 域名 > 原始 S3 Origin 端点
                suffix = ".amazonaws.com.cn" if str(region).startswith("cn-") else ".amazonaws.com"
                domain = cf.get(name) or f"{name}.s3.{region}{suffix}"
                out.append({
                    "name": name, "cloud": "aws", "account": profile,
                    "region": region, "region_display": disp,
                    "domain": domain,
                    "created": (b.get("CreationDate") or "")[:10],
                })
        out.sort(key=lambda s: (s["account"], s["name"]))
        return out

    # —— CloudFront 分布（只读）。全球分区 + 中国分区都纳管：china 是独立分区、端点在中国区，
    #    经 _cf_region 指到 cn-northwest-1 才列得到（全球分区 region-less）。行里带 partition 便于前端区分。
    @staticmethod
    def _cert_source(dist):
        vc = dist.get("ViewerCertificate") or {}
        src = vc.get("CertificateSource") or ("cloudfront" if vc.get("CloudFrontDefaultCertificate") else "")
        return {"acm": "ACM", "iam": "IAM", "cloudfront": "CloudFront默认"}.get(src, src or "—")

    def list_cdn(self):
        out = []
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            china = a.get("partition") == "china"
            for d in self._cloudfront_distributions(profile, self._cf_region(a)):
                aliases = (d.get("Aliases") or {}).get("Items") or []
                origins = [o.get("DomainName", "") for o in (d.get("Origins") or {}).get("Items", [])]
                out.append({
                    "id": d.get("Id"),
                    "domain": aliases[0] if aliases else d.get("DomainName", ""),
                    "aliases": ", ".join(aliases),
                    "origin": ", ".join(o for o in origins if o),
                    "status": d.get("Status"),
                    "enabled": bool(d.get("Enabled")),
                    "cert": self._cert_source(d),
                    "partition": "中国 🇨🇳" if china else "全球 🌐",   # aws-cn(独立分区) vs aws(全球)
                    "price_class": (d.get("PriceClass") or "").replace("PriceClass_", ""),
                    "last_modified": (d.get("LastModifiedTime") or "")[:10],
                    "cloud": "aws", "account": profile,
                    "comment": d.get("Comment") or "",
                })
        # 排序：全球在前、中国在后，再按账号/域名（partition 串以「全球」<「中国」偶合升序，稳妥起见显式给权重）
        out.sort(key=lambda c: (0 if "全球" in c["partition"] else 1, c["account"], c["domain"] or ""))
        return out

    # —— Lambda 函数（只读，全账号×区域并发）——
    def _lambda_in(self, profile, region):
        data = _aws_json(profile, region, ["lambda", "list-functions"])
        if not data:
            return []
        _, disp, flag = REGION_META.get(region, (region, region, ""))
        out = []
        for fn in data.get("Functions", []):
            cs = fn.get("CodeSize") or 0
            out.append({
                "name": fn.get("FunctionName"), "cloud": "aws", "account": profile,
                "region": region, "region_display": disp, "flag": flag,
                "runtime": fn.get("Runtime") or fn.get("PackageType") or "—",
                "memory": fn.get("MemorySize"), "timeout": fn.get("Timeout"),
                "code_mb": round(cs / 1048576, 1),
                "last_modified": (fn.get("LastModified") or "")[:10],
            })
        return out

    def list_serverless(self):
        tasks = [(a["profile"], r) for a in deploy_config.aws_accounts() for r in regions_for(a)]
        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for f in as_completed([ex.submit(self._lambda_in, p, r) for p, r in tasks]):
                try:
                    out.extend(f.result())
                except Exception:
                    failed("invalid_response")
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— 消息/事件：SQS / SNS / SES / EventBridge（只读，全账号×区域并发）——
    def _messaging_in(self, profile, region):
        _, disp, flag = REGION_META.get(region, (region, region, ""))
        rows = []

        def row(kind, name, detail=""):
            return {"name": name, "kind": kind, "cloud": "aws", "account": profile,
                    "region": region, "region_display": disp, "flag": flag, "detail": detail}

        d = _aws_json(profile, region, ["sqs", "list-queues"])
        for url in (d or {}).get("QueueUrls", []) or []:
            rows.append(row("SQS", url.rsplit("/", 1)[-1]))

        d = _aws_json(profile, region, ["sns", "list-topics"])
        for t in (d or {}).get("Topics", []) or []:
            rows.append(row("SNS", t.get("TopicArn", "").rsplit(":", 1)[-1]))

        d = _aws_json(profile, region, ["ses", "list-identities"])
        for ident in (d or {}).get("Identities", []) or []:
            rows.append(row("SES", ident, "邮箱" if "@" in ident else "域名"))

        d = _aws_json(profile, region, ["events", "list-rules"])
        for r in (d or {}).get("Rules", []) or []:
            det = r.get("ScheduleExpression") or ("事件模式" if r.get("EventPattern") else "")
            st = r.get("State", "")
            rows.append(row("EventBridge", r.get("Name"),
                            (det + (f" · {st}" if st else "")).strip(" ·")))
        return rows

    def list_messaging(self):
        tasks = [(a["profile"], r) for a in deploy_config.aws_accounts() for r in regions_for(a)]
        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for f in as_completed([ex.submit(self._messaging_in, p, r) for p, r in tasks]):
                try:
                    out.extend(f.result())
                except Exception:
                    failed("invalid_response")
        order = {"SQS": 0, "SNS": 1, "SES": 2, "EventBridge": 3}
        out.sort(key=lambda x: (order.get(x["kind"], 9), x["account"], x["region"], x["name"] or ""))
        return out

    # —— IAM 用户（只读；全局，每用户并发查密钥年龄）——
    def _user_keys(self, profile, username):
        data = _aws_json(profile, None, ["iam", "list-access-keys", "--user-name", username])
        keys = (data or {}).get("AccessKeyMetadata", [])
        now, ages = datetime.now(timezone.utc), []
        for k in keys:
            cd = k.get("CreateDate")
            if cd:
                try:
                    dt = datetime.fromisoformat(str(cd).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ages.append((now - dt).days)
                except ValueError:
                    pass
        return len(keys), (max(ages) if ages else None)

    def list_iam(self):
        out = []
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            data = _aws_json(profile, None, ["iam", "list-users"])
            if not data:
                continue
            users = data.get("Users", [])
            with ThreadPoolExecutor(max_workers=8) as ex:
                keyinfo = list(ex.map(lambda u: self._user_keys(profile, u["UserName"]), users))
            for u, (kc, oldest) in zip(users, keyinfo):
                plu = u.get("PasswordLastUsed")
                out.append({
                    "name": u.get("UserName"), "cloud": "aws", "account": profile,
                    "created": (u.get("CreateDate") or "")[:10],
                    "last_used": str(plu)[:10] if plu else "—",
                    "key_count": kc, "oldest_key_days": oldest,
                })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out

    # —— AI 密钥（IAM 服务专用凭证：Bedrock 长期 API key / Claude Platform on AWS key）——
    # Bedrock 的「长期 API key」本质是绑在 IAM 用户上的 service-specific credential（ID 以 ACCA 开头），
    # 元数据走 `iam list-service-specific-credentials`；密钥串只在创建时给一次，列表拿不到（也不该拿）。
    # 短期 key（12h）是本地用 SigV4 签出来的，服务端无记录，天然列不出、也无需盘。
    # 全球区 --all-users 一次列全账号；**中国区不支持 --all-users**（InvalidInput）——按产品决定直接跳过
    # 中国区账号（aws-cn 的 Bedrock 不是这套 key 体系），不做逐用户兜底（兜底会把缺权限等任何失败
    # 都放大成 2(N+1) 次必败调用、且全程静默，2026-10-24 审查发现）。
    _AI_KEY_SERVICES = {
        "bedrock.amazonaws.com": "Bedrock",
        "aws-external-anthropic.amazonaws.com": "Claude Platform",
    }

    def _ssc_rows(self, profile, service, user=None):
        args = ["iam", "list-service-specific-credentials", "--service-name", service]
        args += ["--user-name", user] if user else ["--all-users"]
        data = _aws_json(profile, None, args)
        return None if data is None else (data.get("ServiceSpecificCredentials") or [])

    def list_apikeys(self):
        out = []
        now = datetime.now(timezone.utc)
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            if a.get("partition") == "china":
                continue                                  # 中国区不支持 --all-users，产品决定不盘
            for service, purpose in self._AI_KEY_SERVICES.items():
                creds = self._ssc_rows(profile, service)
                if creds is None:                         # 缺权限 / CLI 太旧不认服务名 → 该账号该服务为空，但要说出来
                    print(f"  ⚠️ AWS {profile} AI 密钥：list-service-specific-credentials --service-name {service} 失败"
                          f"（缺 iam:ListServiceSpecificCredentials 权限，或 awscli 太旧不认该服务名）", file=sys.stderr)
                    continue
                for c in creds:
                    created = str(c.get("CreateDate") or "")[:10]
                    expires = str(c.get("ExpirationDate") or "")[:10] or None
                    age = None
                    if created:
                        try:
                            age = (now - datetime.fromisoformat(created).replace(tzinfo=timezone.utc)).days
                        except ValueError:
                            pass
                    out.append({
                        "name": c.get("ServiceCredentialAlias") or c.get("ServiceUserName") or c.get("UserName"),
                        "id": c.get("ServiceSpecificCredentialId"),
                        "cloud": "aws", "account": profile,
                        "region": "global", "region_display": "Global", "flag": "🌐",
                        "purpose": purpose, "targets": [service], "unrestricted": False,
                        "service_account": c.get("UserName"),     # 绑定的 IAM 用户
                        "key_type": "长期 key", "status": c.get("Status") or "—",
                        "client_restriction": "",
                        "created": created, "age_days": age, "updated": "", "expires": expires,
                    })
        out.sort(key=lambda r: (r["account"], r["name"] or ""))
        return out

    # —— 成本（Cost Explorer，只读；global + china 两个分区都纳管）——
    # CE 是账号级全局服务（自动聚合该账号所有区域），端点每分区一个：全球 us-east-1、aws-cn cn-northwest-1。
    # 币种随分区不同（global=USD、china=CNY），直接取 Metrics.Unit 落到行里；**不做汇率折算**，
    # 前端按币种分两列、各自合计（见 dashboard BILLING_COLS / updateBillingTotal）。
    # 坑：① 账号需先启用 CE 且首次启用 ~24h 后才有数据（未启用→调用失败→该账号空行，静默降级）；
    #     ② 每分页请求计费 $0.01，故全量 sync 走 12h 缓存（见 sync._collect_kind），单刷强制重拉。
    def list_billing(self, months=3):
        today = datetime.now(timezone.utc).date()
        y, m = today.year, today.month - (months - 1)      # 近 months 个月（含当月）的首日
        while m <= 0:
            m += 12
            y -= 1
        start = f"{y:04d}-{m:02d}-01"
        end = (today + timedelta(days=1)).isoformat()       # CE 的 End 排他，取明天以含当月至今
        cur_month = today.strftime("%Y-%m")
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            ce_region = "cn-northwest-1" if a.get("partition") == "china" else "us-east-1"
            data = _aws_json(profile, ce_region, [
                "ce", "get-cost-and-usage",
                "--time-period", f"Start={start},End={end}",
                "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
                "--group-by", "Type=DIMENSION,Key=SERVICE",
            ])
            if not data:                                    # 未启用 CE / 无权限 → 静默跳过该账号
                continue
            for r in data.get("ResultsByTime", []):
                pstart = (r.get("TimePeriod") or {}).get("Start", "")
                month = pstart[:7]
                for g in r.get("Groups", []):
                    svc = (g.get("Keys") or ["Other"])[0]
                    metric = (g.get("Metrics") or {}).get("UnblendedCost") or {}
                    amt = float(metric.get("Amount") or 0)
                    if amt <= 0:                            # 只保留有实际花费的服务
                        continue
                    out.append({
                        "cloud": "aws", "account": profile, "service": svc,
                        "month": month, "amount": round(amt, 2),
                        "currency": metric.get("Unit", "USD"),
                        "period_start": pstart, "partial": month == cur_month,
                        "fetched_at": fetched,
                    })
        out.sort(key=lambda x: (x["account"], x["month"], -x["amount"]))
        return out

    # —— 账务/额度（只读；一个账号一行）——
    # AWS 没有「余额」概念（后付费、无充值），这一行回答的是三件事：
    #   预算告警 → `budgets describe-budgets --account-id <自己>`（账号级全局服务）
    #   免费额度 → `freetier get-free-tier-usage`（账号不在免费期/未开通会报错 → None；在免费期但无用量 → 空列表）
    #   credits  → `ce get-cost-and-usage` 按 RECORD_TYPE=Credit 过滤取**本月已抵扣**金额（CE 里 credit 是负数，取绝对值）。
    #              注意这是「本月用掉多少 credit」而不是「还剩多少」——AWS 不给剩余 credit 余额接口，
    #              故 credit 列语义写进 plan 字段，别当余额读。
    # 三个都是账号级全局服务，端点每分区一个、与 CE 相同：全球 us-east-1、aws-cn cn-northwest-1
    # （2026-10-25 实测：中国区 budgets/freetier 在 cn-northwest-1 正常返回，别按"仅全球区"跳过）。
    # 坑：① 没预算时 describe-budgets rc=0 但 stdout 为空——_aws_json 已把它当 {} 而不是失败；
    #     ② 每次 CE 请求计费 $0.01，与 list_billing 同样走 sync 的缓存节奏；
    #     ③ 三路独立：某一路失败只影响其字段（None），三路全失败才不出行（别造假行）并提示一次。
    @staticmethod
    def _summarize_budgets(budgets):
        """预算列表 → 一句话：'2 个预算，1 个超限（名）· 1 个预计超限（名）'；空列表 → ""（渲染层标「未设」）。"""
        if budgets is None:
            return None
        if not budgets:
            return ""
        over, forecast = [], []
        for b in budgets:
            name = b.get("BudgetName") or "?"
            try:
                limit = float((b.get("BudgetLimit") or {}).get("Amount"))
            except (TypeError, ValueError):
                continue                                   # PlannedBudgetLimits 等无固定上限的预算不比对
            spend = b.get("CalculatedSpend") or {}
            try:
                actual = float((spend.get("ActualSpend") or {}).get("Amount") or 0)
                fc = float((spend.get("ForecastedSpend") or {}).get("Amount") or 0)
            except (TypeError, ValueError):
                actual, fc = 0.0, 0.0
            if limit > 0 and actual > limit:
                over.append(name)
            elif limit > 0 and fc > limit:
                forecast.append(name)
        parts = [f"{len(budgets)} 个预算"]
        if over:
            parts.append(f"{len(over)} 个超限（{', '.join(over[:3])}）")
        if forecast:
            parts.append(f"{len(forecast)} 个预计超限（{', '.join(forecast[:3])}）")
        if not over and not forecast:
            parts.append("均在限内")
        return "，".join(parts)

    @staticmethod
    def _summarize_free_tier(usages, top=3):
        """免费额度用量列表 → '29 项 Always Free · 用量最高：CodeCommit User-Month 1/5 …'；空 → None。
        按 实际/上限 占比降序取前几项；任一项 ≥85% 标 ⚠️（超出即按量扣款）。"""
        if not usages:
            return None
        types = sorted({u.get("freeTierType") for u in usages if u.get("freeTierType")})
        rated = []
        for u in usages:
            try:
                limit = float(u.get("limit") or 0)
                actual = float(u.get("actualUsageAmount") or 0)
            except (TypeError, ValueError):
                continue
            ratio = actual / limit if limit > 0 else 0.0
            svc = re.sub(r"^(Amazon|AWS)\s+", "", u.get("service") or "")
            rated.append((ratio, f"{svc} {u.get('usageType') or ''} {actual:g}/{limit:g}".strip()
                          + (" ⚠️" if ratio >= 0.85 else "")))
        rated.sort(key=lambda x: -x[0])
        head = f"{len(usages)} 项 {'/'.join(types) or 'Free Tier'}"
        return head + (" · 用量最高：" + " · ".join(s for _, s in rated[:top]) if rated else "")

    def list_credits(self):
        today = datetime.now(timezone.utc).date()
        start = today.replace(day=1).isoformat()
        end = (today + timedelta(days=1)).isoformat()      # CE End 排他，取明天以含今天
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for a in deploy_config.aws_accounts():
            profile = a["profile"]
            china = a.get("partition") == "china"
            region = "cn-northwest-1" if china else "us-east-1"          # 三个全局服务的分区端点，同 list_billing
            bd = _aws_json(profile, region, ["budgets", "describe-budgets", "--account-id", profile])
            budget = self._summarize_budgets(None if bd is None else (bd.get("Budgets") or []))
            ft = _aws_json(profile, region, ["freetier", "get-free-tier-usage"])
            free_tier = self._summarize_free_tier(None if ft is None else (ft.get("freeTierUsages") or []))
            ce = _aws_json(profile, region, [
                "ce", "get-cost-and-usage",
                "--time-period", f"Start={start},End={end}",
                "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
                "--filter", json.dumps({"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}),
            ])
            credit, credit_ccy = None, None
            if ce is not None:
                credit = 0.0
                for r in ce.get("ResultsByTime", []):
                    tot = (r.get("Total") or {}).get("UnblendedCost") or {}
                    try:
                        credit += abs(float(tot.get("Amount") or 0))   # credit 在 CE 里是负数抵扣项
                    except (TypeError, ValueError):
                        pass
                    credit_ccy = credit_ccy or tot.get("Unit")
                credit = round(credit, 2)
            if bd is None and ft is None and ce is None:   # 三路全失败：凭证失效/CLI 缺失/全无权限 → 不造假行
                print(f"  ⚠️ AWS {profile} 账务：budgets / freetier / ce 全部查询失败（凭证失效或缺权限），跳过该账号",
                      file=sys.stderr)
                continue
            currency = credit_ccy or ("CNY" if china else "USD")
            out.append({
                "cloud": "aws", "account": profile, "account_name": f"AWS {profile}",
                # AWS 全是后付费；credit 列放的是「本月已抵扣」而非剩余余额，写进 plan 提醒读者
                "plan": "PayAsYouGo" + ("（credit=本月已抵扣，非余额）" if credit is not None else ""),
                "state": "Active",                         # 走到这里至少一路查询成功 = 凭证有效、账号在用
                "credit": credit, "credit_currency": (currency if credit is not None else None),
                "free_tier": free_tier, "free_tier_end": None, "free_tier_days": None,   # AWS 不直接给到期日
                "spending_limit": "Off",                   # AWS 没有超额停服，预算只告警不断供
                "budget": budget,
                "currency": currency,
                "fetched_at": fetched,
            })
        out.sort(key=lambda x: x["account"])
        return out

    def list_domains(self):
        out = []
        now = datetime.now(timezone.utc)
        for profile in deploy_config.route53_profiles():
            data = _aws_json(profile, "us-east-1", ["route53domains", "list-domains"])
            if not data:
                continue
            for x in data.get("Domains", []):
                exp = x.get("Expiry")
                days, expd = None, ""
                if exp:
                    try:
                        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        days, expd = (dt - now).days, str(exp)[:10]
                    except ValueError:
                        pass
                out.append({
                    "domain": x.get("DomainName"), "type": "gTLD",
                    "registrar": "AWS Route53", "account": profile,
                    "reg_date": "", "expire_date": expd, "days_left": days,
                    "note": "自动续费 ✓" if x.get("AutoRenew") else "未自动续费 ⚠",
                })
        return out
