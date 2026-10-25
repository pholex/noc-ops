"""`ops db status|pause|resume`：目标解析不猜、非 Supabase 拒绝、写操作先确认（非终端无 --yes 拒绝）、只读不记账、写操作记账。离线。"""
import json
import sys

import pytest

from noc_ops import db_cli
from noc_ops.providers.supabase import SupabaseProvider

pytestmark = pytest.mark.unit

ROWS = [
    {"name": "acme-app", "cloud": "supabase", "account": "sb", "ref": "abc", "region": "ap-northeast-1", "state": "available"},
    {"name": "acme-app-staging", "cloud": "supabase", "account": "sb", "ref": "xyz", "region": "ap-northeast-1", "state": "paused"},
    {"name": "prod-rds", "cloud": "aws", "account": "111122223333", "region": "cn-northwest-1", "state": "available"},
]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "data.json").write_text(json.dumps({"databases": ROWS}), encoding="utf-8")
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    monkeypatch.setattr(SupabaseProvider, "token_for", lambda self, label: "tok")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return tmp_path


def _fake_api(monkeypatch, status="ACTIVE_HEALTHY", ok=True):
    seen = []
    monkeypatch.setattr(SupabaseProvider, "project", lambda self, t, ref: {"ref": ref, "status": status})
    monkeypatch.setattr(SupabaseProvider, "pause", lambda self, t, ref: (seen.append(("pause", ref)), (ok, "" if ok else "boom"))[1])
    monkeypatch.setattr(SupabaseProvider, "restore", lambda self, t, ref: (seen.append(("restore", ref)), (ok, "" if ok else "boom"))[1])
    return seen


def _log(ws):
    p = ws / ".state" / "ops-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_resolve_levels_and_ambiguity():
    assert db_cli.resolve("abc", ROWS)[0]["name"] == "acme-app"
    assert db_cli.resolve("ACME-APP", ROWS)[0]["ref"] == "abc"
    row, cands = db_cli.resolve("acme", ROWS)
    assert row is None and len(cands) == 2                      # 同前缀两个，不猜
    assert db_cli.resolve("staging", ROWS)[0]["ref"] == "xyz"
    assert db_cli.resolve("nope", ROWS) == (None, [])


def test_status_is_read_only(ws, monkeypatch, capsys):
    _fake_api(monkeypatch)
    assert db_cli.main(["status", "acme-app"]) == 0
    assert "ACTIVE_HEALTHY" in capsys.readouterr().out
    assert _log(ws) == []


def test_non_supabase_refused(ws, monkeypatch, capsys):
    _fake_api(monkeypatch)
    assert db_cli.main(["pause", "prod-rds"]) == 1
    assert "只有 Supabase" in capsys.readouterr().err


def test_pause_without_yes_in_non_tty_is_refused_and_not_logged(ws, monkeypatch, capsys):
    seen = _fake_api(monkeypatch)
    assert db_cli.main(["pause", "acme-app"]) == 3
    err = capsys.readouterr().err
    assert "--yes" in err and seen == [] and _log(ws) == []


def test_dry_run_prints_plan_only(ws, monkeypatch, capsys):
    seen = _fake_api(monkeypatch)
    assert db_cli.main(["pause", "acme-app", "--dry-run"]) == 0
    assert "将执行：暂停" in capsys.readouterr().out and seen == [] and _log(ws) == []


def test_pause_with_yes_executes_and_logs(ws, monkeypatch, capsys):
    seen = _fake_api(monkeypatch)
    assert db_cli.main(["pause", "acme-app", "--yes", "--source", "claude-code"]) == 0
    assert seen == [("pause", "abc")]
    log = _log(ws)
    assert len(log) == 1 and log[0]["action"] == "db-pause" and log[0]["target"] == "acme-app"
    assert log[0]["source"] == "claude-code" and log[0]["cloud"] == "supabase" and log[0]["result"] == "ok"
    assert "ops db status abc" in capsys.readouterr().out


def test_pause_refuses_when_not_running(ws, monkeypatch, capsys):
    seen = _fake_api(monkeypatch, status="INACTIVE")
    assert db_cli.main(["pause", "acme-app", "--yes"]) == 1
    assert "不是运行中" in capsys.readouterr().err and seen == []


def test_resume_requires_inactive_and_logs_failure(ws, monkeypatch, capsys):
    seen = _fake_api(monkeypatch, status="INACTIVE", ok=False)
    assert db_cli.main(["resume", "xyz", "--yes", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "resume" and out["ok"] is False and out["error"] == "boom"
    assert seen == [("restore", "xyz")] and _log(ws)[0]["result"] == "fail"
    seen.clear()
    _fake_api(monkeypatch, status="ACTIVE_HEALTHY")
    assert db_cli.main(["resume", "xyz", "--yes"]) == 1       # 没暂停的不恢复


def test_ambiguous_target_lists_candidates(ws, monkeypatch, capsys):
    _fake_api(monkeypatch)
    assert db_cli.main(["status", "acme"]) == 2
    err = capsys.readouterr().err
    assert "匹配到 2 个" in err and "acme-app-staging" in err


def test_usage_errors():
    assert db_cli.main([]) == 2
    assert db_cli.main(["pause"]) == 2
    assert db_cli.main(["--bogus"]) == 2
    assert db_cli.main(["--help"]) == 0
