"""Provider 注册表。加一朵云 = 加一个 <cloud>.py + 在这里注册一行。"""
from .aws import AwsProvider
from .tencent import TencentProvider
from .volcengine import VolcengineProvider
from .aliyun import AliyunProvider
from .gcp import GcpProvider
from .cloudflare import CloudflareProvider
from .supabase import SupabaseProvider
from .azure import AzureProvider
from .oracle import OracleProvider
from .local import LocalProvider
from .sangfor import SangforProvider
from .vmware import VMwareProvider
from .synology import SynologyProvider

PROVIDERS = {p.cloud: p for p in
             (AwsProvider(), AliyunProvider(), TencentProvider(), VolcengineProvider(), GcpProvider(),
              AzureProvider(), OracleProvider(),
              CloudflareProvider(), SupabaseProvider(), LocalProvider(),
              SangforProvider(), VMwareProvider(), SynologyProvider())}


def get(cloud):
    """按 cloud 取 Provider；未知返回 None。"""
    return PROVIDERS.get(cloud)
