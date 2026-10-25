"""Shared onboarding and diagnostic metadata. Credentials never belong here."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Cloud:
    label: str
    cli: str
    identity: str
    required: tuple
    login: str
    region: str = ""
    multiple: bool = True


CLOUDS = {
    "aws": Cloud("AWS", "aws", "profile", ("profile", "partition"), "aws configure --profile <profile>"),
    "aliyun": Cloud("阿里云", "aliyun", "profile", ("profile", "account"), "aliyun configure --profile <profile>", "cn-hangzhou"),
    "tencent": Cloud("腾讯云", "tccli", "account", ("account",), "tccli configure", "ap-nanjing", False),
    "volcengine": Cloud("火山引擎", "ve", "account", ("account",), "ve configure", "cn-beijing", False),
    "gcp": Cloud("Google Cloud", "gcloud", "project", ("project",), "gcloud auth login"),
    "azure": Cloud("Microsoft Azure", "az", "subscription", ("subscription",), "az login"),
    "oracle": Cloud("Oracle Cloud", "oci", "profile", ("profile", "compartment"), "oci setup config"),
    "cloudflare": Cloud("Cloudflare", "", "account", ("account",), "配置 token_env 指向的环境变量，或 macOS Keychain"),
    "supabase": Cloud("Supabase", "", "account", ("account",), "配置 token_env 指向的环境变量，或 macOS Keychain"),
}


def accounts(config, cloud):
    block = config.get(cloud, {})
    if not isinstance(block, dict):
        raise ValueError(f"{cloud} 必须是配置表")
    values = block.get("accounts")
    if values is None:
        values = [block] if block and cloud != "aws" else []
    if not isinstance(values, list) or any(not isinstance(a, dict) for a in values):
        raise ValueError(f"{cloud}.accounts 必须是账号列表")
    return values


def configured(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "<>\n\r")


def validate_account(cloud, account):
    spec = CLOUDS[cloud]
    errors = [f"缺少有效 {key}" for key in spec.required if not configured(account.get(key))]
    if cloud == "aws" and account.get("partition") not in ("global", "china"):
        errors.append("partition 必须是 global 或 china")
    regions = account.get("regions", {})
    if not isinstance(regions, dict) or any(not isinstance(v, (list, tuple)) or len(v) != 2 for v in regions.values()):
        errors.append("regions 必须是 区域 = [显示名, 旗帜] 的映射")
    if account.get("token_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(account["token_env"])):
        errors.append("token_env 必须是环境变量名，不能是 token 值")
    return errors
