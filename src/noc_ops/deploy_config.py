"""部署专属配置（非密）的单一来源 loader。

读取 `<workspace>/ops-config.toml`；缺失时回退打包默认 `assets/ops-config.default.toml`。
按工作区路径缓存（测试切 NOC_HOME 后 reload() 即可重载）。各模块**运行时**调用访问函数，
不在 import 期绑定常量——配置改了无需重载、便于测试隔离。

安全：本配置只含非密部署信息（账号 ID/区域/别名/ARN/profile 名/ssh 用户名/key 文件名），
绝不含 secret——见 ops-config.default.toml 头部。
"""
import tomllib
from importlib import resources

from . import netprobe
from . import paths

_CACHE = {}   # str(workspace) -> 原始 dict


def _raw() -> dict:
    key = str(paths.workspace_dir())
    if key not in _CACHE:
        f = paths.deploy_config_path()
        if f.exists():
            text = f.read_text(encoding="utf-8")
        else:
            text = (resources.files("noc_ops") / "assets" / "ops-config.default.toml").read_text(encoding="utf-8")
        _CACHE[key] = tomllib.loads(text)
    return _CACHE[key]


def reload():
    """清缓存（测试切 NOC_HOME / 改配置后调用）。"""
    _CACHE.clear()
    clear_account_scope()


def _split_key(k):        # "profile/region" -> (profile, region)
    p, _, r = k.partition("/")
    return (p, r)


def _role_arn(partition, profile, role_name):
    return f"arn:{partition}:iam::{profile}:role/{role_name}"



# ---- 账号收窄（`ops sync --account=`，跨云通用）----
_SCOPE = {}   # cloud -> 收窄键；不在表里 = 该云不收窄


def set_account_scope(cloud, key):
    """把本进程对某朵云的账号视野收窄到单个账号（key=None = 该云恢复全部账号）。

    所有采集路径都从这里取账号——AWS 走 `aws_accounts()` / `route53_profiles()`
    （`sync.build_scopes` 与 `providers/aws.py` 的每个 `list_*`），其余云走 `cloud_accounts()`
    （各 provider 的 `_accounts()`）——故只在这一处过滤，全链路即只扫该账号。
    只由 `ops sync --account=<账号>` 设置；它走 patch 式合并写回，不会用单账号的结果覆盖
    整份 data.json（其它账号/其它云的行原样沿用）。

    key：AWS 用 profile（就是账号 ID）；其余云既认 CLI profile 名（阿里云的 <profile>）也认账号 ID。
    """
    if key is None:
        _SCOPE.pop(cloud, None)
    else:
        _SCOPE[cloud] = key


def account_scope(cloud: str = "aws"):
    return _SCOPE.get(cloud)


def clear_account_scope():
    _SCOPE.clear()


# ---- AWS ----
def all_aws_accounts() -> list:
    """配置里的**全部** AWS 账号（不受 set_account_scope 收窄）——校验 --account 参数用。"""
    return [dict(a) for a in _raw().get("aws", {}).get("accounts", [])]


def aws_accounts() -> list:
    return [a for a in all_aws_accounts() if account_scope("aws") in (None, a["profile"])]


def scan_regions(partition: str) -> list:
    aws = _raw().get("aws", {})
    key = "scan_regions_china" if partition == "china" else "scan_regions_global"
    return list(aws.get(key, []))


def route53_profiles() -> list:
    profiles = list(_raw().get("aws", {}).get("route53_profiles", []))
    return [p for p in profiles if account_scope("aws") in (None, p)]


def aws_alias_prefix() -> dict:
    return {_split_key(k): v for k, v in _raw().get("aws", {}).get("alias_prefix", {}).items()}


def aws_jumps() -> dict:
    return {_split_key(k): tuple(v) for k, v in _raw().get("aws", {}).get("jumps", {}).items()}


# ---- 非 AWS 云（aliyun / tencent）----
def all_cloud_accounts(name: str, network: str | None = None) -> list:
    """某云的**全部**账号（不受 set_account_scope 收窄）。校验 --account 参数用；
    日常采集走 cloud_accounts()（会跟随收窄）。

    兼容两种写法：新式 `[[<name>.accounts]]`（多账号，如阿里云 <profile-a> + <profile-b>）；
    旧式单表 `[<name>]`（视为单账号，如腾讯云）。无配置则返回空列表。

    每个账号都会带上 `network` 网络域（"lan" / "tailscale"）：显式配置优先，没配就按
    url 自动推导（100.64/10 或 *.ts.net → tailscale）。传 `network=` 则只返回该域的账号——
    `ops sync --local` 与 `ops sync --tailscale` 靠它把两类机器分开扫（见 netprobe 模块头注）。
    """
    raw = _raw().get(name, {})
    if raw.get("accounts"):                      # 新式：多账号数组
        items = [dict(a) for a in raw["accounts"]]
    else:                                        # 旧式：整张单表当一个账号
        single = {k: v for k, v in raw.items() if k != "accounts"}
        items = [single] if single else []
    for a in items:
        a["regions"] = {r: tuple(v) for r, v in a.get("regions", {}).items()}
        a["network"] = a.get("network") or netprobe.network_of(a.get("url", ""))
    if network:
        items = [a for a in items if a["network"] == network]
    return items


