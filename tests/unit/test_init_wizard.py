"""Tier 1：ops init 向导——纯函数（profile 发现 / ARN 分区 / TOML 生成）。

不碰真云：caller_identity（跑 sts）不在此测；只测可离线断言的解析与生成，
并验证生成的 TOML 能被 deploy_config 原样读回（schema 对齐）。
"""
import pytest

from noc_ops import deploy_config, init_wizard as w

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache():
    deploy_config.reload()
    yield
    deploy_config.reload()


def test_partition_from_arn():
    assert w.partition_from_arn("arn:aws:iam::123456789012:user/x") == "global"
    assert w.partition_from_arn("arn:aws-cn:iam::123456789012:root") == "china"
    assert w.partition_from_arn("") == "global"


def test_aws_profiles_parsing(monkeypatch, tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[prod]\naws_access_key_id=AKIA\n", encoding="utf-8")
    (aws / "config").write_text("[default]\nregion=us-east-1\n[profile dev]\nregion=us-west-2\n",
                                encoding="utf-8")
    monkeypatch.setattr(w, "AWS_DIR", aws)
    # 去重保序：credentials 的 prod 在前，config 的 default / dev 续上
    assert w.aws_profiles() == ["prod", "default", "dev"]


def test_emit_toml_roundtrips_through_deploy_config(monkeypatch, tmp_path):
    text = w.emit_toml(
        ["111111111111"],
        ["us-east-1", "ap-northeast-1"], ["cn-northwest-1"],
        [{"profile": "111111111111", "partition": "global"},
         {"profile": "222222222222", "partition": "china"}],
        {"profile": "ali", "account": "160000", "ssh_user": "root", "ssh_key": "aliyun",
         "region": "cn-hangzhou", "display": "Hangzhou", "flag": "🇨🇳"},
        None)
    (tmp_path / "ops-config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    deploy_config.reload()
    assert deploy_config.aws_accounts() == [
        {"profile": "111111111111", "partition": "global"},
        {"profile": "222222222222", "partition": "china"}]
    assert deploy_config.scan_regions("global") == ["us-east-1", "ap-northeast-1"]
    assert deploy_config.scan_regions("china") == ["cn-northwest-1"]
    assert deploy_config.route53_profiles() == ["111111111111"]
    assert deploy_config.cloud("aliyun")["regions"]["cn-hangzhou"] == ("Hangzhou", "🇨🇳")


def test_emit_toml_quotes_safely():
    # 含引号/反斜杠的值不破坏 TOML
    out = w.emit_toml([], ["us-east-1"], [], [{"profile": 'a"b\\c', "partition": "global"}], None, None)
    import tomllib
    d = tomllib.loads(out)
    assert d["aws"]["accounts"][0]["profile"] == 'a"b\\c'
