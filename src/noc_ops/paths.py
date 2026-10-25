"""工作区（workspace）路径解析 —— 把"代码位置"和"可变数据位置"彻底分开。

代码装进 pipx/uv 的 venv（只读）；所有可变数据（inventory / runbook / data.json / 日志 / 快照）
落在**工作区**。工作区解析顺序：
1. 环境变量 `NOC_HOME`
2. `ops setup` 写的配置 `~/.config/noc-ops/config.toml` 的 `workspace`
3. 默认 `~/.noc`

布局：
  <workspace>/
    inventory.md          # sync 生成（人读）
    data.json             # sync 生成（看板/MCP 机读）
    runbooks/*.md          # 能力库（用户沉淀，随用增长）
    .state/                # 运行时状态（ssh 用户缓存 / 快照 / 操作日志）
"""
import os
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "noc-ops" / "config.toml"


def _from_config():
    if not CONFIG_FILE.exists():
        return None
    try:
        import tomllib
        ws = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("workspace")
        return Path(ws).expanduser() if ws else None
    except Exception:
        return None


def workspace_dir() -> Path:
    env = os.environ.get("NOC_HOME")
    if env:
        return Path(env).expanduser()
    cfg = _from_config()
    if cfg:
        return cfg
    return Path.home() / ".noc"


def save_workspace(path: Path):
    """把工作区路径写进配置（ops setup 用）。"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f'workspace = "{Path(path).expanduser().resolve()}"\n', encoding="utf-8")


def inventory_path() -> Path:
    return workspace_dir() / "inventory.md"


def data_json_path() -> Path:
    return workspace_dir() / "data.json"


def deploy_config_path() -> Path:
    return workspace_dir() / "ops-config.toml"


def local_servers_path() -> Path:
    return workspace_dir() / "local-servers.toml"


def runbooks_dir() -> Path:
    return workspace_dir() / "runbooks"


def state_dir() -> Path:
    return workspace_dir() / ".state"


def ssh_users_path() -> Path:
    return state_dir() / "ssh-users.json"


def snapshot_path() -> Path:
    return state_dir() / "inventory-snapshot.json"


def ops_log_path() -> Path:
    return state_dir() / "ops-log.jsonl"


def ensure_workspace() -> Path:
    """首启确保工作区存在；runbooks/ 不存在时用包内种子填充。返回工作区路径。"""
    ws = workspace_dir()
    ws.mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)
    # 工作区无部署配置时，用包内默认 seed 一份（之后编辑工作区那份即可）
    cfg = deploy_config_path()
    if not cfg.exists():
        try:
            from importlib import resources
            default = (resources.files("noc_ops") / "assets" / "ops-config.default.toml").read_text(encoding="utf-8")
            cfg.write_text(default, encoding="utf-8")
        except Exception:
            pass
    rb = runbooks_dir()
    if not rb.exists() or not any(rb.glob("*.md")):
        rb.mkdir(parents=True, exist_ok=True)
        try:
            from importlib import resources
            seed = resources.files("noc_ops") / "assets" / "runbooks"
            for f in seed.iterdir():
                if f.name.endswith(".md"):
                    (rb / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    return ws
