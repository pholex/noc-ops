"""Tier 1 单元测试：`ops sync --probe=<实例名|ID>`——只探一台 AWS 实例的登录用户名。

核心不变量：
- 零云 API：目标从 data.json 定位，不调 build_scopes / provider；
- 探测成功 → 写 ssh-users.json 缓存、改 data.json 该行的 ssh_user/verified、整段重渲染 SSH config
  （其它账号/其它云的 Host 不能被抹掉）；
- 探测失败 → 什么都不写，退出码 1；
- 非 AWS / SSM-only / 未开机 / 找不到 / 不唯一 → 拒绝并说清楚（退出码 2），绝不猜。
"""
import json

import pytest

from noc_ops import sync

pytestmark = pytest.mark.unit

ACCT, OTHER = "123456789012", "111122223333"


def _rows():
    return [
        {"cloud": "aws", "profile": ACCT, "id": "i-tk", "name": "东京机器", "region": "ap-northeast-1",
         "region_display": "Tokyo", "flag": "🇯🇵", "type": "t3.micro", "state": "running",
         "az": "ap-northeast-1a", "private_ip": "10.0.0.9", "public_ip": "1.1.1.1",
         "key_name": "k", "ssh_alias": "tk-old", "ssh_user": "ec2-user",
         "key_file": "~/.ssh/k.pem", "connect": "direct", "proxyjump": None, "verified": True},
        {"cloud": "aws", "profile": OTHER, "id": "i-nx", "name": "sample-resource-11", "region": "cn-northwest-1",
         "region_display": "Ningxia", "flag": "🇨🇳", "type": "m6i.4xlarge", "state": "running",
         "az": "cn-northwest-1a", "private_ip": "10.254.0.1", "public_ip": "43.1.1.1",
         "key_name": "acme-dns-oa", "ssh_alias": "sample-resource-09", "ssh_user": None,
         "key_file": "~/.ssh/acme-dns-oa.pem", "connect": "direct", "proxyjump": None, "verified": False},
        {"cloud": "aws", "profile": OTHER, "id": "i-ssm", "name": "ssm-only", "region": "cn-northwest-1",
         "region_display": "Ningxia", "flag": "🇨🇳", "type": "t3.small", "state": "running",
         "az": "cn-northwest-1a", "private_ip": "172.16.0.9", "public_ip": None,
         "key_name": None, "ssh_alias": None, "ssh_user": None,
         "key_file": "", "connect": "ssm", "proxyjump": None, "verified": False},
        {"cloud": "aws", "profile": OTHER, "id": "i-off", "name": "sample-resource-12", "region": "cn-northwest-1",
         "region_display": "Ningxia", "flag": "🇨🇳", "type": "t3.small", "state": "stopped",
         "az": "cn-northwest-1a", "private_ip": "172.16.0.10", "public_ip": None,
         "key_name": "acme-dns-oa", "ssh_alias": "sample-resource-10", "ssh_user": None,
         "key_file": "~/.ssh/acme-dns-oa.pem", "connect": "direct", "proxyjump": None, "verified": False},
        {"cloud": "aliyun", "profile": "acme", "id": "ali-1", "name": "阿里机器", "region": "cn-hangzhou",
         "state": "running", "private_ip": "10.2.0.1", "public_ip": "2.2.2.2",
         "ssh_alias": "ali-1", "ssh_user": "root", "key_file": "~/.ssh/ali.pem"},
    ]


def _run(monkeypatch, tmp_path, target, probe_result="centos"):
    data_file, cache_file = tmp_path / "data.json", tmp_path / "ssh-users.json"
    data_file.write_text(json.dumps({"instances": _rows()}))
    seen = {}
    monkeypatch.setattr(sync, "NOC_DATA", data_file)
    monkeypatch.setattr(sync, "USER_CACHE_FILE", cache_file)
    monkeypatch.setattr(sync.paths, "ensure_workspace", lambda: None)
    monkeypatch.setattr(sync, "build_scopes", lambda: pytest.fail("单机探测不该扫云"))
    monkeypatch.setattr(sync, "probe_user", lambda alias: seen.setdefault("probed", []).append(alias) or probe_result)
    monkeypatch.setattr(sync, "write_ssh_config", lambda body: seen.setdefault("ssh", body))
    monkeypatch.setattr(sync, "write_local_ssh_config", lambda rows: None)
    code = sync.refresh_probe(target)
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else None
    return code, json.loads(data_file.read_text()), cache, seen


def test_probe_by_name_writes_cache_data_and_whole_ssh_config(monkeypatch, tmp_path):
    code, data, cache, seen = _run(monkeypatch, tmp_path, "sample-resource-11")
    assert code == 0
    assert seen["probed"] == ["sample-resource-09"]            # 只探这一台
    assert cache == {"i-nx": {"user": "centos", "verified": True}}
    row = next(i for i in data["instances"] if i["id"] == "i-nx")
    assert (row["ssh_user"], row["verified"]) == ("centos", True)
    assert "Host sample-resource-09\n" in seen["ssh"] and "User centos" in seen["ssh"]
    assert "Host tk-old" in seen["ssh"] and "Host ali-1" in seen["ssh"]   # 其它账号/其它云没被抹掉
    others = [i for i in data["instances"] if i["id"] != "i-nx"]
    assert others == [i for i in _rows() if i["id"] != "i-nx"]            # 其它行原样


def test_probe_by_id(monkeypatch, tmp_path):
    code, data, cache, seen = _run(monkeypatch, tmp_path, "i-nx", probe_result="ubuntu")
    assert code == 0 and cache["i-nx"]["user"] == "ubuntu"


def test_probe_failure_writes_nothing(monkeypatch, tmp_path):
    code, data, cache, seen = _run(monkeypatch, tmp_path, "sample-resource-11", probe_result=None)
    assert code == 1 and cache is None and "ssh" not in seen
    assert next(i for i in data["instances"] if i["id"] == "i-nx")["ssh_user"] is None


@pytest.mark.parametrize("target", ["阿里机器", "ssm-only", "sample-resource-12", "不存在的机器"])
def test_refuses_non_aws_ssm_stopped_or_unknown(monkeypatch, tmp_path, target):
    code, data, cache, seen = _run(monkeypatch, tmp_path, target)
    assert code == 2 and "probed" not in seen and cache is None


def test_ambiguous_substring_lists_candidates_and_refuses(monkeypatch, tmp_path, capsys):
    code, data, cache, seen = _run(monkeypatch, tmp_path, "sample-resource")     # 子串命中两台
    assert code == 2 and "probed" not in seen
    err = capsys.readouterr().err
    assert "i-nx" in err and "i-off" in err


def test_resolve_prefers_exact_id_then_exact_name():
    rows = _rows()
    assert sync.resolve_instance_row(rows, "i-nx")[0]["id"] == "i-nx"
    assert sync.resolve_instance_row(rows, "sample-resource-11")[0]["id"] == "i-nx"   # 精确名优先于子串
    row, cands = sync.resolve_instance_row(rows, "sample-resource")
    assert row is None and {c["id"] for c in cands} == {"i-nx", "i-off"}
