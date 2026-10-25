"""Tier 1：无密钥对的 AWS 实例 = 仅 SSM 可达，sync 不把它当 SSH 机器。

背景（2026-10-22）：visassemble 实例群模板用 CFN Guard 强制 KeyName !exists、无公网、SG 零入站，
只能 SSM Session Manager。此前 sync 每轮对它们刷"找不到私钥 KeyName=None"+ 去撞 SSH 探测，
全是误报。不变量：
- connection_of → ("ssm", None, None)；
- ~/.ssh/config 托管段不出现这些实例（也不留空的分组头）；
- 有密钥对的实例行为不变。
"""
import pytest

from noc_ops import sync

pytestmark = pytest.mark.unit

SCOPE = {"profile": "123456789012", "region": "us-east-1", "region_display": "Virginia",
         "flag": "🇺🇸", "prefix": "virginia", "jump_alias": None, "jump_instance": None}


def _inst(name, key_name, public_ip=None):
    return {"id": f"i-{name}", "name": name, "type": "t4g.small", "state": "running",
            "az": "us-east-1a", "private_ip": "10.0.0.5", "public_ip": public_ip, "key_name": key_name}


def _record(inst):
    mode, host, pj = sync.connection_of(SCOPE, inst)
    return {"scope": SCOPE, "inst": inst, "alias": sync.make_alias(SCOPE, inst), "keyfile": None,
            "mode": mode, "hostname": host, "proxyjump": pj, "user": None, "verified": False}


def test_no_keypair_means_ssm():
    assert sync.connection_of(SCOPE, _inst("render", None)) == ("ssm", None, None)
    assert sync.connection_of(SCOPE, _inst("render", "")) == ("ssm", None, None)


def test_keypair_keeps_ssh_semantics():
    assert sync.connection_of(SCOPE, _inst("web", "mykey", "1.2.3.4")) == ("direct", "1.2.3.4", None)
    assert sync.connection_of(SCOPE, _inst("db", "mykey")) == ("direct", "10.0.0.5", None)


def test_ssh_config_omits_ssm_instances_and_empty_groups():
    ssm_only = [_record(_inst("render-fleet", None))]
    assert sync._render_ssh_config(ssm_only, []).strip() == ""

    mixed = [_record(_inst("render-fleet", None)), _record(_inst("web", "mykey", "1.2.3.4"))]
    body = sync._render_ssh_config(mixed, [])
    assert "Host virginia-web" in body
    assert "render-fleet" not in body
    assert "HostName None" not in body
