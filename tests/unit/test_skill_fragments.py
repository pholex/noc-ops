"""`ops setup` 渲染完整版 skill 时自动拼入插件提供的片段（entry point 组 noc_ops.skill_fragments）。离线。"""
from pathlib import Path

import pytest
import yaml

from noc_ops import setup_clients

pytestmark = pytest.mark.unit


class _EP:
    def __init__(self, name, obj, fail=False):
        self.name, self.value, self._obj, self._fail = name, f"pkg.mod:{name}", obj, fail

    def load(self):
        if self._fail:
            raise ImportError("boom")
        return self._obj


def _install(monkeypatch, *eps):
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: list(eps))


def test_no_fragments_renders_template_only(monkeypatch):
    _install(monkeypatch)
    out = setup_clients.render_skill(Path("/w"))
    assert "插件补充" not in out and "{{NOC_HOME}}" not in out and "/w/" in out


def test_render_records_generating_package_version(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(setup_clients, "__version__", "9.8.7")
    out = setup_clients.render_skill(Path("/w"))
    metadata = yaml.safe_load(out.split("---", 2)[1])["metadata"]
    assert metadata["version"] == "9.8.7"
    assert "{{NOC_VERSION}}" not in out


def test_fragments_appended_sorted_with_noc_home_substituted(monkeypatch, capsys):
    _install(monkeypatch,
             _EP("zeta", lambda: "- 规则 Z：看 {{NOC_HOME}}/runbooks/z.md"),
             _EP("alpha", "- 规则 A（字符串常量也行）"))
    out = setup_clients.render_skill(Path("/w"))
    body = out.split("## 插件补充")[1]
    assert "<!-- fragment: alpha -->" in body and "<!-- fragment: zeta -->" in body
    assert body.index("fragment: alpha") < body.index("fragment: zeta")          # 按名字稳定排序
    assert "/w/runbooks/z.md" in body and "{{NOC_HOME}}" not in out
    assert "拼入 2 个插件 skill 片段" in capsys.readouterr().out


def test_broken_or_empty_fragment_skipped(monkeypatch, capsys):
    _install(monkeypatch,
             _EP("bad", None, fail=True),
             _EP("wrongtype", lambda: 123),
             _EP("empty", lambda: "   \n"),
             _EP("ok", lambda: "- 唯一有效的一条"))
    out = setup_clients.render_skill(Path("/w"))
    assert out.count("<!-- fragment:") == 1 and "唯一有效的一条" in out
    err = capsys.readouterr().out
    assert "`bad`" in err and "`wrongtype`" in err and "已跳过" in err


def test_install_skill_uses_render(monkeypatch, tmp_path):
    """_install_skill 走 render_skill：片段落到写出的 SKILL.md 里（兜底路径，不调 npx）。"""
    _install(monkeypatch, _EP("x", lambda: "- 片段 X"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(setup_clients, "_fanout_via_skills_cli", lambda staging: False)
    setup_clients._install_skill(tmp_path / "ws")
    text = (tmp_path / ".agents" / "skills" / setup_clients.SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    assert "- 片段 X" in text and str(tmp_path / "ws") in text
