# 安装指引 —— noc-ops（多云运维 ops CLI）

面向**新用户**：装完后你得到一个全新的 `ops`，管理**你自己**的云资源
（AWS / 阿里云 / 腾讯云 / 火山引擎 / Google Cloud / Microsoft Azure / Oracle Cloud / Cloudflare / Supabase，以及内网机器）。
全程不含分发者的任何账号信息——你只需填上自己的账号。

支持 **macOS（主要平台）与 Linux**（纯 Python 包），Python ≥ 3.11。**不支持 Windows**，WSL 未验证。

---

## 快速安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/pholex/noc-ops/main/scripts/install.sh | sh     # 检查本机，仅补齐缺失项
```

脚本先检查 `uv`、`noc-ops` 的版本及已有工作区配置和 skill：已安装的工具默认跳过，不重装、不升级；已有配置和 skill 时不重复运行 `ops setup`。如果只缺客户端配置，就只运行 `ops setup`；如果现有 noc-ops 可用且不安装依赖 uv 的工具，就不补装 uv。无法运行的 uv 或冲突的 `ops` 命令会提示先修复，不自动覆盖。

skill 的版本记录在 `SKILL.md` 的 `metadata.version`，对应生成时的 noc-ops 版本。安装检查读取已安装 skill 的实际记录；旧 skill 没有记录时显示“版本未标注”，不会套用当前程序版本。更新程序后可手动运行 `ops setup` 重新生成 skill 并记录版本；插件补充片段不单独计入这个版本号。

随后检查 `aws`、`aliyun`、`tccli`、`coscli`、`ve`、`tosutil`、`gcloud`、`az`、`oci`：已安装的显示本机版本，缺失的给出官方安装指引；版本读取失败也会提示并继续安装。Cloudflare / Supabase 无需 CLI。

仅将缺失的云 CLI 列入交互菜单（`curl | sh` 也支持）。若全部已安装，跳过云 CLI 选择；基础工具或配置还有缺失时，展示具体步骤并询问是否执行：输入 `y` 继续，回车或 `q` 退出。全部就绪时直接结束，不再显示安装确认。

有缺失工具时：

- 回车或 `0`：云 CLI 自行处理，仅补齐缺失的基础工具和配置。
- 输入多个编号，如 `1 3 5`：预览所选工具的安装命令，输入 `y` 确认后执行；已安装的跳过，不升级。
- 输入 `q`：直接退出，此时还没有安装 uv、云 CLI 或 noc-ops，也没有执行 `ops setup`。

自动安装方式：AWS、阿里云、Google Cloud、Azure 使用 macOS 上已有的 Homebrew；腾讯云、Oracle 使用 uv 的隔离环境；火山引擎使用已有的 npm。COS、TOS，以及缺少相应包管理器或平台不适用的工具，提供手工安装指引。单个云 CLI 安装失败会提示并继续其余步骤。

没有交互终端时默认跳过云 CLI 安装，只补齐缺失的基础工具和配置。自动化可显式设置 `NOC_NONINTERACTIVE=1`（管道安装时放在 `sh` 前：`curl -fsSL <脚本URL> | NOC_NONINTERACTIVE=1 sh`）。

只有显式设置 `NOC_PACKAGE`（例如测试 wheel）或 `NOC_EXTRAS`（例如 `[mcp]`），才会按指定安装源或 extra 替换现有 noc-ops 并刷新 skill。新增客户端或需要刷新已有 skill 时，手动运行 `ops setup`。

检查仅确认本机安装情况，不验证凭证或查询最新版本。按需安装你使用的云工具并配置凭证后，再运行：

```bash
ops init          # 交互式生成配置（自动发现你的 AWS 账号/区域/SSH key）
ops doctor --fix  # 体检，并自动修可修项
ops sync          # 首次扫云，生成清单
```

> 偏好手动、或要了解每步细节，见下方分步说明。

---

## 0. 前置依赖

| 依赖 | 用途 | 备注 |
|---|---|---|
| **Python ≥ 3.11** | 运行 `ops` 本体 | `python3 --version` |
| **uv** 或 **pipx** | 安装 CLI（任选其一） | `curl -LsSf https://astral.sh/uv/install.sh \| sh` 或 `brew install uv` |
| **Node ≥ 22（可选）** | 把 skill 一键扇出到所有 LLM 客户端 | 没有也行：只装 Claude Code；`npx skills` 要求 Node ≥ 22.20 |
| **各云 CLI + 凭证** | 实际操作云资源 | 用到哪个云就装哪个；安装脚本检查上列 CLI，凭证需另行配置 |

