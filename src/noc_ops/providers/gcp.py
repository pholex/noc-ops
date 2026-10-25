"""Google Cloud 驱动（Compute / Storage / IAM / AI 密钥 / Cloud SQL / Functions·Run / Domains·DNS，shell 出 gcloud）。

展示（只读）：list_instances（Compute Engine）/ list_storage（GCS 桶）/ list_iam（项目 IAM 绑定）
/ list_credits（结算账号状态·预算，需 SA 在结算账号上有 roles/billing.viewer）
/ list_apikeys（Cloud API Keys 元数据——AI Studio 的 Gemini key、Vertex AI Express key 本质都是它；只盘点，绝不取密钥串）
/ list_databases（Cloud SQL 实例）/ list_serverless（Cloud Functions 2nd gen + Cloud Run 服务）
/ list_domains（Cloud Domains 注册域名 + Cloud DNS 托管区）。
这些 API（sqladmin / cloudfunctions / run / domains / dns）项目没启用时 gcloud 直接报错，本模块只提示、
**绝不替用户启用 API**（启用是写操作）。
生命周期（第二阶段）：reboot_argv（reset 硬重启，免确认自动跑）/ status_argv；启停经通用
gcloud 通道走"确认后执行"（is_read/is_reboot 分类器交给 agent/MCP 闸门）。
多项目：见 ops-config.toml 的 [[gcp.accounts]]（每个含 project，可选 configuration=gcloud 命名配置）。
GCP 的"账号"= 项目（project id），故记录里 account/profile 一律填 project id。
认证沿用本机已配好的 gcloud（`gcloud auth login` / ADC），noc 不碰任何密钥。
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

from ..collection import observed
from .. import deploy_config
from ..aws_safety import is_read_only_gcp, is_reboot_gcp
from .base import Provider, resolve_keyfile

_FLAG = "🌐"   # GCP 多为多区域服务，用地球；具体 region 显示在 region 列


def _summarize_budgets(buds):
    """`gcloud billing budgets list --format json` → 一句话（渲染进「预算告警」列）。

    None（查询失败/无权限）→ None；[]（有权限但没设预算）→ ""（渲染层据此标「⚠️ 未设」）；
    否则每条「名称 金额 @阈值%」用「；」拼接。金额取 specifiedAmount，未指定则是「上期实际花费」口径。
    """
    if buds is None:
        return None
    parts = []
    for b in buds:
        amt = b.get("amount") or {}
        sa = amt.get("specifiedAmount")
        if sa:
            money = f"{sa.get('units') or 0} {sa.get('currencyCode') or ''}".strip()
        else:
            money = "上期实际花费" if "lastPeriodAmount" in amt else "?"
        pct = "/".join(f"{round(float(r.get('thresholdPercent') or 0) * 100)}%"
                       for r in (b.get("thresholdRules") or []))
        parts.append(f"{b.get('displayName') or '预算'} {money}" + (f" @{pct}" if pct else ""))
    return "；".join(parts)


def _is_google_managed_sa(email):
    """是不是 Google 托管的「服务代理 / 默认 SA」——每启用一个 API 自动建（代表 Google 各服务
    操作你的资源），非你创建、不该删，对运维/安全审查是噪音。NOC 的 IAM tab 默认不显示这些，
    只留人类用户 + 你自建的项目 SA（`<name>@<项目id>.iam.gserviceaccount.com`）。

    覆盖：service-<projnum>@…（服务代理，如 compute-system/container-engine-robot/containerregistry）、
    @gcp-sa-*（如 pubsub/notebooks/cloudbuild/cloudaicompanion serviceAgent）、
    <projnum>@cloudservices|cloudbuild（Google APIs / 旧版 Build 默认 SA）。
    """
    local, _, domain = email.partition("@")
    if not domain.endswith("gserviceaccount.com"):
        return False
    return (local.startswith("service-")
            or "gcp-sa-" in domain
            or domain.endswith("-system.iam.gserviceaccount.com")
            or "-robot" in domain
            or domain in ("cloudservices.gserviceaccount.com", "cloudbuild.gserviceaccount.com"))


@observed(result="nullable")
def _gcloud(account, args, timeout=60):
    """跑一条 gcloud/gsutil 命令（带 --project/可选 --configuration），解析 JSON。

    失败返回 None，并**打一行诊断到 stderr**（不静默吞）——否则一次临时的 gcloud
    掉登录会让看板上 GCP 资源"凭空消失"，看不出是认证问题。区分三态：
    成功有数据 / 成功空列表（rc=0，返回 []）/ 调用失败（rc≠0 或非 JSON，返回 None + 诊断）。
    """
    proj = account.get("project", "?")
    action = " ".join(a for a in args[1:4] if not a.startswith("-"))   # 如 "storage buckets" / "projects get-iam-policy"
    cmd = list(args)
    if account.get("configuration"):
        cmd += ["--configuration", account["configuration"]]
    # --quiet + 关 stdin：项目没启用某 API 时 gcloud 会**交互提问**「API not enabled, enable and retry? (y/N)」，
    # stdin 还开着就一直等到 timeout（2026-10-25 实测 sql/functions/run/domains 全部挂满 180s），还可能
    # 被顺手答 y 启用 API——一个写操作。--quiet 让它取默认答案 N 直接报错，stdin 关掉再兜一层。
    cmd.append("--quiet")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠️ GCP {proj} {action}：跑 gcloud 失败（{e}）", file=sys.stderr)
        return None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        tail = (msg[0] if msg else f"exit {r.returncode}")[:160]
        low = (r.stderr or "").lower()
        # 按错误类别给可操作的提示。注意 PERMISSION_DENIED 的报错里也含 "authenticated as …"，
        # 所以不能拿裸 "auth" 判掉登录，否则缺权限会被误导成「跑 gcloud auth login」。
        # 「API 未启用」要先判——它的报错同时带 "does not have permission to access …" 字样，
        # 后判会被误归成缺权限。
        if "not enabled" in low or "has not been used in project" in low:
            hint = "（该项目未启用此 API：`gcloud services enable <api>.googleapis.com --project <P>`）"
        elif "does not have permission" in low or "permission_denied" in low or "permission denied" in low:
            hint = "（当前身份缺该资源权限——结算账号级要 roles/billing.viewer，项目级要对应 Viewer）"
        elif "reauth" in low or "credential" in low or "invalid_grant" in low or "not logged in" in low:
            hint = "（gcloud 未登录/令牌过期？跑 `gcloud auth login`）"
        else:
            hint = ""
        print(f"  ⚠️ GCP {proj} {action}：gcloud 失败——{tail}{hint}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"  ⚠️ GCP {proj} {action}：gcloud 输出非 JSON，跳过", file=sys.stderr)
        return None


def _mem_mb(s):
    """Cloud Run / Functions v2 的内存串 → MB 整数：'256M' / '256Mi' → 256，'1G' / '1Gi' → 1024，
    纯数字按字节算；None / 解析不了 → None（渲染 —）。Google 两处都把 M 当 MiB 用，这里不区分 M/Mi。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?[Bb]?\s*", str(s))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    scale = {"": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1048576}[unit]
    return int(round(n * scale))


