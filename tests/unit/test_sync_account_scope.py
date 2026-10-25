"""Tier 1 单元测试：`ops sync --account=<账号>`（可与 --kind 组合）的账号收窄与合并写回。

核心不变量：
- 收窄只在 deploy_config 一处生效 → AWS 走 aws_accounts()/route53_profiles()、其余云走
  cloud_accounts()，都只剩该账号（全链路自动跟随）；
- 写回是 **patch 合并**：只替换该账号的行，同云其它账号 / 其它云的行原样沿用（不被单账号结果覆盖）；
- instances 的 SSH config 是整段重写的 → 沿用的其它账号实例必须一起渲染进去，不能被抹掉；
- 刷非 AWS 账号时**一次 AWS API 都不打**（AWS 行全部从 data.json 还原）；
- 该云没有的类型（阿里云的 iam/billing…）、无账号维度的类型（scheduled/runbooks/ops_log）
  与未知账号 → 拒绝并给出可用值，不静默乱刷。
"""
import json

import pytest

from noc_ops import deploy_config, sync
from noc_ops.providers import PROVIDERS

pytestmark = pytest.mark.unit

ACCT = "123456789012"       # 被单刷的 AWS 账号
OTHER = "111122223333"      # 另一个 AWS 账号：必须原样沿用
ALI = "1234567890123456"    # 阿里云 acme（ops-config.toml 里的账号 ID）
ALI_OTHER = "6543210987654321"   # 阿里云 acme-dns：同云另一账号，必须原样沿用


@pytest.fixture(autouse=True)
def _reset_scope():
    deploy_config.reload()
    yield
    deploy_config.reload()


def _prev_data():
    return {
        "instances": [
            {"cloud": "aws", "profile": ACCT, "id": "i-old", "name": "旧机器", "region": "ap-northeast-1",
             "region_display": "Tokyo", "flag": "🇯🇵", "type": "t3.micro", "state": "running",
             "az": "ap-northeast-1a", "private_ip": "10.0.0.9", "public_ip": "1.1.1.1",
             "key_name": "k", "ssh_alias": "tk-old", "ssh_user": "ec2-user",
             "key_file": "~/.ssh/k.pem", "connect": "direct", "proxyjump": None, "verified": True},
            {"cloud": "aws", "profile": OTHER, "id": "i-cn", "name": "宁夏机器", "region": "cn-northwest-1",
             "region_display": "Ningxia", "flag": "🇨🇳", "type": "t3.small", "state": "running",
             "az": "cn-northwest-1a", "private_ip": "10.1.0.5", "public_ip": None,
             "key_name": "kcn", "ssh_alias": "nx-app", "ssh_user": "ubuntu",
             "key_file": "~/.ssh/kcn.pem", "connect": "jump", "proxyjump": "sample-resource-08", "verified": True},
            {"cloud": "aliyun", "profile": ALI, "id": "ali-1", "name": "阿里机器",
             "region": "cn-hangzhou", "state": "running", "private_ip": "10.2.0.1"},
            {"cloud": "sangfor", "profile": "sangfor", "id": "vm-1", "name": "内网VM",
             "region": "idc", "state": "running", "private_ip": "192.0.2.3"},
        ],
        "storage": [
            {"cloud": "aws", "account": ACCT, "name": "bucket-old", "region": "us-east-1"},
            {"cloud": "aws", "account": OTHER, "name": "bucket-cn", "region": "cn-northwest-1"},
            {"cloud": "aliyun", "account": ALI, "name": "oss-bucket", "region": "cn-hangzhou"},
        ],
        "domains": [
            {"domain": "a.com", "registrar": "AWS Route53", "account": ACCT, "days_left": 100},
            {"domain": "b.cn", "registrar": "阿里云", "account": ALI, "days_left": 50},
            {"domain": "c.cn", "registrar": "阿里云", "account": ALI_OTHER, "days_left": 70},
        ],
        "totals": {"instances": 4, "storage": 3, "domains": 3},
    }


