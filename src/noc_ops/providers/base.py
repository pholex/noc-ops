"""Provider 驱动基类 + 共享工具。

每朵云一个 Provider，知道：CLI 二进制、读/重启分类、reboot/status argv、
list_instances（拉实例，非 AWS 云实现；AWS 走 sync.build_scopes）、list_domains（拉域名）。

归一化实例记录 = 工作区 data.json 里的 instance dict，至少含：
  id · name · cloud · profile · region · region_display · flag · type · state ·
  private_ip · public_ip · ssh_alias · ssh_user · key_file · connect · proxyjump
"""
from pathlib import Path

# 私钥路径约定：~/.ssh/<profile>/<KeyName>.pem（账号 ID 分目录，文件名=KeyName）。
# AWS 与 腾讯/阿里 都用它推导私钥；覆盖表仅用于不符合约定的例外。
KEYFILE_OVERRIDE = {
    # "some-keyname": "~/.ssh/custom/path.pem",
}


def resolve_keyfile(profile, key_name):
    """按 ~/.ssh/<profile>/<KeyName>.pem 约定推导私钥路径（~ 形式）；不存在则查覆盖表。"""
    if not key_name:
        return None
    cand = Path.home() / ".ssh" / profile / f"{key_name}.pem"
    if cand.exists():  # 跟随软链
        return f"~/.ssh/{profile}/{key_name}.pem"
    return KEYFILE_OVERRIDE.get(key_name)


class Provider:
    cloud: str = ""        # "aws" | "tencent" | "aliyun"
    cli: str = ""          # CLI 二进制名："aws" | "tccli" | "aliyun"
    display: str = ""      # "AWS" | "腾讯云" | "阿里云"
    flag: str = ""         # 🇨🇳 等（非 AWS 用）
    order: int = 99        # 默认排序：AWS < 阿里 < 腾讯

    # —— CLI 读/写分类（子类用现成的 aws_safety 分类器覆盖）——
    def is_read(self, args: str) -> bool:
        return False

    def is_reboot(self, args: str) -> bool:
        return False

    # —— 动作命令（取代 mcp_server 的 if/elif）——
    def reboot_argv(self, inst: dict) -> list:
        raise NotImplementedError(f"{self.cloud} 未实现 reboot_argv")

    def status_argv(self, inst: dict) -> list:
        raise NotImplementedError(f"{self.cloud} 未实现 status_argv")

    # —— 拉取（sync 遍历调用）——
    def list_instances(self, network: str | None = None) -> list:
        """返回归一化实例记录。默认空（AWS 走 sync.build_scopes，故不在此实现）。

        `network`：只扫该网络域的账号（"lan" / "tailscale"），None = 全部。
        非本地网络的云（AWS/阿里/腾讯…走公网 API）忽略此参数。见 netprobe 模块头注。
        """
        return []

    def unreachable_accounts(self) -> list:
        """上次 list_instances() 里**连不上**的账号标识（= 记录中的 `_acct` 值）。

        本地网络 provider（sangfor/vmware/synology…）多账号时用：某账号本轮连不上，
        `refresh_local` 据此把该账号上次的记录**沿用**下来，而不是当"已删除"清掉；
        连得上的账号里缺失的机器才算真删除。多账号 provider 应在 list_instances 开头
        `self._unreachable = []`，账号连接失败时 `self._unreachable.append(<该账号的 _acct>)`。
        默认空（单账号/静态 provider 无需覆盖）。
        """
        return list(getattr(self, "_unreachable", []) or [])

    def list_domains(self) -> list:
        """返回域名记录（domain/registrar/type/account/expire_date/days_left/note）。默认空。"""
        return []

    def list_databases(self) -> list:
        """返回数据库记录（name/cloud/account/region/engine/class/storage/state/endpoint…）。默认空。"""
        return []

    def list_storage(self) -> list:
        """返回存储桶记录（name/cloud/account/region/created）。默认空。"""
        return []

    def list_serverless(self) -> list:
        """返回函数记录（name/cloud/account/region/runtime/memory/timeout/last_modified）。默认空。"""
        return []

    def list_cdn(self) -> list:
        """返回 CDN 分布记录（CloudFront 等：id/domain/aliases/origin/status/enabled/cert/price_class/last_modified/account）。默认空。"""
        return []

    def list_iam(self) -> list:
        """返回访问主体记录（IAM 用户：name/cloud/account/created/last_used/key_count/oldest_key_days）。默认空。"""
        return []

    def list_apikeys(self) -> list:
        """返回 AI 密钥**元数据**记录（name/id/cloud/account/purpose/targets/unrestricted/service_account(绑定身份：
        GCP 服务账号 / AWS IAM 用户)/key_type/status/client_restriction/created/age_days/updated/expires）。
        只盘点、永远不含密钥串。默认空。"""
        return []

    def list_messaging(self) -> list:
        """返回消息/事件资源记录（SQS/SNS/SES/EventBridge：name/kind/cloud/account/region/region_display/flag/detail）。默认空。"""
        return []

    def list_billing(self) -> list:
        """返回成本行（账号×服务×月份：cloud/account/service/month/amount/currency/period_start/partial/fetched_at）。默认空。"""
        return []

    def list_credits(self) -> list:
        """返回**账务/额度**行（一个账号一行）。默认空。

        与 list_billing 的分工：billing 是「花了多少」（账号×服务×月的流水），credits 是
        「还剩多少、超额会怎样」（账号级的**状态**）——信用余额、免费额度到期、超额保护、
        预算告警。两者数据源不同、更新频率不同，故分开两个键，别塞进 billing。

        归一化字段（拿不到的填 None，渲染层统一显示 —；别自己填 "—"，那样没法判空）：
          cloud · account（账号标识）· account_name · plan（付费类型）· state（账号状态）
          credit（信用余额数值，float；0 有意义，None=该云没有这个概念/拿不到）· credit_currency
          free_tier（免费额度名目）· free_tier_end（YYYY-MM-DD）· free_tier_days（剩余天数）
          spending_limit（"On"=超额停服 / "Off"=超额继续扣款 / None=无此概念）
          budget（预算告警一句话）· currency（结算币种）· fetched_at
        """
        return []
