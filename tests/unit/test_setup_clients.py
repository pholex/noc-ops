"""Tier 1：ops setup 客户端接入——MCP 写入器（Codex TOML / Claude Desktop JSON）幂等与隔离。

不跑 `npx add-mcp`（需网络/node）；只测无 npx 时的兜底写入器：幂等、保留其它块/server、清旧名。
"""
import json

import pytest

from noc_ops import setup_clients as s

pytestmark = pytest.mark.unit


def test_strip_mcp_blocks_keeps_others():
    text = (
        "[mcp_servers.other]\ncommand = \"x\"\n\n"
        "[mcp_servers.multicloud-ops]\ncommand = \"ops-mcp\"\n"
        "env = { NOC_HOME = \"/a\" }\n\n"
        "[mcp_servers.ops]\ncommand = \"old\"\n\n"        # 旧名
        "[other_section]\nk = 1\n")
    out = s._strip_mcp_blocks(text)
    assert "[mcp_servers.multicloud-ops]" not in out
    assert "[mcp_servers.ops]" not in out
    assert "[mcp_servers.other]" in out                   # 别人的 server 保留
    assert "[other_section]" in out


def test_register_codex_mcp_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    s._register_codex_mcp(ws)
    s._register_codex_mcp(ws)
    cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert cfg.count("[mcp_servers.multicloud-ops]") == 1
    assert str(ws) in cfg


def test_register_codex_mcp_preserves_other(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text("[mcp_servers.other]\ncommand = \"x\"\n", encoding="utf-8")
    s._register_codex_mcp(tmp_path / "ws")
    out = (codex / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in out
    assert "[mcp_servers.multicloud-ops]" in out


def test_register_claude_desktop_idempotent_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    cfg = tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    # 预存别的 server + 旧名，确认不被误删（其它保留、旧名清掉）
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, "ops": {"command": "old"}}}),
                   encoding="utf-8")
    s._register_claude_desktop_mcp(ws)
    s._register_claude_desktop_mcp(ws)        # 二次幂等
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "multicloud-ops"}     # 旧名 ops 已清
    assert data["mcpServers"]["multicloud-ops"]["env"]["NOC_HOME"] == str(ws)


def test_register_claude_desktop_handles_corrupt_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not valid json", encoding="utf-8")
    s._register_claude_desktop_mcp(tmp_path / "ws")          # 不抛，重建
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "multicloud-ops" in data["mcpServers"]
