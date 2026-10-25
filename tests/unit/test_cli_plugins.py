"""`ops <插件子命令>`：经 entry point 组 `noc_ops.commands` 挂入的外部包子命令。
团队专属命令（IM 机器人 / 用量限额 / 流量报表…）剥到私有包后靠这个口回到 `ops` 下。离线。"""
import sys
import types

import pytest

from noc_ops import cli, deploy_config

pytestmark = pytest.mark.unit


class _EP:
    """最小 EntryPoint 替身：name / value / dist / load()。"""
    def __init__(self, name, fn, dist_name="noc-ops-internal", fail=False):
        self.name, self.value, self._fn, self._fail = name, f"x.y:{name}", fn, fail
        self.dist = types.SimpleNamespace(metadata={"Name": dist_name})

    def load(self):
        if self._fail:
            raise ImportError("boom")
        return self._fn


def _install(monkeypatch, *eps):
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: list(eps))


def test_plugin_command_dispatch_and_exit_code(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 7

    _install(monkeypatch, _EP("hello", fake_main))
    monkeypatch.setattr(sys, "argv", ["ops", "hello", "--flag", "x"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 7 and seen["argv"] == ["--flag", "x"]


def test_plugin_returning_none_exits_zero(monkeypatch):
    _install(monkeypatch, _EP("quiet", lambda argv: None))
    monkeypatch.setattr(sys, "argv", ["ops", "quiet"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0


def test_plugin_cannot_shadow_builtin(monkeypatch):
    """插件叫 `sync` 不能顶掉内建 sync。"""
    _install(monkeypatch, _EP("sync", lambda argv: 99), _EP("doctor", lambda argv: 99))
    assert cli._plugin_commands() == {}


def test_broken_plugin_does_not_crash_ops(monkeypatch, capsys):
    _install(monkeypatch, _EP("bad", None, fail=True))
    monkeypatch.setattr(sys, "argv", ["ops", "bad"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    assert "加载失败" in capsys.readouterr().err


def test_help_lists_plugins(monkeypatch, capsys):
    _install(monkeypatch, _EP("hello", lambda a: 0), _EP("world", lambda a: 0, dist_name="acme-ops"))
    monkeypatch.setattr(sys, "argv", ["ops", "help"])
    cli.main()
    out = capsys.readouterr().out
    assert "已安装的扩展" in out and "ops hello" in out and "[acme-ops]" in out


def test_help_without_plugins_unchanged(monkeypatch, capsys):
    _install(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["ops", "--help"])
    cli.main()
    assert "已安装的扩展" not in capsys.readouterr().out


def test_unknown_input_goes_to_default_handler(monkeypatch):
    """既非内建也非插件 → 交给 noc_ops.default_command 的处理器（如扩展包里的终端 agent），整段 argv 原样传。"""
    seen = {}
    _install(monkeypatch, _EP("agent", lambda argv: seen.setdefault("argv", argv) and 0))
    monkeypatch.setattr(sys, "argv", ["ops", "有几台", "机器"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and seen["argv"] == ["有几台", "机器"]


def test_no_default_handler_gives_hint_not_traceback(monkeypatch, capsys):
    _install(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["ops", "有几台机器"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "没有安装处理自然语言输入的扩展包" in err and "multicloud-ops skill" in err


def test_default_handler_listed_in_help(monkeypatch, capsys):
    _install(monkeypatch, _EP("agent", lambda argv: 0, dist_name="acme-ops"))
    monkeypatch.setattr(sys, "argv", ["ops", "help"])
    cli.main()
    out = capsys.readouterr().out
    assert "默认处理器 agent" in out and "[acme-ops]" in out


def test_plugin_command_takes_precedence_over_default_handler(monkeypatch):
    """同一个 entry point 列表里既有子命令又有默认处理器：命中子命令名时走子命令。"""
    hit = {}
    _install(monkeypatch, _EP("hello", lambda argv: hit.setdefault("who", "cmd") and 3))
    monkeypatch.setattr(sys, "argv", ["ops", "hello"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 3 and hit["who"] == "cmd"


def test_deploy_config_section_passthrough(monkeypatch):
    monkeypatch.setattr(deploy_config, "_raw", lambda: {"feishu_bot": {"admin_name": "A"}, "aws": {}})
    assert deploy_config.section("feishu_bot") == {"admin_name": "A"}
    assert deploy_config.section("nope") == {}
    s = deploy_config.section("feishu_bot"); s["x"] = 1          # 返回的是拷贝
    assert deploy_config.section("feishu_bot") == {"admin_name": "A"}


def test_version_prints_core_and_plugin_dists(monkeypatch, capsys):
    """ops --version / -V / version：首行 noc-ops 版本，其后每个扩展包一行（按分发包去重），不进 gate、不进默认处理器。"""
    import noc_ops

    class _Dist:
        def __init__(self, name, version):
            self.metadata, self.version = {"Name": name}, version

    d = _Dist("acme-ops", "9.9.9")
    ep1 = _EP("hello", lambda argv: 0)
    ep2 = _EP("bye", lambda argv: 0)
    ep1.dist = ep2.dist = d
    _install(monkeypatch, ep1, ep2)
    monkeypatch.setattr(cli, "_run_default", lambda argv: pytest.fail("不该进默认处理器"))
    for flag in ("--version", "-V", "version"):
        monkeypatch.setattr(sys, "argv", ["ops", flag])
        cli.main()
        out = capsys.readouterr().out
        assert out == f"noc-ops {noc_ops.__version__}\nacme-ops 9.9.9\n", out


def test_version_without_plugins_is_single_line(monkeypatch, capsys):
    _install(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["ops", "--version"])
    cli.main()
    assert capsys.readouterr().out.strip().split() == ["noc-ops", __import__("noc_ops").__version__]
