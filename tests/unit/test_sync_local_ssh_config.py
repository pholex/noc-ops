"""write_local_ssh_config 只收本地网络域机器，公有云实例（network 为空）不得混入本地托管段。"""
import pytest

from noc_ops import sync

pytestmark = pytest.mark.unit


def _row(alias, cloud, network, private_ip="10.0.0.1", user="noc"):
    return {"ssh_alias": alias, "cloud": cloud, "network": network,
            "private_ip": private_ip, "ssh_user": user}


def test_local_block_excludes_public_cloud_rows(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(sync, "SSH_CONFIG", cfg)
    sync.write_local_ssh_config([
        _row("local-box", "local", "lan"),
        _row("ts-nas", "synology", "tailscale"),
        _row("gcp-noc-free-micro", "gcp", None),      # 配了 ssh_user 的云实例：不收
        _row("sample-resource-03", "aliyun", None),
    ])
    text = cfg.read_text()
    assert "Host local-box" in text and "Host ts-nas" in text
    assert "gcp-noc-free-micro" not in text and "sample-resource-03" not in text


def test_local_block_not_written_when_only_cloud_rows(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(sync, "SSH_CONFIG", cfg)
    sync.write_local_ssh_config([_row("gcp-x", "gcp", None)])
    assert not cfg.exists()