def _run(monkeypatch, tmp_path, name=ACCT, kind=None, fresh_rows=None,
         fresh_scopes=None, fresh_cloud_insts=None, cli_available=True):
    """把 data.json 指到 tmp，桩掉云调用，跑 refresh_account，返回 (退出码, 回写的 data, 侧录)。

    fresh_scopes=None 时 build_scopes 直接爆炸——刷非 AWS 账号的用例靠它证明 AWS 一次都没扫。
    """
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(_prev_data(), ensure_ascii=False))
    seen = {}

    def _no_aws():
        raise AssertionError("本轮不该扫 AWS")

    monkeypatch.setattr(sync.shutil, "which", lambda cmd: f"/mock/bin/{cmd}" if cli_available else None)
    monkeypatch.setattr(sync, "NOC_DATA", data_file)
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync, "build_scopes", (lambda: fresh_scopes) if fresh_scopes is not None else _no_aws)
    monkeypatch.setattr(sync, "_load_json", lambda p, d: json.loads(p.read_text()) if p == data_file else d)
    monkeypatch.setattr(sync, "resolve_keyfile", lambda p, k: f"/keys/{k}.pem")
    monkeypatch.setattr(sync, "write_ssh_config", lambda body: seen.setdefault("ssh", body))
    monkeypatch.setattr(sync, "write_local_ssh_config", lambda rows: None)
    def _instances(network=None):
        seen["provider_calls"] = seen.get("provider_calls", 0) + 1
        return list(fresh_cloud_insts or [])

    monkeypatch.setattr(PROVIDERS["aliyun"], "list_instances", _instances)

    def _collect(k, clouds=None):
        seen["clouds"] = clouds
        return list(fresh_rows or [])

    monkeypatch.setattr(sync, "_collect_kind", _collect)
    code = sync.refresh_account(name, kind)
    return code, json.loads(data_file.read_text()), seen


# ---- 收窄本身（deploy_config 一处生效）----

def test_scope_narrows_aws_accounts_and_route53():
    deploy_config.set_account_scope("aws", ACCT)
    assert [a["profile"] for a in deploy_config.aws_accounts()] == [ACCT]
    assert deploy_config.route53_profiles() == [ACCT]
    assert len(deploy_config.all_aws_accounts()) == 3      # 全量视图不受收窄影响（校验参数用）
    deploy_config.set_account_scope("aws", None)
    assert len(deploy_config.aws_accounts()) == 3


def test_scope_narrows_cloud_accounts_by_profile_or_id():
    for key in ("acme", ALI):                            # profile 名与账号 ID 都认
        deploy_config.set_account_scope("aliyun", key)
        assert [a["account"] for a in deploy_config.cloud_accounts("aliyun")] == [ALI]
        assert len(deploy_config.all_cloud_accounts("aliyun")) == 2   # 全量视图不受影响
    deploy_config.set_account_scope("aliyun", None)
    assert len(deploy_config.cloud_accounts("aliyun")) == 2
    deploy_config.set_account_scope("aliyun", ALI)
    assert len(deploy_config.aws_accounts()) == 3          # 收窄只作用于被点名的那朵云


def test_resolve_account_maps_name_to_cloud_and_row_id():
    assert sync.resolve_account(ACCT) == ("aws", ACCT)
    assert sync.resolve_account("acme") == ("aliyun", ALI)    # profile 名 → 行里的账号 ID
    assert sync.resolve_account(ALI) == ("aliyun", ALI)
    assert sync.resolve_account("acme-dns") == ("aliyun", ALI_OTHER)
    assert sync.resolve_account("nope") == (None, None)


# ---- AWS 账号单刷 ----

def test_kind_refresh_keeps_other_accounts_and_clouds(monkeypatch, tmp_path):
    fresh = [{"cloud": "aws", "account": ACCT, "name": "bucket-new", "region": "us-west-2"}]
    code, data, seen = _run(monkeypatch, tmp_path, kind="storage", fresh_rows=fresh, fresh_scopes=[])
    assert code == 0
    names = {s["name"] for s in data["storage"]}
    assert names == {"bucket-new", "bucket-cn", "oss-bucket"}   # 本账号旧桶被换掉，其它账号/云沿用
    assert seen["clouds"] == {"aws"}                            # 只问 AWS provider
    assert data["totals"]["storage"] == 3
    assert data["domains"] == _prev_data()["domains"]           # 没点名的类型一个字都不动


