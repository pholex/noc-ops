# 贡献与扩展指南

面向两类人：想给 `noc-ops` 本身改代码的贡献者，以及想在不改公开包的前提下给 `ops` 挂私有能力的团队。普通用户不需要读这份文档，用法见 [README.md](README.md) 与 [INSTALL.md](INSTALL.md)。

## 代码结构

代码装进 venv 只读；用户数据全部在工作区（默认 `~/.noc`）。两边永远不混放。

```
src/noc_ops/
├── cli.py              # `ops` 入口：内建子命令分发；非内建输入交给 noc_ops.default_command 扩展
├── sync.py             # 扫实例 / 域名 / 数据库 / 存储 / 函数 / CDN / IAM / AI 密钥 / 消息事件 / 成本 / 额度，生成 inventory + data.json + ssh config
├── providers/          # 多云驱动：base + aws / aliyun / tencent / volcengine / gcp / azure / oracle / cloudflare / supabase / synology / vmware / sangfor / local
├── reboot_cli.py       # ops reboot：目标解析 + 按 cloud 选通道
├── db_cli.py           # ops db：托管数据库项目 status / pause / resume（目前 Supabase），写操作先确认
├── schedule_ops.py     # 计划任务（EventBridge Scheduler + Lambda 派发到非 AWS 云）
├── aws_safety.py       # 读 / 写操作分类器（安全闸门）
├── ops_log.py          # 操作日志（写操作审计流水）
├── deploy_config.py    # 读工作区 ops-config.toml；section(name) 给插件透传自有配置段
├── paths.py            # 工作区路径解析，首次使用时 seed 默认配置与种子 runbook
├── init_wizard.py      # ops init
├── doctor.py           # ops doctor
├── setup_clients.py    # ops setup：渲染 skill、经 `npx skills` 扇出到各客户端、可选接 MCP
├── mcp_server.py       # ops-mcp（[mcp] extra），给无 shell 的 GUI 客户端
├── egress_probe.py / netprobe.py   # ops netprobe 与内网 / Tailscale 探针
├── error_patterns.py   # 命令失败后的确定性诊断
├── license.py          # 授权校验（当前在 cli._gate 顶部短路，不生效）
├── dashboard/          # 看板：server.py（stdlib http）+ index.html + vendor/（自托管 ag-Grid、Inter）
├── skill/SKILL.md.tmpl # skill 模板，ops setup 渲染后安装
└── assets/runbooks/    # 全新安装的种子 runbook 与 _TEMPLATE.md
```

## 本地开发

```bash
git clone https://github.com/pholex/noc-ops && cd noc-ops
uv run --extra dev pytest -m unit -q        # Tier 1：离线、零 LLM、秒级
uv tool install --force --no-cache '.[all]' # 把本仓装成全局 ops 试跑
```

- **改完 `src/` 必须重装全局 `ops`**。全局 `ops` 是 `uv tool` 装的 wheel 拷贝，不跟随源码。漏这步会「代码改好了、跑起来还是旧行为」。
- 单测只读匿名化夹具 `tests/fixtures/workspace/ops-config.toml`，不碰任何真实工作区；改配置结构时同步改这份夹具。
- 公开仓只有 Tier 1 单测。真调 LLM 的 agent 行为评测随终端 agent 一起在扩展包里，不在这里。
- 全新机器安装测试：`bash scripts/fresh-install-test/run.sh`，在 Docker `python:3.12-slim` 里按 INSTALL.md 走一遍。
- 仓根目录可以同时当工作区用，`data.json`、`inventory.md`、`.state/`、`ops-config.toml`、`runbooks/` 都在 `.gitignore`，不要手改、不要入库。

## 新增一朵云 / 一类资源 / 一个看板 tab

新增或调整能力时，使用 [云能力核验清单](docs/cloud-capability-checklist.md) 核对官方依据、适用范围、权限、费用和失败处理。按具体能力记录验证结果，不把云厂商支持等同于全部产品或操作均支持。

这几处必须一起改，漏一处就出现「看板有、agent 不知道」：

1. `providers/<cloud>.py`：继承 `base.Provider`，实现对应的 `list_*`，能重启的实现 `reboot_argv` / `status_argv`；`sync.py` 把结果并进 `data.json`。
2. `dashboard/index.html`：tab 与列。
3. `skill/SKILL.md.tmpl`：agent 靠它才知道有这类资源、怎么查。同步到公开 skill 仓 [pholex/multicloud-ops](https://github.com/pholex/multicloud-ops) 的 `skills/multicloud-ops/SKILL.md`。
4. `README.md` 的支持矩阵。
5. 重装全局 `ops`，再运行 `ops setup` 重扇出 skill，验证生成版。

provider 的约束：云 CLI 缺失时返回空列表并只提示一次，不抛异常；空配置（账号没填区域）同样返回空；只读方法绝不带副作用。

## 扩展包：不改公开包给 `ops` 加能力

团队专属的东西（内部 IM 机器人、用量限额、流量报表、终端内的自然语言 agent）放在自己的私有包里，声明三个 entry point 组中的任意几个，`ops` 启动时自动发现：

```toml
# 你的私有包 pyproject.toml
[project]
name = "acme-ops"
dependencies = ["noc-ops"]

# ① 子命令：ops hello … → acme_ops.hello:main(argv) -> int|None
[project.entry-points."noc_ops.commands"]
hello = "acme_ops.hello:main"

# ② skill 片段：零参函数返回 markdown，ops setup 渲染 skill 时拼在「插件补充」节
[project.entry-points."noc_ops.skill_fragments"]
acme = "acme_ops.skill_fragment:fragment"

# ③ 默认处理器：既非内建也非插件的输入（自然语言 / 无参 REPL）交给它，main(argv) -> int|None；只能装一个
[project.entry-points."noc_ops.default_command"]
agent = "acme_ops.agent:main"
```

装法：`uv tool install 'noc-ops[all]' --with acme-ops`（或 `--with /path/to/acme-ops`）。

约定：

- 依赖只有一条方向：扩展包 import `noc_ops`，核心不认识任何扩展。
- 内建子命令名不可被覆盖；扩展加载失败只报该命令，不影响 `ops` 其余功能。
- 扩展读自己的配置段用 `deploy_config.section("<段名>")`，在用户的 `ops-config.toml` 里写 `[<段名>]` 即可，核心不为每个扩展写访问器。
- `ops help` 末尾会列出已安装的扩展子命令与默认处理器来自哪个包。
- 没有装默认处理器时，`ops "<自然语言>"` 退出码 2 并提示去用 skill，不会 Traceback。

## 提交

- 分支名 `feat/` `fix/` `chore/` `docs/`，做完合回 `main`，不留长命分支。
- 秘密（token / PAT / 密码）不进仓库，也不进任何示例配置。
- 示例里的账号、IP、域名用占位符或保留段：账号 `123456789012`，IP 用 RFC 5737 的 `192.0.2.x` / `198.51.100.x`，域名用 `example.com`。

## 提交检查

首次开发运行 `sh scripts/install-git-hooks.sh`。每次提交遵守 [公开内容检查](PUBLIC_CONTENT.md)，复核暂存区与完整提交说明；推送前运行 `python3 scripts/check-public-content.py --commits origin/main..HEAD`。检查器只输出位置与类别。
