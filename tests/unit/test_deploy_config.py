"""Tier 1：部署配置 loader——默认配置必须=当前硬编码值（行为不变）；NOC_HOME 切换可改（证明可配）。"""
import pytest

from noc_ops import deploy_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache():
    deploy_config.reload()
    yield
    deploy_config.reload()


def test_aws_accounts_default():
    profiles = {a["profile"]: a["partition"] for a in deploy_config.aws_accounts()}
    assert profiles == {"111122223333": "china", "123456789012": "global", "210987654321": "global"}


def test_scan_regions():
    assert deploy_config.scan_regions("china") == ["cn-northwest-1"]
    assert "ap-northeast-1" in deploy_config.scan_regions("global")
    assert len(deploy_config.scan_regions("global")) == 11


def test_route53():
    assert deploy_config.route53_profiles() == ["123456789012", "210987654321"]


def test_alias_prefix_and_jumps():
    assert deploy_config.aws_alias_prefix()[("123456789012", "ap-northeast-1")] == "tk"
    assert deploy_config.aws_alias_prefix()[("111122223333", "cn-northwest-1")] == "nx"
    assert deploy_config.aws_jumps()[("111122223333", "cn-northwest-1")] == ("sample-resource-08", "sample-resource-04")


def test_clouds():
    al = deploy_config.cloud("aliyun")
    assert al["profile"] == "acme" and al["account"] == "1234567890123456"
    assert al["regions"]["cn-hangzhou"] == ("Hangzhou", "🇨🇳")
    tc = deploy_config.cloud("tencent")
    assert tc["account"] == "100000000001" and tc["ssh_user"] == "ubuntu" and tc["ssh_key"] == "tencent"


def test_scheduler_default():
    sa = deploy_config.sched_accounts()
    assert sa["111122223333"]["role"] == "arn:aws-cn:iam::111122223333:role/noc-scheduler"   # 中国区 ARN 分区
    assert sa["123456789012"]["role"] == "arn:aws:iam::123456789012:role/noc-scheduler"
    assert sa["210987654321"]["regions"] == ["ap-northeast-1"]
    d = deploy_config.dispatch()
    assert d["lambda"] == "arn:aws:lambda:ap-northeast-1:210987654321:function:noc-reboot-dispatcher"
    assert d["role"] == "arn:aws:iam::210987654321:role/noc-scheduler"
    assert deploy_config.dispatch_clouds() == {("tencent", "100000000001"), ("aliyun", "1234567890123456")}
    assert deploy_config.timezone() == "Asia/Shanghai"
    assert deploy_config.nonaws_region_display()["ap-nanjing"] == ("Nanjing", "🇨🇳")


def test_override_via_noc_home(monkeypatch, tmp_path):
    (tmp_path / "ops-config.toml").write_text(
        '[aws]\nscan_regions_global = ["us-east-1", "eu-west-9"]\nscan_regions_china = []\n'
        'route53_profiles = []\n'
        '[[aws.accounts]]\nprofile = "999000111222"\npartition = "global"\n',
        encoding="utf-8")
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    deploy_config.reload()
    assert deploy_config.scan_regions("global") == ["us-east-1", "eu-west-9"]
    assert deploy_config.aws_accounts() == [{"profile": "999000111222", "partition": "global"}]
