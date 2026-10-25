import json
import pytest
from noc_ops import resources, reboot_cli, db_cli, sync
from noc_ops.providers.supabase import SupabaseProvider

pytestmark = pytest.mark.unit

ROWS = [dict(cloud="aws", profile=account, region=region, id="i-duplicate", name="web", state="running")
        for account, region in (("one", "us-east-1"), ("two", "us-west-2"))]


@pytest.mark.parametrize("resolve", [resources.resolve, reboot_cli.resolve, lambda target, rows: sync.resolve_instance_row(rows, target)])
def test_duplicate_ids_never_select_first(resolve):
    row, candidates = resolve("i-duplicate", ROWS)
    assert row is None and candidates == ROWS


def test_ref_scopes_ip_alias_and_account_filter():
    assert resources.resolve("i-duplicate", ROWS, account="two")[0] == ROWS[1]
    assert resources.resolve(resources.reference(ROWS[1]), ROWS)[0] == ROWS[1]
    assert resources.resolve(resources.reference(ROWS[1]), ROWS, account="one")[0] is None
    odd = {**ROWS[0], "profile": "label/with space", "id": "id/a b", "ssh_alias": "alias", "private_ip": "192.0.2.1"}
    for target in (resources.reference(odd), "alias", "192.0.2.1"):
        assert resources.resolve(target, [odd])[0] == odd


def test_reboot_filtered_dry_run_is_json_and_never_executes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"instances": ROWS}))
    monkeypatch.setattr(reboot_cli, "_run", lambda *a: pytest.fail("dry run must not execute"))
    assert reboot_cli.main(["i-duplicate", "--account", "two", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["account"] == "two" and plan["region"] == "us-west-2"
    assert not plan["confirmation_required"] and plan["dry_run"]
    assert not (tmp_path / ".state/ops-log.jsonl").exists()


def test_db_preview_does_not_read_token_or_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    rows = [{"cloud": "supabase", "account": "one", "name": "db", "ref": "project-ref", "region": "test"}]
    (tmp_path / "data.json").write_text(json.dumps({"databases": rows}))
    monkeypatch.setattr(SupabaseProvider, "token_for", lambda *a: pytest.fail("preview cannot read token"))
    assert db_cli.main(["pause", "db", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["confirmation_required"] and plan["action"] == "pause"


def test_failed_live_db_status_blocks_write_even_with_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"databases": [{"cloud": "supabase", "account": "one", "name": "db", "ref": "ref"}]}))
    monkeypatch.setattr(SupabaseProvider, "token_for", lambda *a: "fake")
    monkeypatch.setattr(SupabaseProvider, "project", lambda *a: None)
    monkeypatch.setattr(SupabaseProvider, "pause", lambda *a: pytest.fail("unknown state must not pause"))
    assert db_cli.main(["pause", "db", "--yes"]) == 1


def test_mcp_uses_same_ambiguity_and_plan(monkeypatch):
    from noc_ops import mcp_server as m
    monkeypatch.setattr(m, "_load", lambda: {"instances": ROWS})
    monkeypatch.setattr(m, "_run", lambda *a: pytest.fail("must not execute"))
    assert "目标不唯一" in m.reboot_instance("i-duplicate")
    plan = json.loads(m.reboot_instance("i-duplicate", account="two", dry_run=True))
    assert plan["resource_ref"] == resources.reference(ROWS[1])
    assert plan["account"] == "two"


def test_find_exposes_capabilities_without_claiming_unsupported_operations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"instances": [{"id": "nas", "cloud": "synology", "name": "NAS"}]}))
    assert resources.main(["NAS", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["resources"][0]["capabilities"] == ["inventory"]


def test_mcp_find_and_cli_share_payload_and_collection_health(monkeypatch):
    from noc_ops import mcp_server as m
    data = {"instances": ROWS, "collection_status": [{"kind": "instances", "cloud": "aws", "account": "two", "status": "error"}]}
    monkeypatch.setattr(m, "_load", lambda: data)
    assert json.loads(m.find_resources("web", account="two")) == resources.query(data, "web", account="two")


def test_mcp_cloud_schedule_and_ssh_writes_require_confirmation(monkeypatch):
    from noc_ops import mcp_server as m
    monkeypatch.setattr(m, "_load", lambda: {"instances": ROWS[:1], "scheduled": [
        {"name": "nightly", "cloud": "aws", "account": "one", "region": "us-east-1"}]})
    monkeypatch.setattr(m.schedule_ops, "is_supported", lambda *a: True)
    monkeypatch.setattr(m.schedule_ops, "create_reboot", lambda *a, **kw: pytest.fail("unconfirmed create"))
    monkeypatch.setattr(m.schedule_ops, "list_all", lambda: pytest.fail("preview cannot query cloud"))
    monkeypatch.setattr(m.schedule_ops, "delete_row", lambda *a: pytest.fail("unconfirmed delete"))
    monkeypatch.setattr(m, "_ssh", lambda *a: pytest.fail("unconfirmed SSH write"))
    assert json.loads(m.schedule_task("web", "0 3 * * *"))["confirmation_required"]
    assert json.loads(m.cancel_scheduled_task("nightly"))["confirmation_required"]
    assert json.loads(m.run_ssh("test", "sudo rm /tmp/example"))["confirmation_required"]
    assert json.loads(m.run_ssh("test", "uptime", dry_run=True))["dry_run"]