def test_instances_refresh_keeps_other_accounts_in_ssh_config(monkeypatch, tmp_path):
    scope = {"profile": ACCT, "region": "ap-northeast-1", "prefix": "tk", "jump_alias": None,
             "jump_instance": None, "region_display": "Tokyo", "flag": "🇯🇵", "nick": "tokyo"}
    inst = {"id": "i-new", "name": "new", "type": "t3.micro", "private_ip": "10.0.0.10",
            "public_ip": "2.2.2.2", "az": "ap-northeast-1a", "vpc": "vpc-1", "key_name": "k",
            "state": "running"}
    code, data, seen = _run(monkeypatch, tmp_path, kind="instances", fresh_scopes=[(scope, [inst])])
    assert code == 0
    ids = {i["id"] for i in data["instances"]}
    assert ids == {"i-new", "i-cn", "ali-1", "vm-1"}   # 本账号换新；另一 AWS 账号 + 阿里 + 内网机沿用
    # SSH config 整段重写：沿用账号的 Host 必须还在，且跳板/私钥/用户名不丢
    assert "Host nx-app" in seen["ssh"] and "ProxyJump sample-resource-08" in seen["ssh"]
    assert "Host tk-new" in seen["ssh"] and "Host tk-old" not in seen["ssh"]
    assert data["totals"]["accounts"] == 4


# ---- 阿里云账号单刷 ----

def test_aliyun_kind_refresh_keeps_other_aliyun_account(monkeypatch, tmp_path):
    fresh = [{"domain": "new.cn", "registrar": "阿里云", "account": ALI, "days_left": 300}]
    code, data, seen = _run(monkeypatch, tmp_path, name="acme", kind="domains", fresh_rows=fresh)
    assert code == 0
    doms = {d["domain"] for d in data["domains"]}
    assert doms == {"new.cn", "a.com", "c.cn"}   # 本账号换新；Route53 与 acme-dns 的域名沿用
    assert seen["clouds"] == {"aliyun"}          # 只问阿里云 provider，不白扫 AWS/腾讯/CF
    assert data["totals"]["domains"] == 3
    assert data["storage"] == _prev_data()["storage"]


def test_aliyun_instances_refresh_reuses_all_aws_rows(monkeypatch, tmp_path):
    """刷阿里云实例：build_scopes 一次都不许调（会 raise），AWS 行全部从 data.json 还原进 SSH config。"""
    fresh = [{"cloud": "aliyun", "profile": ALI, "id": "ali-2", "name": "新阿里机器",
              "region": "cn-hangzhou", "region_display": "Hangzhou", "flag": "🇨🇳", "type": "ecs.t6",
              "state": "running", "az": "cn-hangzhou-a", "private_ip": "10.2.0.2", "public_ip": None,
              "key_name": None, "ssh_alias": "ali-new", "ssh_user": "root", "key_file": "",
              "connect": "direct", "proxyjump": None, "verified": False}]
    code, data, seen = _run(monkeypatch, tmp_path, name=ALI, kind="instances", fresh_cloud_insts=fresh)
    assert code == 0
    ids = {i["id"] for i in data["instances"]}
    assert ids == {"i-old", "i-cn", "ali-2", "vm-1"}   # 阿里换新；两个 AWS 账号 + 内网机沿用
    assert "Host tk-old" in seen["ssh"] and "Host nx-app" in seen["ssh"]


# ---- 拒绝路径 ----

def test_rejects_unknown_account_and_non_scoped_kind(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    assert sync.refresh_account("999999999999") == 2
    err = capsys.readouterr().err
    assert "未知账号" in err and "acme" in err        # 提示里要列出各云可用账号
    assert sync.refresh_account(ACCT, "runbooks") == 2
    assert "没有AWS账号维度" in capsys.readouterr().err
    assert sync.refresh_account("acme", "messaging") == 2   # 阿里云 provider 不产出消息/事件
    assert "没有阿里云账号维度" in capsys.readouterr().err
    assert deploy_config.account_scope("aws") is None   # 拒绝后不留收窄状态
    assert deploy_config.account_scope("aliyun") is None


def test_aliyun_missing_cli_preserves_cached_rows(monkeypatch, tmp_path):
    code, data, seen = _run(monkeypatch, tmp_path, name=ALI, kind="instances",
                            fresh_cloud_insts=[{"id": "unexpected"}], cli_available=False)
    assert code == 0
    assert {row["id"] for row in data["instances"]} == {"i-old", "i-cn", "ali-1", "vm-1"}
    assert "provider_calls" not in seen
    status = next(row for row in data["collection_status"] if row["cloud"] == "aliyun")
    assert status["status"] == "error" and status["preserved"] is True
    assert status["errors"] == ["cli_missing"]