def _days_until(iso, now):
    """RFC3339 时间串 → 距 now 的天数（int）；空/解析失败 → None。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - now).days


def _sql_engine(ver):
    """Cloud SQL databaseVersion → 人读引擎名：POSTGRES_15 → "PostgreSQL 15"，MYSQL_8_0 → "MySQL 8.0"，
    SQLSERVER_2019_STANDARD → "SQL Server 2019 Standard"。数字段用 . 连，剩下的词是版本档。"""
    if not ver:
        return "—"
    fam, _, rest = str(ver).partition("_")
    name = {"POSTGRES": "PostgreSQL", "MYSQL": "MySQL", "SQLSERVER": "SQL Server"}.get(fam, fam.title())
    parts = rest.split("_") if rest else []
    nums = ".".join(p for p in parts if p.isdigit())
    words = [p.title() for p in parts if not p.isdigit()]
    return " ".join(x for x in [name, nums, *words] if x)


def _tld_type(domain):
    """两字母顶级域算 ccTLD，其余 gTLD（与 Route53 行的 type 口径一致）。"""
    tld = (domain or "").rstrip(".").rsplit(".", 1)[-1]
    return "ccTLD" if len(tld) == 2 else "gTLD"


class GcpProvider(Provider):
    cloud = "gcp"
    cli = "gcloud"
    display = "Google Cloud"
    flag = _FLAG
    order = 4                      # AWS=0 阿里=1 腾讯=2 火山=3 GCP=4

    is_read = staticmethod(is_read_only_gcp)
    is_reboot = staticmethod(is_reboot_gcp)

    @staticmethod
    def _accounts():
        return deploy_config.cloud_accounts("gcp")

    def _config_args(self, inst):
        """该实例所属账号若配了 gcloud 命名 configuration，附 --configuration（否则用活动凭据）。"""
        for a in self._accounts():
            if a.get("project") == inst.get("profile") and a.get("configuration"):
                return ["--configuration", a["configuration"]]
        return []

    def reboot_argv(self, inst):
        # GCE 无优雅重启 API：reset = 硬重启（power-cycle，RAM 丢、磁盘留）。按实例名 + zone + 项目。
        return ["gcloud", "compute", "instances", "reset", inst["name"],
                "--zone", inst["az"], "--project", inst["profile"]] + self._config_args(inst)

    def status_argv(self, inst):
        return ["gcloud", "compute", "instances", "describe", inst["name"],
                "--zone", inst["az"], "--project", inst["profile"],
                "--format", "value(name,status,zone)"] + self._config_args(inst)

    # —— Compute Engine 实例 ——
    def list_instances(self, network=None):
        out = []
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            # 可选 ssh_user / ssh_key（同阿里云/OCI 约定）：私钥 ~/.ssh/<project>/<ssh_key>.pem；
            # 公钥需事先注入实例或项目 metadata 的 ssh-keys（GCE 不像 EC2 有创建时绑定的 KeyPair）。
            kf = resolve_keyfile(proj, a.get("ssh_key")) if a.get("ssh_key") else None
            data = _gcloud(a, ["gcloud", "compute", "instances", "list",
                               "--project", proj, "--format", "json"])
            if not data:
                continue
            for i in data:
                zone = (i.get("zone") or "").rsplit("/", 1)[-1]          # …/zones/us-central1-a → us-central1-a
                region = re.sub(r"-[a-z]$", "", zone)                    # us-central1-a → us-central1
                mtype = (i.get("machineType") or "").rsplit("/", 1)[-1]  # …/machineTypes/e2-medium → e2-medium
                nics = i.get("networkInterfaces") or []
                prv = nics[0].get("networkIP") if nics else None
                pub = None
                if nics and nics[0].get("accessConfigs"):
                    pub = nics[0]["accessConfigs"][0].get("natIP")
                status = (i.get("status") or "").upper()                 # RUNNING / TERMINATED / STOPPING…
                state = {"RUNNING": "running", "TERMINATED": "stopped"}.get(status, status.lower())
                name = i.get("name") or i.get("id")
                out.append({
                    "name": name, "id": str(i.get("id") or name),
                    "profile": proj, "region": region, "region_display": region, "flag": self.flag,
                    "type": mtype or "GCE", "state": state, "az": zone,
                    "private_ip": prv, "public_ip": pub,
                    "key_name": a.get("ssh_key"), "ssh_alias": "gcp-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name),
                    "ssh_user": a.get("ssh_user"), "key_file": kf or "",
                    # verified 专指"已真实 SSH 探测"；只读展示不探测，统一 False
                    "connect": "direct", "proxyjump": None, "verified": False, "cloud": "gcp",
                })
        return out

    # —— 账务 / 额度（结算账号级）——
    def list_credits(self):
        """一个项目一行；结算账号信息多项目共用时只查一次。

        权限分两层：项目级（`billing projects describe`）只需项目 Viewer，能看到「绑了哪个结算账号、
        是否启用」；结算账号级（`billing accounts describe` / `budgets list`）需要 SA 在**结算账号**上有
        roles/billing.viewer——没有时 state/currency/budget 为 None（渲染 —），_gcloud 会打一行诊断。
        两个 API（cloudbilling / billingbudgets）都要在项目里启用过。
        """
        out = []
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cache = {}
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            info = _gcloud(a, ["gcloud", "billing", "projects", "describe", proj, "--format", "json"])
            if not info:               # API 未启用 / 无权限 / 掉登录：_gcloud 已打诊断，跳过
                continue
            ba = (info.get("billingAccountName") or "").rsplit("/", 1)[-1] or None
            enabled = bool(info.get("billingEnabled"))
            acct, budget = None, None
            if ba:
                if ba not in cache:
                    cache[ba] = (
                        _gcloud(a, ["gcloud", "billing", "accounts", "describe",
                                    f"billingAccounts/{ba}", "--format", "json"]),
                        _gcloud(a, ["gcloud", "billing", "budgets", "list",
                                    "--billing-account", ba, "--format", "json"]),
                    )
                acct, buds = cache[ba]
                budget = _summarize_budgets(buds)
            out.append({
                "cloud": "gcp", "account": proj,
                "account_name": (acct or {}).get("displayName") or ba or proj,
                # GCP 项目只有「绑了按量结算」与「没绑」两态，没有 Azure 那种 quotaId 档位
                "plan": "PayAsYouGo" if enabled else "Unlinked",
                "state": None if acct is None else ("Open" if acct.get("open") else "Closed"),
                # 试用赠金只在控制台可见，Billing API 不暴露余额 → None（＝拿不到，不是 0）
                "credit": None, "credit_currency": None,
                # Always Free 是常驻品类配额（e2-micro×1 / 30GB 标准盘 / 5GB US 桶…），没有到期日
                "free_tier": "Always Free", "free_tier_end": None, "free_tier_days": None,
                # GCP 没有超额停服机制：超出免费额度直接扣款，预算告警也只告警不断供
                "spending_limit": "Off",
                "budget": budget,
                "currency": (acct or {}).get("currencyCode"),
                "fetched_at": fetched,
            })
        out.sort(key=lambda c: c["account"])
        return out

    # —— Cloud Storage 桶 ——
    def list_storage(self):
        out = []
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            data = _gcloud(a, ["gcloud", "storage", "buckets", "list",
                               "--project", proj, "--format", "json"])
            if not data:
                continue
            for b in data:
                loc = (b.get("location") or "").lower()
                created = (b.get("creation_time") or b.get("timeCreated") or b.get("createTime") or "")[:10]
                name = b.get("name")
                out.append({
                    "name": name, "cloud": "gcp", "account": proj,
                    "region": loc, "region_display": (b.get("location") or "").title(),
                    "domain": f"{name}.storage.googleapis.com",
                    "created": created,
                })
        out.sort(key=lambda s: (s["account"], s["name"] or ""))
        return out

    # —— Cloud SQL 实例（只读）——
    def list_databases(self):
        """一实例一行，字段对齐 RDS 行（engine/class/storage/state/endpoint）。需项目启用 sqladmin.googleapis.com。

        Cloud SQL 的「停止」不是独立 state：停掉的实例 state 仍是 RUNNABLE，只是 settings.activationPolicy=NEVER，
        这里折成 stopped，别被 RUNNABLE 骗了。endpoint 取公网 IP（ipAddresses 里 type=PRIMARY）；只有内网 IP 的
        实例 endpoint 为 —，内网地址进 note。
        """
        out = []
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            data = _gcloud(a, ["gcloud", "sql", "instances", "list", "--project", proj, "--format", "json"])
            if not data:               # API 未启用 / 无权限 / 空：_gcloud 已打诊断
                continue
            for db in data:
                st = db.get("settings") or {}
                ips = {(ip.get("type") or "").upper(): ip.get("ipAddress") for ip in (db.get("ipAddresses") or [])}
                pub, prv = ips.get("PRIMARY"), ips.get("PRIVATE")
                raw = (db.get("state") or "").upper()
                if raw == "RUNNABLE":
                    state = "stopped" if (st.get("activationPolicy") or "").upper() == "NEVER" else "available"
                else:
                    state = raw.lower() or "unknown"       # PENDING_CREATE / MAINTENANCE / SUSPENDED / FAILED…
                size = st.get("dataDiskSizeGb")
                disk = (st.get("dataDiskType") or "").replace("PD_", "")   # PD_SSD → SSD
                avail = (st.get("availabilityType") or "").upper()         # REGIONAL=高可用 / ZONAL
                note = " · ".join(x for x in [
                    "高可用 (REGIONAL)" if avail == "REGIONAL" else ("单区 (ZONAL)" if avail == "ZONAL" else ""),
                    f"内网 {prv}" if prv else "",
                    db.get("connectionName") or "",
                ] if x)
                region = db.get("region") or ""
                out.append({
                    "name": db.get("name"), "cloud": "gcp", "account": proj,
                    "region": region, "region_display": region, "flag": self.flag,
                    "engine": _sql_engine(db.get("databaseVersion")),
                    "class": st.get("tier") or "—",
                    "storage": (f"{size} GB {disk}".strip() if size else "—"),
                    "state": state, "endpoint": pub or "—",
                    "multi_az": avail == "REGIONAL",
                    "created": (db.get("createTime") or "")[:10],
                    "note": note,
                })
        out.sort(key=lambda d: (d["account"], d["region"], d["name"] or ""))
        return out

    # —— 无服务器：Cloud Functions（2nd gen，含 1st gen）+ Cloud Run 服务（只读）——
    def list_serverless(self):
        """字段对齐 Lambda 行（runtime/memory/timeout/code_mb/last_modified）；code_mb 两类都拿不到 → None。

        `gcloud functions list --v2`：统一用 v2 API/格式（不带 --v2 时 1st/2nd gen 各返回各自格式，
        字段名不同），1st gen 函数也会出现、environment=GEN_1。每个 2nd gen 函数背后都是一个 Cloud Run
        服务（label goog-managed-by=cloudfunctions），列 Run 服务时把这些去掉，否则一个函数算两行。
        Run 行 runtime 固定 "Cloud Run"（跑的是容器镜像，没有语言运行时概念），镜像进 note。
        需项目启用 cloudfunctions.googleapis.com / run.googleapis.com。
        """
        out = []
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            fns = _gcloud(a, ["gcloud", "functions", "list", "--v2", "--project", proj, "--format", "json"])
            backing = set()                                   # 2nd gen 函数背后的 Run 服务全名
            for fn in fns or []:
                full = fn.get("name") or ""                   # projects/P/locations/R/functions/N
                m = re.search(r"/locations/([^/]+)/functions/([^/]+)$", full)
                region, name = (m.group(1), m.group(2)) if m else ("", full)
                sc, bc = fn.get("serviceConfig") or {}, fn.get("buildConfig") or {}
                if sc.get("service"):
                    backing.add(sc["service"])
                gen1 = (fn.get("environment") or "").upper() == "GEN_1"
                out.append({
                    "name": name, "cloud": "gcp", "account": proj,
                    "region": region, "region_display": region, "flag": self.flag,
                    "kind": "function",
                    "runtime": (bc.get("runtime") or "—") + (" (gen1)" if gen1 else ""),
                    "memory": _mem_mb(sc.get("availableMemory")),
                    "timeout": sc.get("timeoutSeconds"),
                    "code_mb": None,                          # v2 API 不给包大小
                    "last_modified": (fn.get("updateTime") or "")[:10],
                    "state": (fn.get("state") or "").lower() or None,
                    "note": sc.get("uri") or fn.get("url") or "",
                })
            svcs = _gcloud(a, ["gcloud", "run", "services", "list", "--project", proj, "--format", "json"])
            for s in svcs or []:                              # Knative 风格：metadata / spec / status
                md, spec, status = s.get("metadata") or {}, s.get("spec") or {}, s.get("status") or {}
                labels, ann = md.get("labels") or {}, md.get("annotations") or {}
                region = labels.get("cloud.googleapis.com/location") or ""
                name = md.get("name") or ""
                full = f"projects/{proj}/locations/{region}/services/{name}"
                if labels.get("goog-managed-by") == "cloudfunctions" or full in backing:
                    continue                                  # 函数的影子服务，上面已计
                tmpl = ((spec.get("template") or {}).get("spec") or {})
                cont = (tmpl.get("containers") or [{}])[0]
                limits = (cont.get("resources") or {}).get("limits") or {}
                # 最后修改：Ready 条件的翻转时间（每次新 revision 上线都会刷），兜底创建时间
                ready = [c.get("lastTransitionTime") for c in (status.get("conditions") or [])
                         if c.get("type") == "Ready" and c.get("lastTransitionTime")]
                modified = ready[0] if ready else md.get("creationTimestamp") or ""
                image = cont.get("image") or ""
                out.append({
                    "name": name, "cloud": "gcp", "account": proj,
                    "region": region, "region_display": region, "flag": self.flag,
                    "kind": "run",
                    "runtime": "Cloud Run",
                    "memory": _mem_mb(limits.get("memory")),
                    "timeout": tmpl.get("timeoutSeconds"),
                    "code_mb": None,
                    "last_modified": modified[:10],
                    "state": None,
                    "note": " · ".join(x for x in [image.rsplit("/", 1)[-1] if image else "",
                                                   status.get("url") or "",
                                                   f"by {ann['serving.knative.dev/lastModifier']}"
                                                   if ann.get("serving.knative.dev/lastModifier") else ""] if x),
                })
        out.sort(key=lambda x: (x["account"], x["region"], x["name"] or ""))
        return out

    # —— 域名：Cloud Domains 注册 + Cloud DNS 托管区（只读）——
    def list_domains(self):
        """注册域名一行（有到期日 / 剩余天数 / 续费方式），Cloud DNS 托管区一行（type "DNS zone"，无到期日）；
        同一域名既注册又托管只留注册那条。需项目启用 domains.googleapis.com / dns.googleapis.com，
        没启用的那类对该项目为空（_gcloud 提示一次），不影响另一类。
        注：Cloud Domains 2025 年起不再新注册（存量迁 Squarespace），list 对存量仍有效。"""
        out = []
        now = datetime.now(timezone.utc)
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            registered = set()
            regs = _gcloud(a, ["gcloud", "domains", "registrations", "list",
                               "--project", proj, "--format", "json"])
            for r in regs or []:
                dom = (r.get("domainName") or (r.get("name") or "").rsplit("/", 1)[-1]).rstrip(".").lower()
                if not dom:
                    continue
                registered.add(dom)
                ms = r.get("managementSettings") or {}
                auto = (ms.get("renewalMethod") or "").upper() == "AUTOMATIC_RENEWAL"
                state = (r.get("state") or "").upper()
                notes = ["自动续费 ✓" if auto else "未自动续费 ⚠"]
                if state and state != "ACTIVE":
                    notes.append(f"状态 {state} ⚠")          # SUSPENDED / EXPIRED / EXPORTED…
                if r.get("issues"):
                    notes.append("有待处理问题 ⚠ " + ",".join(r["issues"]))
                exp = r.get("expireTime")
                out.append({
                    "domain": dom, "type": _tld_type(dom), "registrar": "Google Cloud", "account": proj,
                    "reg_date": (r.get("createTime") or "")[:10],
                    "expire_date": (exp or "")[:10], "days_left": _days_until(exp, now),
                    "note": " · ".join(notes),
                })
            zones = _gcloud(a, ["gcloud", "dns", "managed-zones", "list",
                                "--project", proj, "--format", "json"])
            for z in zones or []:
                dom = (z.get("dnsName") or "").rstrip(".").lower()
                if not dom or dom in registered:
                    continue                                  # 注册行已覆盖，不重复
                vis = (z.get("visibility") or "public").lower()
                dnssec = ((z.get("dnssecConfig") or {}).get("state") or "").lower()
                out.append({
                    "domain": dom, "type": "DNS zone", "registrar": "Google Cloud", "account": proj,
                    "reg_date": (z.get("creationTime") or "")[:10],
                    "expire_date": "", "days_left": None,
                    "note": " · ".join(x for x in [f"Cloud DNS 托管区 {z.get('name') or ''}".strip(),
                                                   "私有" if vis == "private" else "公开",
                                                   "DNSSEC on" if dnssec == "on" else ""] if x),
                })
        return out

    # —— AI 密钥（Cloud API Keys 中 AI 用途的那部分）——
    # AI Studio 建的 Gemini key、Vertex AI Express key、Maps key……本质都是项目下的 Cloud API key，
    # 靠 restrictions.apiTargets 区分用途。这里**只列元数据**（名称·限定 API·绑定 SA·客户端限制·
    # 创建时间），**绝不调 `api-keys get-key-string`**——密钥串不该出现在 data.json / inventory 里。
    # 需项目启用 apikeys.googleapis.com（未启用时 _gcloud 会给「启用 API」提示，本项目行为空）。
    _API_PURPOSE = {
        "generativelanguage.googleapis.com": "Gemini API",   # AI Studio 的 Gemini key
        "aiplatform.googleapis.com": "Vertex AI",            # Vertex AI Express key（也跑 Gemini）
    }
    # 只盘点 AI 用途的 key：限定到上面任一 AI 服务的，或**无限制**的（什么都能调，当然也能调 Gemini，
    # 且正被 Google 封禁——必须暴露）。只限定到 Maps / YouTube 等非 AI 服务的 key 不进看板。
    _AI_SERVICES = frozenset(_API_PURPOSE)

    @classmethod
    def _key_purpose(cls, targets):
        """apiTargets 服务名列表 → 用途短名；无限制 key 返回「无限制」（渲染层标红，Google 已在封禁闲置的无限制 key）。"""
        if not targets:
            return "无限制"
        names = [cls._API_PURPOSE.get(svc) or svc.replace(".googleapis.com", "") for svc in targets]
        return " / ".join(dict.fromkeys(names))

    @staticmethod
    def _client_restriction(restr):
        """四选一的客户端限制类型（Referrer / IP / Android / iOS），没有返回 ""。"""
        for field, label in (("browserKeyRestrictions", "Referrer"), ("serverKeyRestrictions", "IP"),
                             ("androidKeyRestrictions", "Android"), ("iosKeyRestrictions", "iOS")):
            if restr.get(field):
                return label
        return ""

    def list_apikeys(self):
        out = []
        now = datetime.now(timezone.utc)
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            data = _gcloud(a, ["gcloud", "services", "api-keys", "list",
                               "--project", proj, "--format", "json"])
            if not data:
                continue
            for k in data:
                restr = k.get("restrictions") or {}
                targets = [t.get("service") for t in (restr.get("apiTargets") or []) if t.get("service")]
                if targets and not (self._AI_SERVICES & set(targets)):
                    continue                                   # 非 AI 用途的 key，不在本 tab 范围
                created = (k.get("createTime") or "")[:10]
                age = None
                if created:
                    try:
                        age = (now - datetime.fromisoformat(created).replace(tzinfo=timezone.utc)).days
                    except ValueError:
                        pass
                sa = k.get("serviceAccountEmail") or None
                out.append({
                    "name": k.get("displayName") or k.get("uid"), "id": k.get("uid"),
                    "cloud": "gcp", "account": proj,
                    "region": "global", "region_display": "Global", "flag": _FLAG,
                    "purpose": self._key_purpose(targets), "targets": targets,
                    "unrestricted": not targets,
                    # 2026-05-28 起 AI Studio 新建的都是绑服务账号的「授权 key」；没绑的是旧式标准 key（淘汰路径）
                    "service_account": sa, "key_type": "授权 key" if sa else "标准 key",
                    "status": "Active",                        # Cloud API key 没有停用态，存在即生效
                    "client_restriction": self._client_restriction(restr),
                    "created": created, "age_days": age,
                    "updated": (k.get("updateTime") or "")[:10],
                    "expires": None,                           # Cloud API key 不过期
                })
        out.sort(key=lambda r: (r["account"], r["name"] or ""))
        return out

    # —— 项目 IAM 绑定（按主体聚合角色）——
    _PRINCIPAL = {"user": "👤 用户", "serviceAccount": "🤖 服务账号", "group": "👥 组", "domain": "🌐 域"}

    @staticmethod
    def _parse_member(member):
        """解析 IAM member → (类型, 标识, 是否已删主体)。
        形如 `user:a@b` / `serviceAccount:x@…`；已删主体形如 `deleted:serviceAccount:x@…?uid=123`
        （删 SA/用户后绑定不会自动清，GCP 用 deleted: 前缀 + ?uid 尾标记，是该清理的"债"）。"""
        deleted = member.startswith("deleted:")
        body = member[len("deleted:"):] if deleted else member
        typ, _, ident = body.partition(":")
        ident = ident.split("?uid=")[0]                # 去掉已删主体的 ?uid=<num> 尾巴
        return typ, ident, deleted

    def list_iam(self):
        out = []
        for a in self._accounts():
            proj = a.get("project")
            if not proj:
                continue
            data = _gcloud(a, ["gcloud", "projects", "get-iam-policy", proj, "--format", "json"])
            if not data:
                continue
            roles_by_member = {}                       # member → [role 短名]
            for b in data.get("bindings", []):
                role = (b.get("role") or "").split("/")[-1]    # roles/owner → owner
                for m in b.get("members", []):
                    roles_by_member.setdefault(m, []).append(role)
            for member, roles in roles_by_member.items():
                typ, ident, deleted = self._parse_member(member)
                label = ("🗑 已删 " if deleted else "") + self._PRINCIPAL.get(typ, typ)
                out.append({
                    "name": ident or member, "cloud": "gcp", "account": proj,
                    # GCP IAM 无密钥/登录时间概念，这些 AWS 专属列留空（前端显示 —）
                    "created": "—", "last_used": "—", "key_count": None, "oldest_key_days": None,
                    # IAM tab 新增「角色/备注」列承载 GCP 语义：主体类型 + 角色清单
                    "detail": f"{label} · {len(roles)} 角色：" + ", ".join(sorted(roles)),
                    # 标记 Google 托管的"活着的"系统 SA（噪音）→ 前端隐藏；已删主体不算系统，要被看到（清理债）
                    "system": (not deleted and typ == "serviceAccount" and _is_google_managed_sa(ident)),
                    "deleted": deleted,
                })
        out.sort(key=lambda x: (x["account"], x["name"] or ""))
        return out
