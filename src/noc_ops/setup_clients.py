"""ops setup —— 多客户端一键安装：记工作区 + 把 skill 扇出到各 LLM 客户端（+ 可选 MCP）。

渠道策略 skill-first：有 shell 的客户端（Claude Code / Codex）走 skill 功能最强，是默认；
MCP 仅无 shell 的 GUI（如 Claude Desktop）才需要，走 `--with-mcp` 显式开启。

- 工作区路径写进 ~/.config/noc-ops/config.toml
- skill 渲染到规范库 ~/.agents/skills/multicloud-ops（去硬编码、读 NOC_HOME），
  再用 `skills` CLI（`npx skills`）扇出到**所有已装客户端**（Claude Code/Codex/Cursor/Gemini/Kiro/Cline…50+ 家）。
- （--with-mcp）MCP：外包给 `npx add-mcp` 扇出到各客户端（含 Claude Desktop）；无 npx 时
  兜底自己写 Codex TOML + Claude Desktop JSON。
幂等：可重复运行。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import paths
from . import __version__

_MCP_PATH_ENV = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"   # GUI 客户端 PATH 常残缺，显式补


SKILL_NAME = "multicloud-ops"        # skill 目录名/标识；客户端按 name 自动渲染显示名 → "Multicloud Ops"
_SKILL_LEGACY = ("ops",)             # 旧名，安装时清掉（迁移）


def _rm_skill_path(p: Path):
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def _fanout_via_skills_cli(staging_skill: Path) -> bool:
    """用 `skills` CLI 把**暂存目录**里的 skill 拷进规范库 + 扇出到所有已装客户端，成功返回 True。

    `skills`（npm 包，`npx` 即取即用）是通用多客户端安装器，内置 50+ 客户端的目录约定
    （Claude Code/Codex/Cursor/Gemini CLI/Kiro/Cline/Amp…），软链或 universal 写入由它按家区分。
    `add <本地路径> -g -a '*' -y`：把该 skill 全局装到所有 agent，幂等可重复跑；内容是**拷贝**进
    规范库 ~/.agents/skills/<name>（删掉源后规范库仍在）。

    ⚠️ 源**必须**是独立于规范库的暂存目录：`skills add <dir>` 安装前会先清空目标目录，
    若把源直接指向 ~/.agents/skills/<name> 自身，会「先清空=把源删了」→ 规范库变空、各客户端软链全部悬空。
    需要本机有 node/npx；没有则由调用方回退到「仅 Claude 软链」。"""
    npx = shutil.which("npx")
    if not npx:
        return False
    try:
        r = subprocess.run(
            [npx, "-y", "skills", "add", str(staging_skill), "-g", "-a", "*", "-y"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠️ 调 `skills` 出错（回退仅 Claude）：{e}")
        return False
    if r.returncode != 0:
        print(f"  ⚠️ `skills` 扇出失败（回退仅 Claude）：{(r.stderr or r.stdout).strip()[:200]}")
        return False
    print("  ✅ 经 `skills` 扇出到所有已装客户端（Claude Code / Codex / Gemini CLI / Kiro …）")
    return True


def _install_to_store_and_claude(agents_skill: Path, rendered: str):
    """无 npx 时的兜底：自己写规范库 + 软链 Claude Code（与旧行为一致）。"""
    agents_skill.mkdir(parents=True, exist_ok=True)
    (agents_skill / "SKILL.md").write_text(rendered, encoding="utf-8")
    claude_skill = Path.home() / ".claude" / "skills" / SKILL_NAME
    claude_skill.parent.mkdir(parents=True, exist_ok=True)
    _rm_skill_path(claude_skill)
    try:
        claude_skill.symlink_to(agents_skill, target_is_directory=True)
        print(f"  ✅ Claude Code skill → {claude_skill}（未经 `skills` 扇出，仅装 Claude；"
              "需 Node ≥ 22 的 npx，装好后重跑 `ops setup` 即扇出全客户端）")
    except OSError:
        claude_skill.mkdir(parents=True, exist_ok=True)
        (claude_skill / "SKILL.md").write_text(rendered, encoding="utf-8")
        print(f"  ✅ Claude Code skill → {claude_skill}（直接写入，未软链）")


SKILL_FRAGMENT_GROUP = "noc_ops.skill_fragments"


def _plugin_skill_fragments(noc_home: Path) -> list:
    """已安装插件提供的 skill 片段 [(entry point 名, markdown 文本)]，按名字排序，稳定可重现。

    插件在 pyproject 声明 `[project.entry-points."noc_ops.skill_fragments"]`，值指向一个
    **无参、返回 markdown 字符串**的函数（或一个字符串常量）。片段里的 `{{NOC_HOME}}` 同样替换。
    用途：公司/团队专属子命令（经 noc_ops.commands 挂入的那些）把「agent 该怎么用它们」的规则随
    自己的包一起发，`ops setup` 渲染完整版 skill 时自动拼在末尾——不再需要手工往渲染结果里贴，
    也不会被下一次 `ops setup` 覆盖掉。公开包本身不提供任何片段。坏片段只警告、不影响主体。"""
    from importlib.metadata import entry_points
    try:
        eps = entry_points(group=SKILL_FRAGMENT_GROUP)
    except TypeError:                                  # Python 3.10/3.11 的 dict 风格返回
        eps = entry_points().get(SKILL_FRAGMENT_GROUP, [])
    out = []
    for ep in sorted(eps, key=lambda e: e.name):
        try:
            obj = ep.load()
            text = obj() if callable(obj) else obj
            if not isinstance(text, str):
                raise TypeError(f"片段应为 str，得到 {type(text).__name__}")
        except Exception as e:
            print(f"  ⚠️ skill 片段 `{ep.name}`（{ep.value}）加载失败，已跳过：{e}")
            continue
        text = text.strip()
        if text:
            out.append((ep.name, text.replace("{{NOC_HOME}}", str(noc_home))))
    return out


def render_skill(noc_home: Path) -> str:
    """完整版 skill 正文 = 公开模板渲染 + 各插件片段（有则拼在末尾，各占一节）。"""
    tmpl = (resources.files("noc_ops") / "skill" / "SKILL.md.tmpl").read_text(encoding="utf-8")
    rendered = (tmpl.replace("{{NOC_HOME}}", str(noc_home))
                .replace("{{NOC_VERSION}}", __version__).rstrip() + "\n")
    frags = _plugin_skill_fragments(noc_home)
    if frags:
        parts = [rendered, "\n---\n\n## 插件补充（来自已安装的扩展包，`ops setup` 自动拼入）\n"]
        for name, text in frags:
            parts.append(f"\n<!-- fragment: {name} -->\n{text}\n")
        rendered = "".join(parts)
        print(f"  ✅ 拼入 {len(frags)} 个插件 skill 片段：{', '.join(n for n, _ in frags)}")
    return rendered


def _install_skill(noc_home: Path):
    rendered = render_skill(noc_home)
    agents_root = Path.home() / ".agents" / "skills"
    claude_root = Path.home() / ".claude" / "skills"
    # 清掉旧名（迁移）：~/.agents/skills/ops 与 ~/.claude/skills/ops
    for old in _SKILL_LEGACY:
        _rm_skill_path(agents_root / old)
        _rm_skill_path(claude_root / old)
    agents_skill = agents_root / SKILL_NAME
    # 渲染到**独立暂存目录**（目录名同 SKILL_NAME，与 frontmatter name 对齐），交给 `skills`
    # 拷进规范库 + 扇出；用完即删。直接拿规范库当源会被 skills 的「装前先清空」自删（见上）。
    staging_root = Path(tempfile.mkdtemp(prefix="noc-skill-"))
    staging_skill = staging_root / SKILL_NAME
    staging_skill.mkdir(parents=True)
    (staging_skill / "SKILL.md").write_text(rendered, encoding="utf-8")
    try:
        if not _fanout_via_skills_cli(staging_skill):
            _install_to_store_and_claude(agents_skill, rendered)  # 兜底
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


MCP_NAME = "multicloud-ops"          # Codex 等客户端里显示的 MCP server 名
_MCP_LEGACY = ("ops",)               # 旧名，setup 时一并清掉（迁移）


def _strip_mcp_blocks(text: str) -> str:
    """删掉本 server 的 [mcp_servers.<name>]（含旧名）及其子表，保留其它所有块。"""
    names = (MCP_NAME, *_MCP_LEGACY)
    out, skip = [], False
    for ln in text.splitlines(keepends=True):
        s = ln.lstrip()
        if s.startswith("["):
            skip = any(s.startswith(f"[mcp_servers.{n}]") or s.startswith(f"[mcp_servers.{n}.")
                       for n in names)
        if not skip:
            out.append(ln)
    return "".join(out)


def _mcp_command() -> str:
    return shutil.which("ops-mcp") or "ops-mcp"


def _fanout_mcp_via_add_mcp(noc_home: Path) -> bool:
    """用 `npx add-mcp` 把本地 stdio server 扇出到所有已装客户端（含 Claude Desktop），成功返回 True。

    `add-mcp`（npm 包，`npx` 即取即用）是 MCP 版的 `skills`：支持自定义本地 command server + env，
    内置 14+ 客户端的配置路径与格式（Claude Desktop/Codex/Cursor/Claude Code/Windsurf…，各家格式它自己兜）。
    与 skill 对称，我们不再维护逐客户端路径表。需本机有 node/npx；没有则回退到下面的最小 in-house 写入。"""
    npx = shutil.which("npx")
    if not npx:
        return False
    cmd = _mcp_command()
    try:
        r = subprocess.run(
            [npx, "-y", "add-mcp", cmd, "--env", f"NOC_HOME={noc_home}",
             "--env", f"PATH={_MCP_PATH_ENV}", "--all", "-g", "-y"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ⚠️ 调 `add-mcp` 出错（回退 Codex+Claude Desktop）：{e}")
        return False
    if r.returncode != 0:
        print(f"  ⚠️ `add-mcp` 扇出失败（回退 Codex+Claude Desktop）：{(r.stderr or r.stdout).strip()[:200]}")
        return False
    print(f"  ✅ 经 `add-mcp` 扇出 MCP 到各已装客户端（含 Claude Desktop；command={cmd}，重启客户端生效）")
    return True


def _register_codex_mcp(noc_home: Path):
    """兜底：往 ~/.codex/config.toml 幂等写入 [mcp_servers.<name>]（清旧名、保留其它块）。"""
    cfg = Path.home() / ".codex" / "config.toml"
    cmd = _mcp_command()
    block = (
        f"\n[mcp_servers.{MCP_NAME}]\n"
        f'command = "{cmd}"\n'
        f'env = {{ NOC_HOME = "{noc_home}", '
        f'PATH = "{_MCP_PATH_ENV}" }}\n'
    )
    existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    updated = _strip_mcp_blocks(existing).rstrip()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text((updated + "\n" + block) if updated else block.lstrip("\n"), encoding="utf-8")
    print(f"  ✅ Codex MCP → {cfg}（[mcp_servers.{MCP_NAME}]；重启 Codex 生效）")


def _register_claude_desktop_mcp(noc_home: Path):
    """兜底：往 Claude Desktop 的 claude_desktop_config.json 幂等写入 mcpServers.<name>。"""
    cfg = (Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers.pop("ops", None)            # 清旧名
    servers[MCP_NAME] = {
        "command": _mcp_command(),
        "env": {"NOC_HOME": str(noc_home), "PATH": _MCP_PATH_ENV},
    }
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✅ Claude Desktop MCP → {cfg}（mcpServers.{MCP_NAME}；重启 Claude Desktop 生效）")


def _install_mcp(noc_home: Path):
    """MCP 接入（--with-mcp）：优先 `npx add-mcp` 扇出，无 npx 时兜底 Codex+Claude Desktop。"""
    if not _fanout_mcp_via_add_mcp(noc_home):
        _register_codex_mcp(noc_home)
        _register_claude_desktop_mcp(noc_home)
        print("  （未发现 npx，仅兜底 Codex + Claude Desktop；装 node 后重跑 `ops setup --with-mcp` 即扇出全客户端）")


def main(argv=None):
    # ops setup [--workspace <path>] [--with-mcp]
    argv = sys.argv[1:] if argv is None else argv
    if "--workspace" in argv:
        ws = Path(argv[argv.index("--workspace") + 1]).expanduser().resolve()
        paths.save_workspace(ws)        # 先记配置，ensure_workspace 才会用它
    ws = paths.ensure_workspace()
    paths.save_workspace(ws)
    print(f"ops setup —— 工作区: {ws}")
    print(f"  ✅ 工作区已记入 {paths.CONFIG_FILE}")
    _install_skill(ws)
    if "--with-mcp" in argv:
        _install_mcp(ws)
    print("\n完成。各已装客户端新会话即可用 multicloud-ops skill。")
    if "--with-mcp" not in argv:
        print("（无 shell 的 GUI 客户端如 Claude Desktop 需结构化工具：`ops setup --with-mcp`）")
    print("下一步：`ops init` 生成配置 → `ops doctor` 体检 → `ops sync` 扫云。")
    return 0
