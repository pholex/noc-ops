import copy
import json
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from noc_ops import cloud_config, credentials, doctor, init_wizard, toml_io

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cloud", cloud_config.CLOUDS)
def test_all_clouds_roundtrip_and_merge_without_losing_unrelated_config(cloud):
    spec = cloud_config.CLOUDS[cloud]
    row = {k: "demo" for k in spec.required}
    if cloud == "aws":
        row["partition"] = "global"
    if spec.region:
        row["regions"] = {spec.region: ["Example", ""]}
    previous = {"instance_notes": {"i-demo": '备注\n带"引号"'}, "scheduler": {"timezone": "Asia/Tokyo"}}
    untouched = copy.deepcopy(previous)
    merged = init_wizard.merge_config(previous, {cloud: {"accounts": [row]}})
    parsed = tomllib.loads(toml_io.dumps(merged))
    assert previous == untouched
    assert parsed["instance_notes"] == untouched["instance_notes"]
    assert parsed["scheduler"] == untouched["scheduler"]
    assert cloud_config.accounts(parsed, cloud) == [row]
    assert cloud_config.validate_account(cloud, row) == []


def test_merge_existing_identity_retains_extra_fields_and_regions():
    previous = {"aliyun": {"profile": "demo", "account": "123", "custom": True,
                           "regions": {"old": ["Old", ""]}}, "gcp": {"accounts": [{"project": "keep"}]}}
    new = {"aliyun": {"accounts": [{"profile": "demo", "account": "123", "regions": {"new": ["New", ""]}}]}}
    merged = init_wizard.merge_config(previous, new)
    account = merged["aliyun"]["accounts"][0]
    assert account["custom"] and set(account["regions"]) == {"old", "new"}
    assert merged["gcp"] == previous["gcp"]


def test_init_azure_only_never_scans_aws_and_backs_up(tmp_path, monkeypatch):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    cfg = tmp_path / "ops-config.toml"
    original = '[instance_notes]\n"i-demo" = "keep"\n'
    cfg.write_text(original)
    monkeypatch.setattr(init_wizard, "_collect_aws", lambda: pytest.fail("Azure onboarding must not scan AWS"))
    monkeypatch.setattr(init_wizard, "collect_cloud", lambda c: [{"subscription": "demo-sub"}])
    monkeypatch.setattr(init_wizard, "_yn", lambda *a, **kw: True)
    assert init_wizard.main(["--cloud", "azure"]) == 0
    result = tomllib.loads(cfg.read_text())
    assert result["instance_notes"]["i-demo"] == "keep"
    assert result["azure"]["accounts"][0]["subscription"] == "demo-sub"
    assert list(tmp_path.glob("*.bak"))[0].read_text() == original


def test_init_preview_and_cancel_do_not_create_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "new"
    monkeypatch.setenv("NOC_HOME", str(workspace))
    monkeypatch.setattr(init_wizard, "collect_cloud", lambda c: [{"project": "demo"}])
    assert init_wizard.main(["--cloud", "gcp", "--dry-run"]) == 0
    monkeypatch.setattr(init_wizard, "_yn", lambda *a, **kw: False)
    assert init_wizard.main(["--cloud", "gcp"]) == 0
    assert not workspace.exists()


def test_init_refuses_invalid_existing_file_without_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    cfg = tmp_path / "ops-config.toml"
    cfg.write_text("[broken")
    assert init_wizard.main(["--cloud", "azure"]) == 2
    assert cfg.read_text() == "[broken" and not list(tmp_path.glob("*.bak"))


def test_single_identity_cloud_cannot_silently_add_second_account():
    with pytest.raises(ValueError):
        init_wizard.merge_config({"volcengine": {"account": "first"}},
                                 {"volcengine": {"accounts": [{"account": "second"}]}})


def test_doctor_azure_only_does_not_require_other_clis(tmp_path, monkeypatch):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    (tmp_path / "ops-config.toml").write_text('[[azure.accounts]]\nsubscription="demo"\n')
    monkeypatch.setattr(doctor.shutil, "which", lambda c: "/fake/az" if c == "az" else None)
    monkeypatch.setattr(doctor, "probe", lambda *a: pytest.fail("offline must not authenticate"))
    rows = doctor.diagnose(offline=True)
    assert not [r for r in rows if r["status"] == "error"]
    assert [r for r in rows if r.get("cloud") == "aws"][0]["status"] == "skipped"
    assert next(r for r in rows if r["name"] == "凭证与访问")["status"] == "skipped"


def test_doctor_checks_each_named_account_and_json_does_not_leak_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOC_HOME", str(tmp_path))
    (tmp_path / "ops-config.toml").write_text('[[aws.accounts]]\nprofile="one"\npartition="global"\n[[aws.accounts]]\nprofile="two"\npartition="global"\n')
    monkeypatch.setattr(doctor.shutil, "which", lambda c: "/fake/" + c)
    seen = []
    def fake(cloud, account):
        seen.append(account["profile"])
        return account["profile"] == "one", "身份检查"
    monkeypatch.setattr(doctor, "probe", fake)
    assert doctor.main(["--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert seen == ["one", "two"] and not result["ok"]
    assert [c for c in result["checks"] if c["name"] == "凭证与访问"][1]["status"] == "error"


def test_doctor_without_fix_is_read_only(tmp_path, monkeypatch):
    workspace = tmp_path / "missing"
    monkeypatch.setenv("NOC_HOME", str(workspace))
    doctor.diagnose(offline=True)
    assert not workspace.exists()


def test_business_error_and_wrong_identity_fail_probe(monkeypatch):
    def response(data):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(data)))
    response({"Response": {"Error": {"Code": "Denied"}}})
    assert not doctor.probe("tencent", {"account": "123"})[0]
    response({"AccountId": "456"})
    assert not doctor.probe("aliyun", {"profile": "named", "account": "123"})[0]
    response({"Account": "123"})
    assert doctor.probe("aws", {"profile": "named"})[0]
    assert "named" in doctor.credential_command("aws", {"profile": "named"})


def test_environment_token_never_passes_value_to_subprocess(monkeypatch):
    monkeypatch.setenv("DEMO_API_TOKEN", "sensitive-test-value")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("env token needs no command"))
    assert credentials.token({"token_env": "DEMO_API_TOKEN"}, "supabase") == "sensitive-test-value"
    assert credentials.token({"token_env": "invalid name"}, "supabase") == ""


def test_nonaws_init_removes_shipped_aws_and_scheduler_examples():
    from importlib import resources
    previous = tomllib.loads((resources.files("noc_ops") / "assets/ops-config.default.toml").read_text())
    merged = init_wizard.merge_config(previous, {"azure": {"accounts": [{"subscription": "demo"}]}})
    assert merged["aws"]["accounts"] == []
    assert merged["aws"]["route53_profiles"] == []
    assert merged["scheduler"]["accounts"] == []
    assert tomllib.loads(toml_io.dumps(merged)) == merged
