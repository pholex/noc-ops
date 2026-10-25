import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from noc_ops import update

pytestmark = pytest.mark.unit


def test_release_check_excludes_prerelease_yanked_and_empty(monkeypatch):
    data = {"releases": {"0.9.0": [{"yanked": False}], "0.10.0": [{"yanked": False}],
                         "1.0.0rc1": [{"yanked": False}], "2.0.0": [{"yanked": True}], "3.0.0": []}}
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(data).encode()))
    assert update.latest_version() == "0.10.0"


def test_update_check_never_installs(monkeypatch, capsys):
    monkeypatch.setattr(update, "latest_version", lambda: "99.0.0")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("check cannot install"))
    assert update.main(["--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["update_available"]


def test_uv_plan_keeps_receipt_and_never_reconstructs_plugins(tmp_path, monkeypatch):
    receipt = '[tool]\nrequirements=[{name="noc-ops",extras=["all"],directory="/example/source"},{name="private-extension",directory="/example/extension"}]\n'
    (tmp_path / "uv-receipt.toml").write_text(receipt)
    monkeypatch.setattr(update.shutil, "which", lambda name: "/bin/uv")
    plan = update.update_plan(tmp_path)
    assert plan["commands"] == [["/bin/uv", "tool", "upgrade", "noc-ops"], [str(tmp_path / "bin/ops"), "setup"]]
    assert plan["extensions"] == ["private-extension"]
    assert "自定义" in plan["source"]
    assert (tmp_path / "uv-receipt.toml").read_text() == receipt


def test_update_dry_run_is_offline(monkeypatch):
    monkeypatch.setattr(update, "update_plan", lambda: {"source": "test", "commands": [["uv", "tool", "upgrade", "noc-ops"]]})
    monkeypatch.setattr(update, "latest_version", lambda: pytest.fail("preview cannot use network"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("preview cannot install"))
    assert update.main(["--dry-run", "--json"]) == 0


@pytest.mark.parametrize("failed_step", [None, 0, 1])
def test_update_runs_new_setup_only_after_upgrade_success(monkeypatch, failed_step):
    commands = [["uv", "tool", "upgrade", "noc-ops"], ["/new/ops", "setup"]]
    monkeypatch.setattr(update, "update_plan", lambda: {"source": "test", "commands": commands})
    seen = []
    def run(cmd, **kwargs):
        index = len(seen)
        seen.append(cmd)
        return SimpleNamespace(returncode=1 if index == failed_step else 0)
    monkeypatch.setattr(subprocess, "run", run)
    assert update.main(["--yes"]) == (0 if failed_step is None else 1)
    assert seen == (commands[:1] if failed_step == 0 else commands)


def test_update_no_confirmation_no_install(monkeypatch):
    monkeypatch.setattr(update, "update_plan", lambda: {"source": "test", "commands": []})
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unconfirmed update"))
    assert update.main([]) == 2


def test_non_uv_install_is_not_silently_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/bin/uv")
    with pytest.raises(ValueError, match="uv tool"):
        update.update_plan(tmp_path)