def cloud_accounts(name: str, network: str | None = None) -> list:
    """某云的账号列表，**跟随 set_account_scope 的收窄**（`ops sync --account=` 只扫一个账号）。

    收窄键既认 CLI profile 名也认账号 ID——阿里云两种写法（<profile> / 1234567890123456）都行。
    """
    key = account_scope(name)
    return [a for a in all_cloud_accounts(name, network)
            if key in (None, a.get("profile"), a.get("account"), a.get("project"), a.get("subscription"),
                       name if not (a.get("profile") or a.get("account")) else None)]


def cloud(name: str) -> dict:
    """某云的**主账号**（=第一个）配置。单账号语义的旧调用点用；多账号见 cloud_accounts()。"""
    accs = cloud_accounts(name)
    return accs[0] if accs else {}


def nonaws_region_display() -> dict:
    """合并 aliyun+tencent+volcengine 全部账号的 regions → {region: (显示名, 旗帜)}（取代 _NONAWS_REGION）。"""
    out = {}
    for n in ("aliyun", "tencent", "volcengine"):
        for a in cloud_accounts(n):
            out.update(a.get("regions", {}))
    return out


# ---- 实例用途备注（人工维护）----
def instance_notes() -> dict:
    """`[instance_notes]` 段：实例 ID → 一句话用途，渲染进 inventory 的「用途」列与 data.json。

    inventory.md 每次 `ops sync` 整份重写，直接写在生成物里的备注必被抹掉——所以人工
    维护的用途说明只落在这份配置里。键用**实例 ID** 而非名字：实例改名后仍然对得上。
    """
    return {str(k): str(v).strip() for k, v in _raw().get("instance_notes", {}).items()}


# ---- 数据库用途备注（人工维护）----
def database_notes() -> dict:
    """`[database_notes]` 段：数据库名（RDS 实例标识 / Supabase 项目名）→ 一句话用途与授权备忘，
    渲染进 inventory 数据库表的「用途」列与 data.json 的 databases[].note。

    与 instance_notes 同理：inventory.md 每次 sync 整份重写，备注只在这里活得过下一次。
    数据库没有独立于名字的稳定 ID（RDS 的标识符就是名字，改名即换端点），所以键直接用名字。
    """
    return {str(k): str(v).strip() for k, v in _raw().get("database_notes", {}).items()}


def abandoned_domains() -> dict:
    """`[abandoned_domains]` 段：域名 → 放弃原因。到期不再续费、有意弃养的域名登记在此。

    域名到期不会立刻从账号消失（阿里云有**赎回保护期**，会继续挂着若干天），不登记的话
    这段时间每次 sync 都反复报同一条假告警，真正漏续的反而被淹没。登记后看板标「已放弃」，
    并从「最近到期 / 临期数」里剔除。域名真正移出账号后本条即成死配置，可删。
    """
    return {str(k).strip().lower(): str(v).strip()
            for k, v in _raw().get("abandoned_domains", {}).items()}


# ---- 已知出口网段（人工维护）----
def known_egress() -> dict:
    """`[known_egress]` 段：一句话标签 → CIDR 列表，供 `ops netprobe` 给出口 IP 定性。

    只用来登记**探测不出来的语义**——哪个网段是办公室、哪个是专线出局。自有云主机的
    公网 IP 不必写在这：那些从 data.json 自动反查得到。登记后，探到的出口 IP 落在这些
    段里就标成对应标签，落不到任何已知段才提示"未登记"（家宽 IP 每天变，天然未登记）。
    """
    out = {}
    for label, v in _raw().get("known_egress", {}).items():
        nets = [v] if isinstance(v, str) else list(v or [])
        out[str(label)] = [str(n).strip() for n in nets if str(n).strip()]
    return out


# ---- 计划任务 ----
def scheduler() -> dict:
    return dict(_raw().get("scheduler", {}))


def timezone() -> str:
    return scheduler().get("timezone", "Asia/Shanghai")


def display_tz() -> str:
    return scheduler().get("display_tz", "北京时间(UTC+8)")


def sched_accounts() -> dict:
    s = scheduler()
    rn = s.get("role_name", "noc-scheduler")
    return {a["profile"]: {"partition": a["partition"], "regions": list(a["regions"]),
                           "role": _role_arn(a["partition"], a["profile"], rn)}
            for a in s.get("accounts", [])}


def dispatch() -> dict:
    s = scheduler()
    d = dict(s.get("dispatch", {}))
    d["lambda"] = d.pop("lambda_arn", d.get("lambda", ""))   # TOML 键 lambda_arn → 代码用 "lambda"
    d["role"] = _role_arn("aws", d.get("account", ""), s.get("role_name", "noc-scheduler"))
    return d


def dispatch_clouds() -> set:
    return {(c["cloud"], c["account"]) for c in scheduler().get("dispatch_clouds", [])}


# ---- 插件/扩展的自有配置段（通用透传）----
def section(name: str) -> dict:
    """ops-config.toml 里任意顶层段 `[<name>]` 原样透传（拷贝）；没有则 {}。

    给经 entry point 挂入的插件子命令用（如 `[feishu_bot]`）：插件自己合并默认值、自己校验，
    核心只负责「读出来」，不为每个插件单独写访问器。"""
    return dict(_raw().get(name, {}))



# ---- 本地网络连通性预检 ----
def local_net_probe() -> list:
    """`ops sync --local` 前的**内网**存活探针目标 [host:port, ...]（任一通即算在内网）。

    无端口默认 22。返回空列表 = 未配置预检 → 视为始终通过（老行为，不做门控）。
    只管 LAN；Tailscale 域不看这个探针（它的门控是 `tailscale status`，见 netprobe.tailnet_up）。
    """
    v = _raw().get("local_net", {}).get("probe", [])
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in v]
