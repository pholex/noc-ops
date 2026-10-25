# noc —— 本仓开发约定

本文件只写**在这个仓库里改代码时**的硬规矩，不复述项目介绍。
- 项目是什么 / 怎么装怎么用 → `README.md`、`INSTALL.md`
- 怎么用它做多云运维（各云 CLI、安全闸门、runbook）→ skill `multicloud-ops`（源模板 `src/noc_ops/skill/SKILL.md.tmpl`）

## 代码 ↔ 数据分离
`src/noc_ops/` 是代码（打包进 venv，只读）；清单/看板数据/运行状态属于**工作区**（默认 `~/.noc`，`NOC_HOME` 或 `ops setup --workspace` 可指定）。
本仓根目录同时**就是**当前工作区，所以仓里混着两类东西：`data.json`、`inventory.md`、`.state/`、`local-servers.toml` 都是 `ops sync` 等生成的产物，**不手改、不入库**（已在 `.gitignore`）。要改它们的内容，改生成它们的代码。
`ops-config.toml`、`runbooks/` 与其他工作区数据不入库。单测使用 `tests/fixtures/workspace/ops-config.toml` 中的匿名化夹具；配置结构变更时同步维护夹具。

## 改完 src/ 必须重装全局 ops
全局 `ops` 是 `uv tool` 装的 wheel **拷贝**，不跟随 `src/`。改完源码（尤其新增子命令）立刻：

```bash
uv tool install --force --no-cache '<本仓绝对路径>[all]'   # [all] 带上 mcp 可选依赖；本机还要 --with 私有扩展包，别把 agent 弄丢
```

漏这步会让"代码已改好、跑起来还是旧行为"，排查成本极高。

## 测试
```bash
uv run --extra dev pytest -m unit -q     # Tier 1：离线、零 LLM、秒级，改完就跑
```
公开仓只有 Tier 1；agent 行为评测（Tier 2，真调 LLM）随终端 agent 一起在 noc-internal。

## 新增 provider / 资源种类 / 看板 tab 的同步清单
这几处必须一起改，漏一处就出现「看板有、agent 不知道」：
1. `src/noc_ops/providers/<cloud>.py` + `sync.py`（扫出来、并进 `data.json`）
2. `src/noc_ops/dashboard/index.html`（tab 与列）
3. `src/noc_ops/skill/SKILL.md.tmpl`（agent 靠它才知道查什么、怎么查）+ 公开 skill 仓 https://github.com/pholex/multicloud-ops 的 `skills/multicloud-ops/SKILL.md`（引流版的云列表与能力标注要跟着改并推送）
4. `README.md`（tab 列表与能力描述）
5. 改完 `ops setup` 重扇出 skill，再按上面重装全局 `ops`

## 前端依赖自托管
看板的 JS / 字体一律放 `src/noc_ops/dashboard/vendor/`，**不走第三方 CDN**；看板服务运行期只用 Python stdlib。
（仅限「看板运行期」这条热路径；`ops setup` 调 `npx skills` 这类构建期成熟工具不受此限。）

## 提交
- 本地提交即可，**push 只在用户明确要求时做**（远程 github.com/pholex/noc-ops 公开，推之前再过一遍标识扫描）。在 `main` 上先开分支：`feat/` `fix/` `chore/` `docs/`，做完即合回 `main`，别攒长命分支。
- 秘密（token / PAT / 密码）进 macOS Keychain 或 Parameter Store，**绝不进仓库、也不进 `ops-config.toml`**。
- 机器特定的东西（具体账号、内网 IP、工号、个人路径）不写进本文件——它可能随发行版一起发出去。

## 公开内容检查
- 每次提交遵守 `PUBLIC_CONTENT.md`：人工复核完整 diff 与提交说明，并运行 `python3 scripts/check-public-content.py --staged`。
- 本机启用 `sh scripts/install-git-hooks.sh`；禁止绕过 hooks。推送前检查待推送提交范围。
- 检查报告只写位置和类别，不回显命中内容；专有标识只保存在 Git 本机元数据目录。
