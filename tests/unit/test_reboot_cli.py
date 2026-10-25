"""Tier 1 单元测试：`ops reboot`（目标解析 / 通道选择 / 只读模式 / 审计记账）。

覆盖目标歧义拒绝，以及只读模式不执行命令、不写审计日志的约束。
"""
import json

import pytest

from noc_ops import reboot_cli
from noc_ops.providers import PROVIDERS

pytestmark = pytest.mark.unit

INSTS = [
    {"id": "sample-app-001", "name": "示例应用服务器", "cloud": "sangfor", "profile": "Sangfor HCI",
     "_acct": "sangfor", "region": "本地网络", "region_display": "本地网络", "state": "running",
     "ssh_alias": "local-acme-dnsoa"},
    {"id": "sample-db-001", "name": "示例数据库服务器-sql", "cloud": "sangfor", "profile": "Sangfor HCI",
     "_acct": "sangfor", "region": "本地网络", "region_display": "本地网络", "state": "running"},
    {"id": "i-123", "name": "sample-web", "cloud": "aws", "profile": "111122223333",
     "region": "cn-northwest-1", "region_display": "Ningxia", "state": "running"},
    {"id": "nas-1", "name": "sample-resource-02", "cloud": "synology", "profile": "Synology NAS",
     "region": "本地网络", "region_display": "本地网络", "state": "running", "ssh_alias": "sample-resource-07"},
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """独立工作区：data.json 喂假清单，审计日志也落这里（别碰仓库 .state）。

    深信服账号也在这里打桩——argv 拼装要从配置取 SCP 地址/用户名，
    tmp 工作区没有 ops-config.toml，不打桩会一律退化成"找不到账号"。
    """
    (tmp_path / "data.json").write_text(json.dumps({"instances": INSTS}), encoding="utf-8")
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    monkeypatch.setattr(PROVIDERS["sangfor"], "_accounts", lambda *a, **k: [
        {"account": "sangfor", "url": "https://scp.test:4430", "username": "admin",
         "keychain_service": "noc-sangfor-test"}])
    return tmp_path


def _log_lines(ws):
    p = ws / ".state" / "ops-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ── 目标解析 ──────────────────────────────────────────────────────────────
def test_resolve_by_id_and_exact_name():
    assert reboot_cli.resolve("sample-app-001", INSTS)[0]["name"] == "示例应用服务器"
    assert reboot_cli.resolve("sample-web", INSTS)[0]["id"] == "i-123"


def test_resolve_case_insensitive_and_substring():
    assert reboot_cli.resolve("SAMPLE-WEB", INSTS)[0]["id"] == "i-123"
    assert reboot_cli.resolve("web", INSTS)[0]["id"] == "i-123"  # 唯一子串才算数


def test_resolve_ambiguous_returns_candidates_not_a_guess():
    inst, cands = reboot_cli.resolve("示例", INSTS)
    assert inst is None and len(cands) == 2                            # 两台同前缀 → 停下来问


def test_resolve_miss():
    assert reboot_cli.resolve("不存在的机器", INSTS) == (None, [])


def test_ambiguous_target_exits_2_without_running(workspace, monkeypatch, capsys):
    monkeypatch.setattr(reboot_cli, "_run", lambda *a, **k: pytest.fail("不该执行任何命令"))
    assert reboot_cli.main(["示例"]) == 2
    err = capsys.readouterr().err
    assert "匹配到 2 台" in err and "sample-app-001" in err
    assert _log_lines(workspace) == []


# ── 通道选择 ──────────────────────────────────────────────────────────────
def test_sangfor_goes_through_scp_api_not_ssh(workspace, capsys):
    """深信服重启必须走 SCP 管控 API（provider 的内联 python 通道），不是 ssh。"""
    assert reboot_cli.main(["sample-app-001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "noc_ops.providers.sangfor" in out and "_reboot_server" in out
    assert "ssh" not in out.lower()
    assert _log_lines(workspace) == []                                  # dry-run 不记账


def test_aws_dry_run_shows_cli(workspace, capsys):
    assert reboot_cli.main(["i-123", "--dry-run"]) == 0
    assert "aws ec2 reboot-instances" in capsys.readouterr().out


def test_no_control_api_tells_you_what_to_do(workspace, capsys):
    """群晖没接管控 API：如实说走 DSM/SSH，并把别名给出来，别抛 NotImplementedError。"""
    assert reboot_cli.main(["nas-1"]) == 1
    err = capsys.readouterr().err
    assert "DSM" in err and "sample-resource-07" in err
    assert _log_lines(workspace) == []


# ── 执行与记账 ────────────────────────────────────────────────────────────
def test_reboot_runs_and_records_audit(workspace, monkeypatch, capsys):
    seen = {}

    def fake_run(argv, timeout):
        seen["argv"] = argv
        return True, "reboot accepted"

    monkeypatch.setattr(reboot_cli, "_run", fake_run)
    assert reboot_cli.main(["sample-app-001", "--source", "claude-code"]) == 0
    assert "_reboot_server" in " ".join(seen["argv"])
    logs = _log_lines(workspace)
    assert len(logs) == 1
    assert logs[0]["action"] == "reboot" and logs[0]["result"] == "ok"
    assert logs[0]["source"] == "claude-code" and logs[0]["detail"] == "sample-app-001"
    assert "40 秒" in capsys.readouterr().out                            # 提示复查，别立刻 SSH


def test_failed_reboot_records_fail_and_exits_1(workspace, monkeypatch):
    monkeypatch.setattr(reboot_cli, "_run", lambda argv, timeout: (False, "401 Unauthorized"))
    assert reboot_cli.main(["sample-app-001"]) == 1
    assert _log_lines(workspace)[0]["result"] == "fail"


def test_status_is_read_only(workspace, monkeypatch, capsys):
    monkeypatch.setattr(reboot_cli, "_run", lambda argv, timeout: (True, '{"state":"running"}'))
    assert reboot_cli.main(["sample-app-001", "--status"]) == 0
    assert _log_lines(workspace) == []                                   # 只读不进审计线
    assert "running" in capsys.readouterr().out


def test_json_output(workspace, monkeypatch, capsys):
    monkeypatch.setattr(reboot_cli, "_run", lambda argv, timeout: (True, "ok"))
    assert reboot_cli.main(["i-123", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["action"] == "reboot" and payload["id"] == "i-123"


def test_empty_inventory_points_at_sync(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    assert reboot_cli.main(["whatever"]) == 1
    assert "ops sync" in capsys.readouterr().err


def test_usage_on_no_target(workspace, capsys):
    assert reboot_cli.main([]) == 2
    assert "ops reboot" in capsys.readouterr().err