云 CLI（按需）：

| 云 | 安装（macOS；Linux 点云名看官方说明） | 凭证配置 |
|---|---|---|
| [AWS](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | `curl -fsSL https://awscli.amazonaws.com/v2/install.sh \| bash` | `aws configure --profile <账号>`，每个账号一个 profile |
| [阿里云](https://help.aliyun.com/zh/cli/install-cli-on-macos) | `brew install aliyun-cli` | `aliyun configure --profile <名>` |
| [腾讯云](https://cloud.tencent.com/document/product/1278/46705) | `pip install tccli` | `tccli configure` |
| [腾讯云 COS](https://cloud.tencent.com/document/product/436/63144)（看桶才需要） | 下载 `coscli` 二进制，`chmod 755` 后放进 PATH | `coscli config init` |
| [火山引擎](https://github.com/volcengine/volcengine-cli) | `npm install -g @volcengine/cli`（命令名 `ve`） | `ve configure`（仅单账号） |
| [火山引擎 TOS](https://docs.volcengine.com/docs/6349/152751)（看桶才需要） | 下载 `tosutil` 二进制，`chmod a+x` 后放进 PATH | `tosutil config -interactive` |
| [Google Cloud](https://docs.cloud.google.com/sdk/docs/install) | `brew install --cask gcloud-cli` | `gcloud auth login` |
| [Azure](https://learn.microsoft.com/cli/azure/install-azure-cli-macos) | `brew install azure-cli` | `az login` |
| [Oracle Cloud](https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm) | `brew install oci-cli` | `oci setup config` |
| Cloudflare / Supabase | 无 CLI | API token 存系统 Keychain / 环境变量，见 `ops-config.toml` 注释 |

SSH 私钥按约定放 `~/.ssh/<账号或profile>/<keyfile>.pem`。

> 只用 AWS 的话，其余云的 CLI 都可以不装；没装 CLI 的云会在 `ops sync` 时自动跳过。

---

## 1. 安装 CLI

```bash
uv tool install noc-ops            # 推荐；或 pipx install noc-ops
```

装好后有 `ops` 命令，二进制落在 `~/.local/bin`，确认它在 `PATH` 里：

```bash
ops --version  # 打印版本即安装成功；ops --help 看完整用法
```

**可选组件**（按需加 extra）：

```bash

uv tool install --force 'noc-ops[mcp]'     # ops-mcp：仅无 shell 的 GUI 客户端（Claude Desktop）需要

```

核心功能（清单同步 / 看板 / 定时任务 / 体检 / skill）只用 Python 标准库，不加 extra 也完整可用。

---

## 2. 初始化工作区

`ops` 把**代码**（只读，在 venv 里）和**工作区**（你的可变数据）分开：

```bash
ops setup --workspace ~/noc       # 不带 --workspace 默认用 ~/.noc
```

`ops setup` 会：
1. 记下工作区路径；
2. 把**出厂配置模板**复制成 `<工作区>/ops-config.toml`（下一步用 `ops init` 填好）；
3. 装好 skill（`multicloud-ops`）并扇出到所有已装客户端（Claude Code / Codex / Gemini CLI / Kiro …）。
   没有 Node ≥ 22 时只装 Claude Code，装好 Node 后重跑一次即可扇出全部。

> 渠道策略 **skill 优先**：有 shell 的客户端（Claude Code / Codex）走 skill 功能最强，是默认。
> 只有**无 shell 的 GUI 客户端（如 Claude Desktop）**才需要 MCP——装 `noc-ops[mcp]` 后跑 `ops setup --with-mcp`。

工作区布局：

```
<工作区>/
├── ops-config.toml   # ← 你要编辑的部署配置（账号/区域，非密）
├── inventory.md      # 实例清单（ops sync 生成）
├── data.json         # 机读数据（ops sync 生成，看板用）
├── runbooks/*.md     # 能力库（用着用着自己长出来）
└── .state/           # 运行时状态（缓存/快照/操作日志）
```

---

## 3. 填上你自己的账号（关键一步）

**推荐用向导**——自动发现本机 AWS profile、用 `sts` 推导账号 ID 与 partition、建议区域、检测 SSH key：

```bash
ops init
```

问答式生成 `<工作区>/ops-config.toml`（已存在会先备份 `.bak`），可重复跑。
向导目前覆盖 AWS / 阿里云 / 腾讯云；其余云按模板里注释掉的段落手动取消注释填好即可。

<details><summary>或：手动编辑 ops-config.toml</summary>

把占位符换成你的账号/区域。**只放非密信息**（账号 ID、区域、profile 名、ssh 用户名、私钥**文件名**）——**绝不**写 access key / 私钥内容 / 密码。

最小可用示例（只用 AWS 单账号）：

```toml
[aws]
route53_profiles = ["123456789012"]                 # 要扫域名的 global 账号；不扫就留 []
scan_regions_global = ["us-east-1", "us-west-2"]     # 你实际有机器的区域
scan_regions_china = ["cn-northwest-1"]

[[aws.accounts]]
profile = "123456789012"      # 你 `aws configure` 里的 profile 名
partition = "global"          # global | china
```

其它云参考模板里 `[[aliyun.accounts]]` / `[tencent]` / `[[gcp.accounts]]` / `[[azure.accounts]]` / `[[oracle.accounts]]` 等被注释掉的段。**改完配置不用改任何代码。**

</details>

---

## 4. 体检 + 首次同步

```bash
ops doctor --fix   # 体检：部分云 CLI / AWS 默认凭证 / 工作区 / 配置 / 客户端接入；--fix 自动修可修项
ops sync           # 全量扫描你的云，生成 inventory.md + data.json + ~/.ssh/config 段
```

`ops doctor` 会给出未通过项的处理提示。目前云 CLI 检查覆盖 AWS、阿里云、腾讯云和火山引擎，凭证探测仅覆盖 AWS 默认 profile。只用部分云的话，忽略其余云 CLI 缺失的提示即可；`--fix` 不会安装云 CLI。

---

## 5. 开用

在 Claude Code / Codex 里直接说话（skill 已接入）：「东京有几台 EC2 在运行」「重启 xxx」「同步清单」。

终端命令：

```bash
ops sync                       # 新增实例/账号后重新同步
ops noc                        # 本地 NOC 运维看板（默认后台，http://127.0.0.1:8765）
ops noc status | stop | restart

```

---

## 安全模型（默认行为）

| 操作类型 | 行为 |
|---|---|
| 只读（describe/list/get、状态查询） | **自动执行**，不打扰 |
| 实例重启 | **直接执行**（低风险可逆） |
| 其它写操作（停止/销毁/改配置/SSH 改动/删资源） | **先回显计划、等你确认**才做 |

写操作都会记进 `<工作区>/.state/ops-log.jsonl`（审计流水，看板「操作日志」页可看）。

---

## 常见问题

- **`ops: command not found`** —— `~/.local/bin` 不在 PATH。`export PATH="$HOME/.local/bin:$PATH"`。
- **`ops doctor` 报某云 CLI 缺失** —— 没装/没配该云 CLI。只用 AWS 的话忽略其余即可。
- **`ops "<指令>"` 提示没有安装扩展包** —— 终端自然语言 agent 不在公开包；在 Claude Code / Codex 等客户端里用 skill 即可自然语言运维。
- **`ops setup` 提示 skills 扇出失败** —— 本机 Node 低于 22.20；只影响 Codex/Gemini CLI 等的自动接入，Claude Code 已装好。
- **升级** —— `ops update --check` 查询版本，`ops update --dry-run` 预览，`ops update` 确认后更新并刷新 skill；沿用安装来源、extra、扩展及版本约束。工作区数据独立保存。0.2.x 尚无此命令时先运行 `uv tool upgrade noc-ops`，再运行 `ops setup`。详见 [RELEASE.md](RELEASE.md)。
